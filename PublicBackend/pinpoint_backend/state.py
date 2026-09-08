from __future__ import annotations

import hashlib
import fcntl
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class StateStoreError(RuntimeError):
    pass


class QuotaExceeded(StateStoreError):
    pass


class UploadJobUnavailable(StateStoreError):
    pass


class UploadAttemptMismatch(UploadJobUnavailable):
    pass


class UploadAttemptRetired(UploadJobUnavailable):
    pass


class SessionUnavailable(StateStoreError):
    pass


class TesterMembershipUnavailable(StateStoreError):
    pass


class BetaInvitationUnavailable(StateStoreError):
    pass


class LocalActivationUnavailable(StateStoreError):
    pass


class BetaAuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    INVITATION_REQUIRED = "invitation_required"
    INVITATION_UNAVAILABLE = "invitation_unavailable"
    MEMBERSHIP_DISABLED = "membership_disabled"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


@dataclass(frozen=True)
class UploadJob:
    job_id: str
    user_id: str
    file_id: str | None
    plaud_upload_id: str | None
    file_type: str
    file_size: int
    part_count: int | None
    state: str
    download_url: str | None
    transcription_id: str | None


@dataclass(frozen=True)
class BetaTesterMembership:
    user_id: str
    state: str
    authorized_at: float
    updated_at: float


@dataclass(frozen=True)
class BetaInvitation:
    invite_id: str
    label: str
    state: str
    created_at: float
    expires_at: float
    consumed_at: float | None
    consumed_by_user_id: str | None
    revoked_at: float | None


@dataclass(frozen=True)
class LocalActivation:
    activation_id: str
    state: str
    created_at: float
    expires_at: float
    reserved_at: float | None
    consumed_at: float | None


@dataclass(frozen=True)
class DeviceBinding:
    user_id: str
    serial_number: str
    device_type: str
    state: str
    updated_at: float


class ActiveDeviceBindings(StateStoreError):
    def __init__(self, bindings: list[DeviceBinding]) -> None:
        super().__init__("PinPoint member still has an active recorder binding")
        self.bindings = tuple(bindings)


class DeviceBindingUnavailable(StateStoreError):
    pass


class RecorderClaimConflict(StateStoreError):
    pass


class RecorderLedgerReconciliationRequired(StateStoreError):
    pass


class StateStore:
    """Small durable security ledger shared by backend workers on one host.

    Each operation opens its own SQLite connection. ``BEGIN IMMEDIATE`` plus
    primary-key constraints make nonce consumption and ownership claims atomic
    across threads and processes that share this database file.
    """

    def __init__(self, path: str, *, now=time.time) -> None:
        self._path = Path(path).expanduser().resolve()
        self._now = now
        parent_was_missing = not self._path.parent.exists()
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if parent_was_missing:
            os.chmod(self._path.parent, 0o700)
        elif stat.S_IMODE(self._path.parent.stat().st_mode) & 0o077:
            raise StateStoreError(
                "State database directory must not be accessible to group or other users"
            )
        self._initialize()
        self._secure_files()

    @property
    def path(self) -> Path:
        return self._path

    def assert_healthy(self) -> None:
        """Fail the deployment health check when the durable ledger is unusable."""
        try:
            with self._connection() as connection:
                result = connection.execute("PRAGMA quick_check(1)").fetchone()
                if result is None or result[0] != "ok":
                    raise StateStoreError("State ledger integrity check failed")
                # A read-only mount can pass quick_check but cannot run the
                # service. Take and release the same write lock real requests
                # need without mutating any row.
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
        except StateStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise StateStoreError("State ledger health check failed") from exc

    def consume_fixed_window(
        self,
        *,
        bucket: str,
        subject_digest: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Atomically consume one durable fixed-window request allowance.

        ``subject_digest`` is deliberately restricted to a SHA-256-style hex
        digest so a future caller cannot accidentally persist a raw client IP.
        Every process sharing this SQLite file observes the same counter.
        """
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", bucket):
            raise StateStoreError("Rate-limit bucket is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", subject_digest):
            raise StateStoreError("Rate-limit subject must be a private digest")
        if limit < 1 or window_seconds < 1:
            raise StateStoreError("Rate-limit policy is invalid")

        now = float(self._now())
        window_start = int(now // window_seconds) * window_seconds
        expires_at = window_start + window_seconds
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM fixed_window_limits WHERE expires_at <= ?",
                    (now,),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO fixed_window_limits (
                        bucket, subject_digest, window_start, expires_at, request_count
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(bucket, subject_digest, window_start) DO UPDATE SET
                        request_count = fixed_window_limits.request_count + 1
                    WHERE fixed_window_limits.request_count < ?
                    """,
                    (bucket, subject_digest, window_start, expires_at, limit),
                )
                allowed = cursor.rowcount == 1
                connection.commit()
        except sqlite3.Error as exc:
            raise StateStoreError("Rate-limit ledger is unavailable") from exc
        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=max(1, math.ceil(expires_at - now)),
        )

    def beta_user_is_active(self, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return bool(row and row[0] == "active")

    def upsert_apple_refresh_token(
        self,
        *,
        user_id: str,
        encrypted_refresh_token: str,
    ) -> None:
        """Store only an authenticated ciphertext for an authorized PinPoint member."""
        if not user_id.startswith("pinpoint_") or len(user_id) > 96:
            raise StateStoreError("Apple token owner is invalid")
        if (
            not re.fullmatch(r"v1\.[A-Za-z0-9_-]{32,32768}", encrypted_refresh_token)
        ):
            raise StateStoreError("Apple refresh token ciphertext is invalid")
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            membership = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if membership is None or membership[0] != "active":
                connection.rollback()
                raise TesterMembershipUnavailable("PinPoint access is disabled")
            connection.execute(
                """
                INSERT INTO apple_token_custody (
                    user_id, encrypted_refresh_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_refresh_token = excluded.encrypted_refresh_token,
                    updated_at = excluded.updated_at
                """,
                (user_id, encrypted_refresh_token, now, now),
            )
            connection.commit()

    def has_apple_refresh_token(self, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM apple_token_custody WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None

    def apple_refresh_token_ciphertext(self, user_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT encrypted_refresh_token FROM apple_token_custody WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def create_beta_invite(
        self,
        *,
        invite_id: str,
        invite_verifier: str,
        label: str,
        expires_at: float,
    ) -> BetaInvitation:
        normalized_label = label.strip()
        now = float(self._now())
        if not re.fullmatch(r"inv_[A-Za-z0-9_-]{12,64}", invite_id):
            raise StateStoreError("Invitation id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", invite_verifier):
            raise StateStoreError("Invitation verifier is invalid")
        if (
            not normalized_label
            or len(normalized_label) > 120
            or not normalized_label.isprintable()
        ):
            raise StateStoreError("Invitation label must be 1 to 120 printable characters")
        if not math.isfinite(expires_at) or expires_at <= now:
            raise StateStoreError("Invitation expiry must be in the future")
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO beta_invites (
                        invite_id, invite_verifier, label, state, created_at, expires_at,
                        consumed_at, consumed_by_user_id, revoked_at
                    ) VALUES (?, ?, ?, 'available', ?, ?, NULL, NULL, NULL)
                    """,
                    (invite_id, invite_verifier, normalized_label, now, expires_at),
                )
        except sqlite3.IntegrityError as exc:
            raise BetaInvitationUnavailable("Invitation id or verifier already exists") from exc
        return BetaInvitation(
            invite_id=invite_id,
            label=normalized_label,
            state="available",
            created_at=now,
            expires_at=expires_at,
            consumed_at=None,
            consumed_by_user_id=None,
            revoked_at=None,
        )

    def create_local_activation(
        self,
        *,
        activation_id: str,
        activation_verifier: str,
        expires_at: float,
    ) -> LocalActivation:
        """Store one short-lived activation verifier, never its bearer secret."""
        now = float(self._now())
        if not re.fullmatch(r"act_[A-Za-z0-9_-]{12,64}", activation_id):
            raise StateStoreError("Local activation id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", activation_verifier):
            raise StateStoreError("Local activation verifier is invalid")
        if (
            not math.isfinite(expires_at)
            or expires_at <= now
            or expires_at > now + 10 * 60 + 1
        ):
            raise StateStoreError("Local activation expiry must be within 10 minutes")
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO local_activation_codes (
                        activation_id, activation_verifier, state, created_at,
                        expires_at, reservation_id, reserved_at, consumed_at
                    ) VALUES (?, ?, 'available', ?, ?, NULL, NULL, NULL)
                    """,
                    (activation_id, activation_verifier, now, expires_at),
                )
        except sqlite3.IntegrityError as exc:
            raise LocalActivationUnavailable(
                "Local activation id or verifier already exists"
            ) from exc
        return LocalActivation(
            activation_id=activation_id,
            state="available",
            created_at=now,
            expires_at=expires_at,
            reserved_at=None,
            consumed_at=None,
        )

    def reserve_local_activation(
        self,
        *,
        activation_verifier: str,
        reservation_id: str,
        user_id: str,
        membership_verifier: str,
    ) -> bool:
        """Atomically reserve a code and provision the fixed local identity.

        A reservation blocks concurrent reuse while the Plaud token request is
        in flight. The caller may release it after a confirmed pre-session
        failure, or consume it after successfully creating the session.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", activation_verifier):
            raise StateStoreError("Local activation verifier is invalid")
        if not re.fullmatch(r"res_[A-Za-z0-9_-]{12,64}", reservation_id):
            raise StateStoreError("Local activation reservation is invalid")
        if not user_id.startswith("pinpoint_") or len(user_id) > 96:
            raise StateStoreError("Local activation user is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", membership_verifier):
            raise StateStoreError("Local membership verifier is invalid")
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                """
                UPDATE local_activation_codes
                SET state = 'reserved', reservation_id = ?, reserved_at = ?
                WHERE activation_verifier = ?
                  AND state = 'available'
                  AND expires_at > ?
                """,
                (reservation_id, now, activation_verifier, now),
            )
            if claimed.rowcount != 1:
                connection.rollback()
                return False
            existing = connection.execute(
                """
                SELECT invite_digest, membership_state
                FROM beta_users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != membership_verifier or existing[1] != "active":
                    connection.rollback()
                    return False
            else:
                try:
                    connection.execute(
                        """
                        INSERT INTO consumed_invites (invite_digest, consumed_at)
                        VALUES (?, ?)
                        """,
                        (membership_verifier, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO beta_users (
                            user_id, invite_digest, authorized_at,
                            membership_state, membership_updated_at
                        ) VALUES (?, ?, ?, 'active', ?)
                        """,
                        (user_id, membership_verifier, now, now),
                    )
                except sqlite3.IntegrityError:
                    connection.rollback()
                    return False
            connection.commit()
        return True

    def release_local_activation(
        self,
        *,
        activation_verifier: str,
        reservation_id: str,
    ) -> None:
        """Make a reservation retryable after a confirmed Plaud failure."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE local_activation_codes
                SET state = 'available', reservation_id = NULL, reserved_at = NULL
                WHERE activation_verifier = ? AND reservation_id = ?
                  AND state = 'reserved' AND expires_at > ?
                """,
                (activation_verifier, reservation_id, float(self._now())),
            )

    def consume_local_activation(
        self,
        *,
        activation_verifier: str,
        reservation_id: str,
    ) -> None:
        """Permanently retire exactly the reservation that issued a session."""
        now = float(self._now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE local_activation_codes
                SET state = 'consumed', consumed_at = ?
                WHERE activation_verifier = ? AND reservation_id = ?
                  AND state = 'reserved'
                """,
                (now, activation_verifier, reservation_id),
            )
            if cursor.rowcount != 1:
                raise LocalActivationUnavailable(
                    "Local activation reservation was lost"
                )

    def list_beta_invites(self) -> list[BetaInvitation]:
        now = float(self._now())
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT invite_id, label, state, created_at, expires_at,
                       consumed_at, consumed_by_user_id, revoked_at
                FROM beta_invites ORDER BY created_at, invite_id
                """
            ).fetchall()
        return [self._beta_invitation_from_row(row, now=now) for row in rows]

    def revoke_beta_invite(self, invite_id: str) -> BetaInvitation:
        if not re.fullmatch(r"inv_[A-Za-z0-9_-]{12,64}", invite_id):
            raise BetaInvitationUnavailable("PinPoint invitation was not found")
        now = float(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT invite_id, label, state, created_at, expires_at,
                       consumed_at, consumed_by_user_id, revoked_at
                FROM beta_invites WHERE invite_id = ?
                """,
                (invite_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise BetaInvitationUnavailable("PinPoint invitation was not found")
            if row[2] == "consumed":
                connection.rollback()
                raise BetaInvitationUnavailable(
                    "Consumed invitations cannot be revoked; disable the member instead"
                )
            if row[2] == "available":
                connection.execute(
                    """
                    UPDATE beta_invites
                    SET state = 'revoked', revoked_at = ?
                    WHERE invite_id = ? AND state = 'available'
                    """,
                    (now, invite_id),
                )
                row = connection.execute(
                    """
                    SELECT invite_id, label, state, created_at, expires_at,
                           consumed_at, consumed_by_user_id, revoked_at
                    FROM beta_invites WHERE invite_id = ?
                    """,
                    (invite_id,),
                ).fetchone()
            connection.commit()
        return self._beta_invitation_from_row(row, now=now)

    def set_beta_user_enabled(self, user_id: str, *, enabled: bool) -> None:
        state = "active" if enabled else "disabled"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            membership = connection.execute(
                "SELECT 1 FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if membership is None:
                connection.rollback()
                raise TesterMembershipUnavailable("PinPoint member was not found")
            if not enabled:
                bindings = self._device_bindings_from_rows(connection.execute(
                    """
                    SELECT user_id, serial_number, device_type, state, updated_at
                    FROM device_bindings
                    WHERE user_id = ?
                      AND state IN ('binding', 'bound', 'release_pending')
                    ORDER BY updated_at, serial_number
                    """,
                    (user_id,),
                ).fetchall())
                if bindings:
                    connection.rollback()
                    raise ActiveDeviceBindings(bindings)
            connection.execute(
                """
                UPDATE beta_users
                SET membership_state = ?, membership_updated_at = ?
                WHERE user_id = ?
                """,
                (state, self._now(), user_id),
            )
            if not enabled:
                connection.execute(
                    """
                    UPDATE sessions SET state = 'revoked'
                    WHERE user_id = ? AND state IN ('active', 'rotated')
                    """,
                    (user_id,),
                )
            connection.commit()

    def list_device_bindings(self, user_id: str) -> list[DeviceBinding]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id, serial_number, device_type, state, updated_at
                FROM device_bindings WHERE user_id = ?
                ORDER BY updated_at, serial_number
                """,
                (user_id,),
            ).fetchall()
        return self._device_bindings_from_rows(rows)

    def begin_operator_device_release(self, *, user_id: str, serial_number: str) -> str:
        """Claim an exact ledger-owned recorder for an operator unbind.

        Unlike the user route, this deliberately works while membership is
        disabled. It never creates a binding and refuses ambiguous serial
        ownership across non-released ledger rows.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._device_bindings_from_rows(connection.execute(
                """
                SELECT user_id, serial_number, device_type, state, updated_at
                FROM device_bindings
                WHERE serial_number = ? AND state != 'released'
                ORDER BY updated_at, user_id
                """,
                (serial_number,),
            ).fetchall())
            if len(rows) != 1 or rows[0].user_id != user_id:
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "Recorder binding is missing or has ambiguous ledger ownership"
                )
            binding = rows[0]
            if binding.device_type not in {"notepins", "notepro"}:
                connection.rollback()
                raise DeviceBindingUnavailable("Recorder binding has an invalid model type")
            expected_prefix = "882" if binding.device_type == "notepins" else "881"
            if not binding.serial_number.startswith(expected_prefix):
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "Recorder serial does not match its ledger model type"
                )
            connection.execute(
                """
                UPDATE device_bindings
                SET state = 'release_pending', updated_at = ?
                WHERE user_id = ? AND serial_number = ?
                """,
                (self._now(), user_id, serial_number),
            )
            connection.commit()
        return binding.device_type

    def list_beta_users(self) -> list[BetaTesterMembership]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id, membership_state, authorized_at, membership_updated_at
                FROM beta_users ORDER BY authorized_at, user_id
                """
            ).fetchall()
        return [
            BetaTesterMembership(
                user_id=str(row[0]),
                state=str(row[1]),
                authorized_at=float(row[2]),
                updated_at=float(row[3]),
            )
            for row in rows
        ]

    @contextmanager
    def device_lifecycle_lock(self, *, user_id: str, serial_number: str):
        """Serialize one recorder's cloud lifecycle across backend processes.

        The lock lives beside the private SQLite database, so every worker that
        shares the required persistent volume observes the same advisory lock.
        The filename is a digest; it never exposes a user id or recorder serial.
        """
        # Binding is exclusive at the physical-recorder level, so different
        # users attempting the same serial must share one lock as well.
        digest = hashlib.sha256(f"v2:{serial_number}".encode("utf-8")).hexdigest()
        lock_path = self._path.parent / f".device-{digest}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def reserve_nonce(self, nonce_id: str, expires_at: float) -> bool:
        now = self._now()
        if expires_at <= now:
            return False
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sign_in_nonces WHERE expires_at <= ?", (now,))
            try:
                connection.execute(
                    "INSERT INTO sign_in_nonces (nonce_id, expires_at, state) VALUES (?, ?, 'reserved')",
                    (nonce_id, expires_at),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
        return True

    def release_nonce(self, nonce_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM sign_in_nonces WHERE nonce_id = ? AND state = 'reserved'",
                (nonce_id,),
            )

    def consume_nonce(self, nonce_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sign_in_nonces SET state = 'consumed' WHERE nonce_id = ? AND state = 'reserved'",
                (nonce_id,),
            )
            if cursor.rowcount != 1:
                raise StateStoreError("Sign-in nonce reservation was lost")

    def authorize_beta_user(
        self,
        *,
        user_id: str,
        invite_digest: str | None,
        allowed_invite_digests: set[str],
        managed_invite_verifier: str | None = None,
    ) -> bool:
        """Allow an existing member or atomically consume one invite code."""
        return self.beta_authorization_status(
            user_id=user_id,
            invite_digest=invite_digest,
            allowed_invite_digests=allowed_invite_digests,
            managed_invite_verifier=managed_invite_verifier,
        ) is BetaAuthorizationStatus.AUTHORIZED

    def beta_authorization_status(
        self,
        *,
        user_id: str,
        invite_digest: str | None,
        allowed_invite_digests: set[str],
        managed_invite_verifier: str | None = None,
    ) -> BetaAuthorizationStatus:
        """Authorize an existing member or atomically consume one invitation."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return (
                    BetaAuthorizationStatus.AUTHORIZED
                    if existing[0] == "active"
                    else BetaAuthorizationStatus.MEMBERSHIP_DISABLED
                )
            if not managed_invite_verifier and not invite_digest:
                connection.rollback()
                return BetaAuthorizationStatus.INVITATION_REQUIRED

            now = float(self._now())
            membership_verifier: str | None = None
            if managed_invite_verifier:
                managed = connection.execute(
                    """
                    SELECT state, expires_at FROM beta_invites
                    WHERE invite_verifier = ?
                    """,
                    (managed_invite_verifier,),
                ).fetchone()
                if managed is not None:
                    if managed[0] != "available" or float(managed[1]) <= now:
                        connection.rollback()
                        return BetaAuthorizationStatus.INVITATION_UNAVAILABLE
                    claimed = connection.execute(
                        """
                        UPDATE beta_invites
                        SET state = 'consumed', consumed_at = ?, consumed_by_user_id = ?
                        WHERE invite_verifier = ?
                          AND state = 'available'
                          AND expires_at > ?
                        """,
                        (now, user_id, managed_invite_verifier, now),
                    )
                    if claimed.rowcount != 1:
                        connection.rollback()
                        return BetaAuthorizationStatus.INVITATION_UNAVAILABLE
                    membership_verifier = managed_invite_verifier

            if membership_verifier is None:
                if not invite_digest or invite_digest not in allowed_invite_digests:
                    connection.rollback()
                    return BetaAuthorizationStatus.INVITATION_UNAVAILABLE
                membership_verifier = invite_digest

            consumed = connection.execute(
                "SELECT 1 FROM consumed_invites WHERE invite_digest = ?",
                (membership_verifier,),
            ).fetchone()
            if consumed is not None:
                connection.rollback()
                return BetaAuthorizationStatus.INVITATION_UNAVAILABLE
            try:
                connection.execute(
                    "INSERT INTO consumed_invites (invite_digest, consumed_at) VALUES (?, ?)",
                    (membership_verifier, now),
                )
                connection.execute(
                    """
                    INSERT INTO beta_users
                        (user_id, invite_digest, authorized_at, membership_state,
                         membership_updated_at)
                    VALUES (?, ?, ?, 'active', ?)
                    """,
                    (user_id, membership_verifier, now, now),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return BetaAuthorizationStatus.INVITATION_UNAVAILABLE
            connection.commit()
        return BetaAuthorizationStatus.AUTHORIZED

    def begin_device_binding(self, *, user_id: str, serial_number: str, device_type: str) -> None:
        """Persist intent before the idempotent Plaud bind side effect."""
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            membership = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if membership is None or membership[0] != "active":
                connection.rollback()
                raise TesterMembershipUnavailable("PinPoint access is disabled")
            self._reject_other_active_recorder_claim(
                connection,
                user_id=user_id,
                serial_number=serial_number,
            )
            existing = connection.execute(
                """
                SELECT device_type, state FROM device_bindings
                WHERE user_id = ? AND serial_number = ?
                """,
                (user_id, serial_number),
            ).fetchone()
            if existing is not None and existing[1] == "release_pending":
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "Recorder release is pending and must be completed before binding"
                )
            if (
                existing is not None
                and existing[1] in {"binding", "bound"}
                and existing[0] != device_type
            ):
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "Recorder model does not match its active ledger claim"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO device_bindings
                        (user_id, serial_number, device_type, state, updated_at)
                    VALUES (?, ?, ?, 'binding', ?)
                    ON CONFLICT(user_id, serial_number) DO UPDATE SET
                        device_type = excluded.device_type,
                        state = 'binding',
                        updated_at = excluded.updated_at
                    """,
                    (user_id, serial_number, device_type, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RecorderClaimConflict(
                    "Recorder already has another non-released owner claim"
                ) from exc
            connection.commit()

    def mark_device_bound(self, *, user_id: str, serial_number: str) -> None:
        self._set_device_binding_state(
            user_id=user_id,
            serial_number=serial_number,
            state="bound",
        )

    def mark_device_not_owned(self, *, user_id: str, serial_number: str) -> None:
        # Plaud's DEVICE_BOUND response proves this PinPoint user is not the
        # cloud owner. Treat it as released so account deletion cannot become
        # permanently blocked by a recorder owned elsewhere.
        self._set_device_binding_state(
            user_id=user_id,
            serial_number=serial_number,
            state="released",
        )

    def begin_device_release(
        self,
        *,
        user_id: str,
        serial_number: str,
        device_type: str,
    ) -> bool:
        """Persist release intent only for this user's existing active claim.

        Unbind is not an ownership-discovery operation. It must never create or
        revive a ledger claim, because doing so could block the recorder's real
        owner after an untrusted or stale client request. A missing same-user
        row is an idempotent no-op: this ledger is committed before every Plaud
        bind, retains released tombstones, and never deletes binding history, so
        absence proves that this backend never started that cloud bind.
        """
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            membership = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if membership is None or membership[0] != "active":
                connection.rollback()
                raise TesterMembershipUnavailable("PinPoint access is disabled")
            self._reject_other_active_recorder_claim(
                connection,
                user_id=user_id,
                serial_number=serial_number,
            )
            existing = connection.execute(
                """
                SELECT device_type, state FROM device_bindings
                WHERE user_id = ? AND serial_number = ?
                """,
                (user_id, serial_number),
            ).fetchone()
            if existing is None:
                connection.commit()
                return False
            if existing[0] != device_type:
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "No releasable recorder claim belongs to this user and device type"
                )
            if existing[1] == "released":
                connection.commit()
                return False
            cursor = connection.execute(
                """
                UPDATE device_bindings
                SET state = 'release_pending', updated_at = ?
                WHERE user_id = ? AND serial_number = ? AND device_type = ?
                  AND state IN ('binding', 'bound', 'release_pending')
                """,
                (now, user_id, serial_number, device_type),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise DeviceBindingUnavailable(
                    "No releasable recorder claim belongs to this user and device type"
                )
            connection.commit()
        return True

    def mark_device_released(self, *, user_id: str, serial_number: str) -> None:
        self._set_device_binding_state(
            user_id=user_id,
            serial_number=serial_number,
            state="released",
        )

    def _set_device_binding_state(
        self,
        *,
        user_id: str,
        serial_number: str,
        state: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE device_bindings
                SET state = ?, updated_at = ?
                WHERE user_id = ? AND serial_number = ?
                """,
                (state, self._now(), user_id, serial_number),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StateStoreError("Recorder binding ledger entry was lost")
            connection.commit()

    def create_session(
        self,
        *,
        jti: str,
        family_id: str,
        user_id: str,
        expires_at: float,
        absolute_expires_at: float,
    ) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_sessions(connection, now)
            membership = connection.execute(
                "SELECT membership_state FROM beta_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if membership is None or membership[0] != "active":
                connection.rollback()
                raise TesterMembershipUnavailable("PinPoint access is disabled")
            connection.execute(
                """
                INSERT INTO sessions
                    (jti, family_id, user_id, state, issued_at, expires_at, absolute_expires_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (jti, family_id, user_id, now, expires_at, absolute_expires_at),
            )
            connection.commit()

    def refresh_session(
        self,
        *,
        jti: str,
        family_id: str,
        user_id: str,
        expires_at: float,
        absolute_expires_at: float,
    ) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_sessions(connection, now)
            row = connection.execute(
                """
                SELECT family_id, user_id, absolute_expires_at
                FROM sessions
                WHERE jti = ? AND state = 'active' AND expires_at > ?
                """,
                (jti, now),
            ).fetchone()
            if (
                row is None
                or row[0] != family_id
                or row[1] != user_id
                or float(row[2]) != float(absolute_expires_at)
                or absolute_expires_at <= now
            ):
                connection.rollback()
                raise SessionUnavailable("Session can no longer be refreshed")
            connection.execute(
                "UPDATE sessions SET expires_at = ? WHERE jti = ? AND state = 'active'",
                (expires_at, jti),
            )
            connection.commit()

    def session_is_active(self, *, jti: str, family_id: str, user_id: str) -> bool:
        now = self._now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM sessions
                JOIN beta_users ON beta_users.user_id = sessions.user_id
                WHERE sessions.jti = ? AND sessions.family_id = ?
                  AND sessions.user_id = ? AND sessions.state = 'active'
                  AND sessions.expires_at > ? AND sessions.absolute_expires_at > ?
                  AND beta_users.membership_state = 'active'
                """,
                (jti, family_id, user_id, now, now),
            ).fetchone()
        return row is not None

    def revoke_session_family(self, *, jti: str, family_id: str, user_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE jti = ? AND family_id = ? AND user_id = ?",
                (jti, family_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise SessionUnavailable("Session was not found")
            connection.execute(
                "UPDATE sessions SET state = 'revoked' WHERE family_id = ? AND user_id = ?",
                (family_id, user_id),
            )
            connection.commit()

    def reserve_upload_job(
        self,
        *,
        job_id: str,
        user_id: str,
        file_size: int,
        file_type: str,
        request_id: str,
        source_id: str,
        user_daily_limit: int,
        global_daily_limit: int,
        user_active_limit: int,
        user_daily_bytes_limit: int = 2**63 - 1,
        global_daily_bytes_limit: int = 2**63 - 1,
        ttl_seconds: int = 24 * 60 * 60,
        upload_attempt_lease_seconds: int = 10 * 60,
    ) -> UploadJob | None:
        now = self._now()
        day_start = int(now // 86400) * 86400
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_uploads(connection, now, upload_attempt_lease_seconds)

            def enforce_quotas() -> None:
                user_daily = connection.execute(
                    "SELECT COUNT(*) FROM uploads WHERE user_id = ? AND created_at >= ?",
                    (user_id, day_start),
                ).fetchone()[0]
                global_daily = connection.execute(
                    "SELECT COUNT(*) FROM uploads WHERE created_at >= ?",
                    (day_start,),
                ).fetchone()[0]
                user_daily_bytes = connection.execute(
                    "SELECT COALESCE(SUM(file_size), 0) FROM uploads WHERE user_id = ? AND created_at >= ?",
                    (user_id, day_start),
                ).fetchone()[0]
                global_daily_bytes = connection.execute(
                    "SELECT COALESCE(SUM(file_size), 0) FROM uploads WHERE created_at >= ?",
                    (day_start,),
                ).fetchone()[0]
                active = connection.execute(
                    """
                    SELECT COUNT(*) FROM uploads
                    WHERE user_id = ? AND state IN ('generating', 'uploading', 'completing', 'uploaded', 'submitting')
                    """,
                    (user_id,),
                ).fetchone()[0]
                if user_daily >= user_daily_limit:
                    raise QuotaExceeded("Daily recording limit reached")
                if global_daily >= global_daily_limit:
                    raise QuotaExceeded("PinPoint daily capacity reached")
                if active >= user_active_limit:
                    raise QuotaExceeded("Too many recordings are already in progress")
                if user_daily_bytes + file_size > user_daily_bytes_limit:
                    raise QuotaExceeded("Daily recording data limit reached")
                if global_daily_bytes + file_size > global_daily_bytes_limit:
                    raise QuotaExceeded("PinPoint daily data capacity reached")

            source_row = connection.execute(
                """
                SELECT uploads.job_id, uploads.user_id, uploads.file_id,
                       uploads.plaud_upload_id, uploads.file_type, uploads.file_size,
                       uploads.part_count, uploads.state, uploads.download_url,
                       uploads.transcription_id, uploads.request_id,
                       uploads.completion_unknown
                FROM recording_sources
                LEFT JOIN uploads ON uploads.job_id = recording_sources.job_id
                WHERE recording_sources.user_id = ? AND recording_sources.source_id = ?
                """,
                (user_id, source_id),
            ).fetchone()
            if source_row is not None:
                if source_row[0] is None:
                    connection.rollback()
                    raise StateStoreError("Recording source ledger points to a missing upload job")
                existing = self._upload_from_row(source_row[:10])
                if existing.file_size != file_size or existing.file_type != file_type:
                    connection.commit()
                    return existing
                safe_to_retry = (
                    existing.state in {"failed", "expired"}
                    and not bool(source_row[11])
                )
                if not safe_to_retry or source_row[10] == request_id:
                    connection.commit()
                    return existing
                try:
                    enforce_quotas()
                except QuotaExceeded:
                    connection.rollback()
                    raise
                connection.execute(
                    """
                    INSERT INTO uploads (
                        job_id, user_id, request_id, source_id, file_type, file_size,
                        state, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'generating', ?, ?, ?)
                    """,
                    (
                        job_id, user_id, request_id, source_id, file_type, file_size,
                        now, now, now + ttl_seconds,
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE recording_sources
                    SET job_id = ?, updated_at = ?
                    WHERE user_id = ? AND source_id = ? AND job_id = ?
                    """,
                    (job_id, now, user_id, source_id, existing.job_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise StateStoreError("Recording source changed during upload allocation")
                connection.commit()
                return None

            request_row = connection.execute(
                """
                SELECT job_id, user_id, file_id, plaud_upload_id, file_type,
                       file_size, part_count, state, download_url, transcription_id,
                       source_id
                FROM uploads WHERE user_id = ? AND request_id = ?
                """,
                (user_id, request_id),
            ).fetchone()
            if request_row is not None:
                if request_row[10] not in {None, source_id}:
                    connection.rollback()
                    raise StateStoreError("Upload attempt was already assigned to another recording source")
                connection.execute(
                    "UPDATE uploads SET source_id = ? WHERE job_id = ? AND source_id IS NULL",
                    (source_id, request_row[0]),
                )
                connection.execute(
                    """
                    INSERT INTO recording_sources (user_id, source_id, job_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, source_id, request_row[0], now, now),
                )
                connection.commit()
                return self._upload_from_row(request_row[:10])

            try:
                enforce_quotas()
            except QuotaExceeded:
                connection.rollback()
                raise
            connection.execute(
                """
                INSERT INTO uploads (
                    job_id, user_id, request_id, source_id, file_type, file_size, state,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'generating', ?, ?, ?)
                """,
                (
                    job_id, user_id, request_id, source_id, file_type, file_size,
                    now, now, now + ttl_seconds,
                ),
            )
            connection.execute(
                """
                INSERT INTO recording_sources (user_id, source_id, job_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, source_id, job_id, now, now),
            )
            connection.commit()
        return None

    def attach_upload(
        self,
        *,
        job_id: str,
        user_id: str,
        file_id: str,
        plaud_upload_id: str,
        part_count: int,
        upload_plan_json: str,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET file_id = ?, plaud_upload_id = ?, part_count = ?, upload_plan_json = ?,
                    state = 'uploading', updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'generating'
                """,
                (file_id, plaud_upload_id, part_count, upload_plan_json, self._now(), job_id, user_id),
            )
            if cursor.rowcount != 1:
                raise UploadJobUnavailable("Upload job could not be attached")

    def fail_upload_job(
        self,
        job_id: str,
        user_id: str,
        *,
        expected_state: str,
    ) -> bool:
        """Retire only a state that is still known to precede a side effect.

        Callers commonly perform network validation before asking the store to
        retire a job. Another request may advance the same row while that
        validation is in flight, so a broad ``state != submitted`` update would
        be unsafe: it could erase an active completion or transcription
        submission. Returning ``False`` tells the caller to re-read the row and
        report the state that won the race.
        """
        if expected_state not in {"generating", "uploading", "completing", "uploaded"}:
            raise ValueError("Upload retirement requires a safe expected state")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'failed', completion_unknown = 0,
                    download_url = NULL, upload_plan_json = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = ?
                  AND completion_unknown = 0
                """,
                (self._now(), job_id, user_id, expected_state),
            )
        return cursor.rowcount == 1

    def abandon_upload_job(self, job_id: str, user_id: str) -> None:
        """Release a multipart job that has not entered completion.

        Once object completion starts, abandoning could race a transcription
        submission. Those states deliberately require operator reconciliation.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM uploads WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise UploadJobUnavailable("Upload job not found")
            if row[0] in {"failed", "expired"}:
                connection.commit()
                return
            if row[0] not in {"generating", "uploading"}:
                connection.rollback()
                raise UploadJobUnavailable("Upload job can no longer be abandoned safely")
            connection.execute(
                """
                UPDATE uploads
                SET state = 'failed', completion_unknown = 0,
                    download_url = NULL, upload_plan_json = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ?
                """,
                (self._now(), job_id, user_id),
            )
            connection.commit()

    def heartbeat_upload_attempt(
        self,
        *,
        job_id: str,
        user_id: str,
        request_id: str,
        upload_attempt_lease_seconds: int = 10 * 60,
    ) -> None:
        """Renew only the canonical pre-completion multipart attempt.

        A stale or superseded Mac may still possess working presigned URLs, but
        it can never renew the source lease or advance to completion.
        """
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_uploads(connection, now, upload_attempt_lease_seconds)
            row = connection.execute(
                """
                SELECT uploads.state, uploads.request_id, recording_sources.job_id
                FROM uploads
                LEFT JOIN recording_sources
                  ON recording_sources.user_id = uploads.user_id
                 AND recording_sources.source_id = uploads.source_id
                WHERE uploads.job_id = ? AND uploads.user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.commit()
                raise UploadAttemptRetired("Upload attempt was not found")
            if row[1] != request_id:
                connection.commit()
                raise UploadAttemptMismatch("Upload attempt key does not match")
            if row[2] != job_id or row[0] in {"failed", "expired"}:
                connection.commit()
                raise UploadAttemptRetired("Upload attempt has been retired")
            if row[0] != "uploading":
                connection.commit()
                raise UploadJobUnavailable("Upload attempt is no longer accepting parts")
            cursor = connection.execute(
                """
                UPDATE uploads SET updated_at = ?
                WHERE job_id = ? AND user_id = ? AND request_id = ? AND state = 'uploading'
                """,
                (now, job_id, user_id, request_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise UploadJobUnavailable("Upload attempt lease could not be renewed")
            connection.commit()

    def begin_upload_completion(
        self,
        job_id: str,
        user_id: str,
        upload_attempt_lease_seconds: int = 10 * 60,
    ) -> UploadJob:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_uploads(connection, now, upload_attempt_lease_seconds)
            row = connection.execute(
                """
                SELECT job_id, user_id, file_id, plaud_upload_id, file_type,
                       file_size, part_count, state, download_url, transcription_id,
                       updated_at
                FROM uploads WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.commit()
                raise UploadJobUnavailable("Upload job not found")
            state = row[7]
            if state in {"uploaded", "submitted"}:
                connection.commit()
                return self._upload_from_row(row[:10])
            if state != "uploading":
                connection.commit()
                raise UploadJobUnavailable("Upload job is not ready to complete")
            connection.execute(
                "UPDATE uploads SET state = 'completing', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            connection.commit()
            values = list(row[:10])
            values[7] = "completing"
            return self._upload_from_row(values)

    def mark_completion_unknown(self, job_id: str, user_id: str) -> None:
        """Freeze a possibly completed Plaud multipart request.

        Plaud does not document a completion idempotency key or lookup API. A
        transport/response failure after the call begins must therefore never
        be retried automatically.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'failed', completion_unknown = 1,
                    download_url = NULL, upload_plan_json = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'completing'
                """,
                (self._now(), job_id, user_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT 1 FROM uploads
                    WHERE job_id = ? AND user_id = ?
                      AND state = 'failed' AND completion_unknown = 1
                    """,
                    (job_id, user_id),
                ).fetchone()
                if existing is None:
                    raise UploadJobUnavailable("Ambiguous upload completion could not be recorded")

    def save_completed_upload(self, job_id: str, user_id: str, download_url: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'uploaded', completion_unknown = 0,
                    download_url = ?, upload_plan_json = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ?
                  AND (
                    state = 'completing'
                    OR (state = 'failed' AND completion_unknown = 1)
                  )
                """,
                (download_url, self._now(), job_id, user_id),
            )
            if cursor.rowcount != 1:
                raise UploadJobUnavailable("Completed upload could not be secured")

    def begin_transcription_submission(self, job_id: str, user_id: str) -> UploadJob:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job_id, user_id, file_id, plaud_upload_id, file_type,
                       file_size, part_count, state, download_url, transcription_id
                FROM uploads WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise UploadJobUnavailable("Upload job not found")
            if row[7] == "submitted":
                connection.commit()
                return self._upload_from_row(row)
            if row[7] == "submit_unknown":
                connection.rollback()
                raise UploadJobUnavailable(
                    "Plaud may already be processing this recording. PinPoint will not submit it twice."
                )
            if row[7] != "uploaded" or not row[8]:
                connection.rollback()
                raise UploadJobUnavailable("Upload job is not ready for transcription")
            connection.execute(
                "UPDATE uploads SET state = 'submitting', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            connection.commit()
            values = list(row)
            values[7] = "submitting"
            return self._upload_from_row(values)

    def mark_submission_unknown(self, job_id: str, user_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'submit_unknown', download_url = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ? AND state = 'submitting'
                """,
                (self._now(), job_id, user_id),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT 1 FROM uploads
                    WHERE job_id = ? AND user_id = ? AND state = 'submit_unknown'
                    """,
                    (job_id, user_id),
                ).fetchone()
                if existing is None:
                    raise UploadJobUnavailable("Ambiguous submission could not be recorded")

    def mark_upload_submitted(
        self,
        *,
        job_id: str,
        user_id: str,
        transcription_id: str,
        source_url: str,
    ) -> None:
        source_hash = hashlib.sha256(self._canonical_url(source_url).encode("utf-8")).hexdigest()
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT user_id FROM transcriptions WHERE transcription_id = ?",
                (transcription_id,),
            ).fetchone()
            if existing is not None and existing[0] != user_id:
                connection.rollback()
                raise StateStoreError("Transcription identifier ownership conflict")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO transcriptions
                        (transcription_id, user_id, source_url_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (transcription_id, user_id, source_hash, now),
                )
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = 'submitted', transcription_id = ?, download_url = NULL, updated_at = ?
                WHERE job_id = ? AND user_id = ?
                  AND state IN ('submitting', 'submit_unknown')
                """,
                (transcription_id, now, job_id, user_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise UploadJobUnavailable("Upload job was not ready for transcription")
            connection.commit()

    def upload_job(self, job_id: str, user_id: str) -> UploadJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT job_id, user_id, file_id, plaud_upload_id, file_type,
                       file_size, part_count, state, download_url, transcription_id
                FROM uploads WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
        return None if row is None else self._upload_from_row(row)

    def expire_and_get_upload_job(
        self,
        job_id: str,
        user_id: str,
        *,
        upload_attempt_lease_seconds: int = 10 * 60,
    ) -> UploadJob | None:
        """Return current state after atomically retiring stale safe attempts."""
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_uploads(connection, now, upload_attempt_lease_seconds)
            row = connection.execute(
                """
                SELECT job_id, user_id, file_id, plaud_upload_id, file_type,
                       file_size, part_count, state, download_url, transcription_id
                FROM uploads WHERE job_id = ? AND user_id = ?
                """,
                (job_id, user_id),
            ).fetchone()
            connection.commit()
        return None if row is None else self._upload_from_row(row)

    def upload_attempt_matches(self, job_id: str, user_id: str, request_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM uploads
                WHERE job_id = ? AND user_id = ? AND request_id = ?
                """,
                (job_id, user_id, request_id),
            ).fetchone()
        return row is not None

    def upload_plan_json(self, job_id: str, user_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT upload_plan_json FROM uploads WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
        return None if row is None else row[0]

    def completion_is_unknown(self, job_id: str, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT completion_unknown FROM uploads WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
        return bool(row and row[0])

    def owns_transcription(self, transcription_id: str, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM transcriptions WHERE transcription_id = ? AND user_id = ?",
                (transcription_id, user_id),
            ).fetchone()
        return row is not None

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sign_in_nonces (
                    nonce_id TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'consumed'))
                );
                CREATE INDEX IF NOT EXISTS sign_in_nonces_expiry
                    ON sign_in_nonces (expires_at);

                CREATE TABLE IF NOT EXISTS beta_users (
                    user_id TEXT PRIMARY KEY,
                    invite_digest TEXT NOT NULL UNIQUE,
                    authorized_at REAL NOT NULL,
                    membership_state TEXT NOT NULL DEFAULT 'active'
                        CHECK (membership_state IN ('active', 'disabled')),
                    membership_updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS apple_token_custody (
                    user_id TEXT PRIMARY KEY,
                    encrypted_refresh_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS consumed_invites (
                    invite_digest TEXT PRIMARY KEY,
                    consumed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS beta_invites (
                    invite_id TEXT PRIMARY KEY,
                    invite_verifier TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('available', 'consumed', 'revoked')),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    consumed_by_user_id TEXT,
                    revoked_at REAL,
                    CHECK (expires_at > created_at),
                    CHECK (
                        (state = 'available' AND consumed_at IS NULL
                            AND consumed_by_user_id IS NULL AND revoked_at IS NULL)
                        OR (state = 'consumed' AND consumed_at IS NOT NULL
                            AND consumed_by_user_id IS NOT NULL AND revoked_at IS NULL)
                        OR (state = 'revoked' AND consumed_at IS NULL
                            AND consumed_by_user_id IS NULL AND revoked_at IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS beta_invites_state_expiry
                    ON beta_invites (state, expires_at);

                CREATE TABLE IF NOT EXISTS local_activation_codes (
                    activation_id TEXT PRIMARY KEY,
                    activation_verifier TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('available', 'reserved', 'consumed')
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    reservation_id TEXT UNIQUE,
                    reserved_at REAL,
                    consumed_at REAL,
                    CHECK (expires_at > created_at),
                    CHECK (
                        (state = 'available' AND reservation_id IS NULL
                            AND reserved_at IS NULL AND consumed_at IS NULL)
                        OR (state = 'reserved' AND reservation_id IS NOT NULL
                            AND reserved_at IS NOT NULL AND consumed_at IS NULL)
                        OR (state = 'consumed' AND reservation_id IS NOT NULL
                            AND reserved_at IS NOT NULL AND consumed_at IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS local_activation_expiry
                    ON local_activation_codes (state, expires_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    jti TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'rotated', 'revoked', 'expired')),
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    absolute_expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_family
                    ON sessions (family_id, user_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry
                    ON sessions (state, expires_at);

                CREATE TABLE IF NOT EXISTS fixed_window_limits (
                    bucket TEXT NOT NULL,
                    subject_digest TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    request_count INTEGER NOT NULL CHECK (request_count > 0),
                    PRIMARY KEY (bucket, subject_digest, window_start)
                );
                CREATE INDEX IF NOT EXISTS fixed_window_limits_expiry
                    ON fixed_window_limits (expires_at);

                CREATE TABLE IF NOT EXISTS device_bindings (
                    user_id TEXT NOT NULL,
                    serial_number TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('binding', 'bound', 'release_pending', 'released')
                    ),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, serial_number)
                );
                CREATE INDEX IF NOT EXISTS device_bindings_owner_state
                    ON device_bindings (user_id, state, updated_at);

                CREATE TABLE IF NOT EXISTS transcriptions (
                    transcription_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_url_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transcriptions_owner
                    ON transcriptions (user_id, created_at);

                CREATE TABLE IF NOT EXISTS uploads (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    request_id TEXT,
                    source_id TEXT,
                    file_id TEXT,
                    plaud_upload_id TEXT,
                    file_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    part_count INTEGER,
                    state TEXT NOT NULL CHECK (
                        state IN ('generating', 'uploading', 'completing', 'uploaded', 'submitting', 'submitted', 'submit_unknown', 'failed', 'expired')
                    ),
                    download_url TEXT,
                    transcription_id TEXT UNIQUE,
                    upload_plan_json TEXT,
                    completion_unknown INTEGER NOT NULL DEFAULT 0 CHECK (completion_unknown IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS uploads_owner_created
                    ON uploads (user_id, created_at);
                CREATE INDEX IF NOT EXISTS uploads_state_expiry
                    ON uploads (state, expires_at);

                CREATE TABLE IF NOT EXISTS recording_sources (
                    user_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS recording_sources_job
                    ON recording_sources (job_id);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            duplicate_claim = connection.execute(
                """
                SELECT serial_number, COUNT(*)
                FROM device_bindings
                WHERE state != 'released'
                GROUP BY serial_number
                HAVING COUNT(*) > 1
                ORDER BY serial_number
                LIMIT 1
                """
            ).fetchone()
            if duplicate_claim is not None:
                connection.rollback()
                raise RecorderLedgerReconciliationRequired(
                    "Recorder ledger has duplicate non-released claims for serial "
                    f"{duplicate_claim[0]}; reconcile ownership explicitly before startup. "
                    "No claim was changed."
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS device_bindings_one_active_owner
                ON device_bindings (serial_number) WHERE state != 'released'
                """
            )
            connection.commit()
            columns = {row[1] for row in connection.execute("PRAGMA table_info(uploads)")}
            if "request_id" not in columns:
                connection.execute("ALTER TABLE uploads ADD COLUMN request_id TEXT")
            if "source_id" not in columns:
                connection.execute("ALTER TABLE uploads ADD COLUMN source_id TEXT")
            if "upload_plan_json" not in columns:
                connection.execute("ALTER TABLE uploads ADD COLUMN upload_plan_json TEXT")
            if "completion_unknown" not in columns:
                connection.execute(
                    "ALTER TABLE uploads ADD COLUMN completion_unknown INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uploads_owner_request
                ON uploads (user_id, request_id) WHERE request_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS uploads_owner_source
                ON uploads (user_id, source_id) WHERE source_id IS NOT NULL
                """
            )
            beta_user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(beta_users)")
            }
            if "membership_state" not in beta_user_columns:
                connection.execute(
                    """
                    ALTER TABLE beta_users
                    ADD COLUMN membership_state TEXT NOT NULL DEFAULT 'active'
                        CHECK (membership_state IN ('active', 'disabled'))
                    """
                )
            if "membership_updated_at" not in beta_user_columns:
                connection.execute(
                    """
                    ALTER TABLE beta_users
                    ADD COLUMN membership_updated_at REAL NOT NULL DEFAULT 0
                    """
                )
                connection.execute(
                    """
                    UPDATE beta_users SET membership_updated_at = authorized_at
                    WHERE membership_updated_at = 0
                    """
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO consumed_invites (invite_digest, consumed_at)
                SELECT invite_digest, authorized_at FROM beta_users
                """
            )

    @staticmethod
    def _upload_from_row(row) -> UploadJob:
        return UploadJob(
            job_id=row[0],
            user_id=row[1],
            file_id=row[2],
            plaud_upload_id=row[3],
            file_type=row[4],
            file_size=row[5],
            part_count=row[6],
            state=row[7],
            download_url=row[8],
            transcription_id=row[9],
        )

    @staticmethod
    def _device_bindings_from_rows(rows) -> list[DeviceBinding]:
        return [
            DeviceBinding(
                user_id=str(row[0]),
                serial_number=str(row[1]),
                device_type=str(row[2]),
                state=str(row[3]),
                updated_at=float(row[4]),
            )
            for row in rows
        ]

    @staticmethod
    def _beta_invitation_from_row(row, *, now: float) -> BetaInvitation:
        stored_state = str(row[2])
        visible_state = (
            "expired"
            if stored_state == "available" and float(row[4]) <= now
            else stored_state
        )
        return BetaInvitation(
            invite_id=str(row[0]),
            label=str(row[1]),
            state=visible_state,
            created_at=float(row[3]),
            expires_at=float(row[4]),
            consumed_at=None if row[5] is None else float(row[5]),
            consumed_by_user_id=None if row[6] is None else str(row[6]),
            revoked_at=None if row[7] is None else float(row[7]),
        )

    @staticmethod
    def _reject_other_active_recorder_claim(
        connection: sqlite3.Connection,
        *,
        user_id: str,
        serial_number: str,
    ) -> None:
        conflict = connection.execute(
            """
            SELECT 1 FROM device_bindings
            WHERE serial_number = ? AND user_id != ? AND state != 'released'
            LIMIT 1
            """,
            (serial_number, user_id),
        ).fetchone()
        if conflict is not None:
            connection.rollback()
            raise RecorderClaimConflict(
                "Recorder already has another non-released owner claim"
            )

    @staticmethod
    def _expire_uploads(
        connection: sqlite3.Connection,
        now: float,
        upload_attempt_lease_seconds: int = 10 * 60,
    ) -> None:
        # A process may die after atomically entering an external side effect
        # but before storing its result. Once the short execution window has
        # passed, freeze those rows instead of repeating a potentially billed
        # operation. Frozen rows do not consume active upload quota.
        # A stale pre-side-effect reservation can safely retire: at worst the
        # failed worker left an orphan multipart object, never a transcription.
        connection.execute(
            """
            UPDATE uploads
            SET state = 'failed', completion_unknown = 0,
                download_url = NULL, upload_plan_json = NULL, updated_at = ?
            WHERE state = 'generating' AND updated_at <= ?
            """,
            (now, now - 180),
        )
        connection.execute(
            """
            UPDATE uploads
            SET state = 'expired', completion_unknown = 0,
                download_url = NULL, upload_plan_json = NULL, updated_at = ?
            WHERE state = 'uploading' AND updated_at <= ?
            """,
            (now, now - upload_attempt_lease_seconds),
        )
        connection.execute(
            """
            UPDATE uploads
            SET state = 'failed', completion_unknown = 1,
                download_url = NULL, upload_plan_json = NULL, updated_at = ?
            WHERE state = 'completing' AND updated_at <= ?
            """,
            (now, now - 180),
        )
        connection.execute(
            """
            UPDATE uploads
            SET state = 'submit_unknown', download_url = NULL, updated_at = ?
            WHERE state = 'submitting' AND updated_at <= ?
            """,
            (now, now - 180),
        )
        # Entering ``completing`` is the durable marker immediately before the
        # non-idempotent Plaud completion call. Even a freshly updated row must
        # become ambiguous—not safely retryable—when its absolute job TTL
        # expires, because that call may already be in flight.
        connection.execute(
            """
            UPDATE uploads
            SET state = 'failed', completion_unknown = 1,
                download_url = NULL, upload_plan_json = NULL, updated_at = ?
            WHERE expires_at <= ? AND state = 'completing'
            """,
            (now, now),
        )
        connection.execute(
            """
            UPDATE uploads
            SET state = 'expired', completion_unknown = 0,
                download_url = NULL, upload_plan_json = NULL, updated_at = ?
            WHERE expires_at <= ? AND state IN ('generating', 'uploading', 'uploaded')
            """,
            (now, now),
        )

    @staticmethod
    def _expire_sessions(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE sessions SET state = 'expired'
            WHERE state = 'active' AND (expires_at <= ? OR absolute_expires_at <= ?)
            """,
            (now, now),
        )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()
            self._secure_files()

    def _secure_files(self) -> None:
        for path in (
            self._path,
            Path(str(self._path) + "-wal"),
            Path(str(self._path) + "-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    @staticmethod
    def _canonical_url(value: str) -> str:
        parts = urlsplit(value)
        # Never persist a signed audio URL or its query string. The digest only
        # correlates an ownership claim with the same storage object path.
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
