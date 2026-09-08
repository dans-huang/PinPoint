from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_backend.config import ConfigurationError, Settings
from pinpoint_backend.app import (
    AppleSessionRequest,
    Runtime,
    CompletedPartRequest,
    DeviceBindingRequest,
    UploadAbandonRequest,
    UploadCompleteRequest,
    UploadCreateRequest,
    abandon_upload,
    bind_device,
    complete_upload,
    create_apple_session,
    create_upload,
    get_session_status,
    get_transcription,
    get_upload_job,
    resume_upload_submission,
    unbind_device,
)
from pinpoint_backend.plaud import PlaudPartnerClient, PlaudServiceError
from pinpoint_backend.security import (
    AuthenticationError,
    SecurityService,
    VerifiedAppleIdentity,
    managed_invite_verifier,
)
from pinpoint_backend.state import (
    ActiveDeviceBindings,
    BetaAuthorizationStatus,
    BetaInvitationUnavailable,
    DeviceBindingUnavailable,
    RecorderClaimConflict,
    RecorderLedgerReconciliationRequired,
    StateStore,
    UploadAttemptRetired,
    UploadJobUnavailable,
)
from pinpoint_backend.admin import main as admin_main
from fastapi import HTTPException


SECRET = "s" * 48
USER_ID_SECRET = "u" * 48
APPLE_REFRESH_TOKEN_KEY = base64.urlsafe_b64encode(b"a" * 32).decode("ascii")


def write_apple_private_key(path: Path) -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return str(path)


def upload_create_request(**values) -> UploadCreateRequest:
    values.setdefault(
        "source_id",
        hashlib.sha256(values["idempotency_key"].encode("utf-8")).hexdigest(),
    )
    return UploadCreateRequest(**values)


def upload_complete_request(**values) -> UploadCompleteRequest:
    values.setdefault("idempotency_key", "request-owner-000000000001")
    return UploadCompleteRequest(**values)


class FakeSigningKey:
    key = "apple-public-key"


class FakeJWKClient:
    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        return FakeSigningKey()


class ToggleJWKClient:
    def __init__(self):
        self.should_fail = True

    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        if self.should_fail:
            raise RuntimeError("temporary Apple key failure")
        return FakeSigningKey()


class SettingsTests(unittest.TestCase):
    def test_missing_secrets_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_plain_host_is_required(self):
        settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
            plaud_api_domain="https://platform-us.plaud.ai",
        )
        with self.assertRaises(ConfigurationError):
            settings.validate()

    def test_non_plaud_host_is_rejected(self):
        settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
            plaud_api_domain="attacker.example",
        )
        with self.assertRaises(ConfigurationError):
            settings.validate()

    def test_legacy_invitation_environment_is_optional(self):
        environment = {
            "PINPOINT_SESSION_SECRET": SECRET,
            "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
            "PINPOINT_APPLE_AUDIENCE": "com.example.pinpoint",
            "PLAUD_CLIENT_ID": "client",
            "PLAUD_CLIENT_SECRET": "secret",
            "PLAUD_API_KEY": "api",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.beta_invite_codes, ())

    def test_apple_token_custody_is_disabled_by_default(self):
        settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
        )
        settings.validate()
        self.assertFalse(settings.apple_token_custody_enabled)

    def test_enabled_apple_token_custody_requires_complete_private_config(self):
        settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
            apple_token_custody_enabled=True,
        )
        with self.assertRaisesRegex(ConfigurationError, "settings are missing"):
            settings.validate()

    def test_enabled_apple_token_custody_loads_strict_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = write_apple_private_key(Path(directory) / "AuthKey_TEST.p8")
            environment = {
                "PINPOINT_SESSION_SECRET": SECRET,
                "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
                "PINPOINT_APPLE_AUDIENCE": "com.example.pinpoint",
                "PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED": "true",
                "PINPOINT_APPLE_TEAM_ID": "TEAMID1234",
                "PINPOINT_APPLE_KEY_ID": "KEYID12345",
                "PINPOINT_APPLE_PRIVATE_KEY_PATH": key_path,
                "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1": APPLE_REFRESH_TOKEN_KEY,
                "PLAUD_CLIENT_ID": "client",
                "PLAUD_CLIENT_SECRET": "secret",
                "PLAUD_API_KEY": "api",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_environment()
        self.assertTrue(settings.apple_token_custody_enabled)
        self.assertEqual(settings.apple_private_key_path, key_path)

    def test_apple_token_custody_rejects_ambiguous_boolean(self):
        environment = {
            "PINPOINT_SESSION_SECRET": SECRET,
            "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
            "PINPOINT_APPLE_AUDIENCE": "com.example.pinpoint",
            "PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED": "sometimes",
            "PLAUD_CLIENT_ID": "client",
            "PLAUD_CLIENT_SECRET": "secret",
            "PLAUD_API_KEY": "api",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "must be true or false"):
                Settings.from_environment()


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.clock = time.time()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path, now=lambda: self.clock)
        self.security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock,
            invite_codes=("security-test-invite-0001",),
        )
        self.assertTrue(self.security.authorize_beta_user(
            "pinpoint_abc",
            "security-test-invite-0001",
        ))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def custody_security(self, exchange) -> SecurityService:
        key_path = write_apple_private_key(
            Path(self.temporary_directory.name) / "AuthKey_TEST.p8"
        )
        return SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock,
            apple_token_custody_enabled=True,
            apple_team_id="TEAMID1234",
            apple_key_id="KEYID12345",
            apple_private_key_path=key_path,
            apple_refresh_token_key_v1=APPLE_REFRESH_TOKEN_KEY,
            apple_token_exchange=exchange,
        )

    def test_stable_user_id_is_private_and_deterministic(self):
        first = self.security.stable_user_id("apple-subject-a")
        second = self.security.stable_user_id("apple-subject-a")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("pinpoint_"))
        self.assertNotIn("apple-subject-a", first)

        rotated_session = SecurityService(
            session_secret="r" * 48,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=StateStore(self.database_path, now=lambda: self.clock),
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock,
        )
        self.assertEqual(first, rotated_session.stable_user_id("apple-subject-a"))

    def test_nonce_is_signed_short_lived_and_single_use(self):
        nonce = self.security.issue_nonce()
        claims = jwt.decode(nonce, SECRET, algorithms=["HS256"], issuer="pinpoint")
        self.assertEqual(claims["typ"], "pinpoint_signin_nonce")
        self.assertEqual(claims["exp"] - claims["iat"], 300)

        apple_claims = {
            "sub": "apple-sub",
            "nonce": hashlib.sha256(nonce.encode()).hexdigest(),
        }
        with patch("pinpoint_backend.security.jwt.decode", side_effect=[claims, apple_claims]):
            identity = self.security.verify_apple_identity("apple.jwt", nonce)
        self.assertEqual(identity.subject, "apple-sub")

        with self.assertRaises(AuthenticationError):
            self.security.verify_apple_identity("apple.jwt", nonce)

    def test_apple_authorization_code_is_exchanged_and_refresh_token_is_encrypted(self):
        forms: list[dict[str, str]] = []

        def exchange(form: dict[str, str]) -> dict[str, str]:
            forms.append(form)
            return {
                "id_token": "apple.exchange.jwt",
                "refresh_token": "refresh-token-plaintext",
            }

        security = self.custody_security(exchange)
        with patch(
            "pinpoint_backend.security.jwt.decode",
            return_value={"sub": "apple-subject-a"},
        ):
            security.capture_apple_refresh_token(
                user_id="pinpoint_abc",
                apple_subject="apple-subject-a",
                authorization_code="single-use-authorization-code",
            )

        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["code"], "single-use-authorization-code")
        self.assertEqual(forms[0]["client_id"], "com.example.pinpoint")
        self.assertEqual(forms[0]["grant_type"], "authorization_code")
        self.assertNotIn("refresh-token-plaintext", forms[0]["client_secret"])
        encrypted = self.state.apple_refresh_token_ciphertext("pinpoint_abc")
        self.assertIsNotNone(encrypted)
        self.assertTrue(encrypted.startswith("v1."))
        self.assertNotIn("refresh-token-plaintext", encrypted)
        self.assertEqual(
            security.apple_refresh_token_for_user("pinpoint_abc"),
            "refresh-token-plaintext",
        )

    def test_apple_exchange_subject_must_match_verified_sign_in(self):
        security = self.custody_security(
            lambda form: {
                "id_token": "apple.exchange.jwt",
                "refresh_token": "refresh-token-plaintext",
            }
        )
        with patch(
            "pinpoint_backend.security.jwt.decode",
            return_value={"sub": "apple-subject-b"},
        ):
            with self.assertRaisesRegex(AuthenticationError, "did not match"):
                security.capture_apple_refresh_token(
                    user_id="pinpoint_abc",
                    apple_subject="apple-subject-a",
                    authorization_code="single-use-authorization-code",
                )
        self.assertFalse(self.state.has_apple_refresh_token("pinpoint_abc"))

    def test_apple_exchange_without_new_refresh_token_reuses_existing_custody(self):
        responses = [
            {
                "id_token": "apple.exchange.jwt",
                "refresh_token": "original-refresh-token",
            },
            {"id_token": "apple.exchange.jwt"},
        ]
        security = self.custody_security(lambda form: responses.pop(0))
        with patch(
            "pinpoint_backend.security.jwt.decode",
            return_value={"sub": "apple-subject-a"},
        ):
            security.capture_apple_refresh_token(
                user_id="pinpoint_abc",
                apple_subject="apple-subject-a",
                authorization_code="first-authorization-code",
            )
            first_ciphertext = self.state.apple_refresh_token_ciphertext("pinpoint_abc")
            security.capture_apple_refresh_token(
                user_id="pinpoint_abc",
                apple_subject="apple-subject-a",
                authorization_code="second-authorization-code",
            )
        self.assertEqual(
            self.state.apple_refresh_token_ciphertext("pinpoint_abc"),
            first_ciphertext,
        )
        self.assertEqual(
            security.apple_refresh_token_for_user("pinpoint_abc"),
            "original-refresh-token",
        )

    def test_apple_custody_failure_never_surfaces_credential_values(self):
        security = self.custody_security(
            lambda form: (_ for _ in ()).throw(
                RuntimeError("single-use-authorization-code refresh-token-plaintext")
            )
        )
        with self.assertRaises(AuthenticationError) as failure:
            security.capture_apple_refresh_token(
                user_id="pinpoint_abc",
                apple_subject="apple-subject-a",
                authorization_code="single-use-authorization-code",
            )
        self.assertEqual(str(failure.exception), "Apple authorization code exchange failed")
        self.assertNotIn("single-use-authorization-code", str(failure.exception))

    def test_disabled_apple_custody_preserves_identity_token_only_behavior(self):
        called = False

        def exchange(form):
            nonlocal called
            called = True
            return {}

        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            apple_token_exchange=exchange,
        )
        security.capture_apple_refresh_token(
            user_id="pinpoint_abc",
            apple_subject="apple-subject-a",
            authorization_code=None,
        )
        self.assertFalse(called)
        self.assertFalse(self.state.has_apple_refresh_token("pinpoint_abc"))

    def test_existing_ledger_migrates_to_encrypted_apple_token_custody(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE apple_token_custody")
            connection.commit()

        migrated = StateStore(self.database_path, now=lambda: self.clock)
        self.assertTrue(migrated.beta_user_is_active("pinpoint_abc"))
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(apple_token_custody)"
                )
            }
        self.assertEqual(
            columns,
            {
                "user_id",
                "encrypted_refresh_token",
                "created_at",
                "updated_at",
            },
        )

    def test_session_token_round_trip(self):
        token, expiry = self.security.issue_session("pinpoint_abc")
        self.assertGreater(expiry, datetime.now(timezone.utc))
        self.assertEqual(self.security.verify_session(token), "pinpoint_abc")

    def test_session_refresh_reuses_jti_and_the_original_bearer_can_retry(self):
        clock = [1_900_000_000.0]
        state = StateStore(self.database_path, now=lambda: clock[0])
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=120,
            session_absolute_ttl_seconds=600,
            nonce_ttl_seconds=300,
            state_store=state,
            jwks_client=FakeJWKClient(),
            now=lambda: clock[0],
        )
        original, _ = security.issue_session("pinpoint_abc")
        original_claims = jwt.decode(
            original,
            SECRET,
            algorithms=["HS256"],
            audience="pinpoint",
            issuer="pinpoint",
            options={"verify_exp": False, "verify_iat": False},
        )
        clock[0] += 30
        refreshed, _ = security.refresh_session(original)
        refreshed_claims = jwt.decode(
            refreshed,
            SECRET,
            algorithms=["HS256"],
            audience="pinpoint",
            issuer="pinpoint",
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(original_claims["jti"], refreshed_claims["jti"])
        self.assertEqual(original_claims["sid"], refreshed_claims["sid"])
        self.assertEqual(security.verify_session(original), "pinpoint_abc")
        self.assertEqual(security.verify_session(refreshed), "pinpoint_abc")
        replayed, _ = security.refresh_session(original)
        replayed_claims = jwt.decode(
            replayed,
            SECRET,
            algorithms=["HS256"],
            audience="pinpoint",
            issuer="pinpoint",
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(replayed_claims["jti"], original_claims["jti"])

    def test_logout_revokes_the_active_session_family(self):
        clock = [1_900_000_000.0]
        state = StateStore(self.database_path, now=lambda: clock[0])
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=120,
            session_absolute_ttl_seconds=600,
            nonce_ttl_seconds=300,
            state_store=state,
            jwks_client=FakeJWKClient(),
            now=lambda: clock[0],
        )
        original, _ = security.issue_session("pinpoint_abc")
        clock[0] += 30
        refreshed, _ = security.refresh_session(original)
        security.revoke_session(refreshed)
        with self.assertRaises(AuthenticationError):
            security.verify_session(refreshed)
        with self.assertRaises(AuthenticationError):
            security.verify_session(original)
        with self.assertRaises(AuthenticationError):
            security.refresh_session(refreshed)

    def test_session_family_cannot_refresh_past_its_absolute_lifetime(self):
        clock = [1_900_000_000.0]
        state = StateStore(self.database_path, now=lambda: clock[0])
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=120,
            session_absolute_ttl_seconds=240,
            nonce_ttl_seconds=300,
            state_store=state,
            jwks_client=FakeJWKClient(),
            now=lambda: clock[0],
        )
        token, _ = security.issue_session("pinpoint_abc")
        original_absolute_expiry = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            audience="pinpoint",
            issuer="pinpoint",
            options={"verify_exp": False, "verify_iat": False},
        )["aexp"]
        for _ in range(2):
            clock[0] += 60
            token, _ = security.refresh_session(token)
            claims = jwt.decode(
                token,
                SECRET,
                algorithms=["HS256"],
                audience="pinpoint",
                issuer="pinpoint",
                options={"verify_exp": False, "verify_iat": False},
            )
            self.assertEqual(claims["aexp"], original_absolute_expiry)
        clock[0] += 61
        with self.assertRaises(AuthenticationError):
            security.refresh_session(token)

    def test_invite_code_authorizes_one_user_and_returning_user_needs_no_code(self):
        invite = "one-time-invite-code-0001"
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=StateStore(self.database_path, now=lambda: self.clock),
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock,
            invite_codes=(invite,),
        )
        self.assertTrue(security.authorize_beta_user("pinpoint_first", invite))
        self.assertFalse(security.authorize_beta_user("pinpoint_second", invite))
        self.assertTrue(security.authorize_beta_user("pinpoint_first", None))

    def test_disabled_tester_cannot_sign_in_verify_or_refresh_and_reenable_needs_new_session(self):
        original, _ = self.security.issue_session("pinpoint_abc")

        self.state.set_beta_user_enabled("pinpoint_abc", enabled=False)
        self.assertFalse(self.security.authorize_beta_user(
            "pinpoint_abc",
            "security-test-invite-0001",
        ))
        with self.assertRaises(AuthenticationError):
            self.security.verify_session(original)
        with self.assertRaises(AuthenticationError):
            self.security.refresh_session(original)

        membership = self.state.list_beta_users()[0]
        self.assertEqual(membership.state, "disabled")
        self.state.set_beta_user_enabled("pinpoint_abc", enabled=True)
        self.assertTrue(self.security.authorize_beta_user("pinpoint_abc", None))
        with self.assertRaises(AuthenticationError):
            self.security.verify_session(original)

        replacement, _ = self.security.issue_session("pinpoint_abc")
        self.assertEqual(self.security.verify_session(replacement), "pinpoint_abc")

    def test_session_status_revalidates_membership_without_issuing_a_plaud_token(self):
        token, _ = self.security.issue_session("pinpoint_abc")
        active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=self.database_path,
            ),
            security=self.security,
            plaud=object(),
            state=self.state,
        )
        self.assertEqual(
            get_session_status(
                authorization=f"Bearer {token}",
                active=active,
            ),
            {"status": "active"},
        )

        self.state.set_beta_user_enabled("pinpoint_abc", enabled=False)
        with self.assertRaises(HTTPException) as failure:
            get_session_status(
                authorization=f"Bearer {token}",
                active=active,
            )
        self.assertEqual(failure.exception.status_code, 401)

    def test_failed_apple_verification_releases_nonce_for_a_real_retry(self):
        jwks = ToggleJWKClient()
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=StateStore(self.database_path, now=lambda: self.clock),
            jwks_client=jwks,
            now=lambda: self.clock,
        )
        nonce = security.issue_nonce()
        claims = jwt.decode(nonce, SECRET, algorithms=["HS256"], issuer="pinpoint")

        with self.assertRaises(AuthenticationError):
            security.verify_apple_identity("invalid.apple.jwt", nonce)

        jwks.should_fail = False
        apple_claims = {
            "sub": "apple-sub",
            "nonce": hashlib.sha256(nonce.encode()).hexdigest(),
        }
        with patch("pinpoint_backend.security.jwt.decode", side_effect=[claims, apple_claims]):
            identity = security.verify_apple_identity("valid.apple.jwt", nonce)
        self.assertEqual(identity.subject, "apple-sub")

    def test_nonce_reservation_is_atomic_across_store_instances(self):
        nonce = self.security.issue_nonce()
        claims = jwt.decode(nonce, SECRET, algorithms=["HS256"], issuer="pinpoint")
        first = StateStore(self.database_path, now=lambda: self.clock)
        second = StateStore(self.database_path, now=lambda: self.clock)
        self.assertTrue(first.reserve_nonce(claims["jti"], claims["exp"]))
        self.assertFalse(second.reserve_nonce(claims["jti"], claims["exp"]))


class ManagedInvitationTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1_900_000_000.0]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path, now=lambda: self.clock[0])
        # This live service object deliberately exists before an operator adds
        # invitations through another StateStore instance.
        self.security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock[0],
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_invite(self, code: str, invite_id: str, *, expires_in: float = 3600):
        return StateStore(self.database_path, now=lambda: self.clock[0]).create_beta_invite(
            invite_id=invite_id,
            invite_verifier=managed_invite_verifier(USER_ID_SECRET, code),
            label=invite_id,
            expires_at=self.clock[0] + expires_in,
        )

    def test_managed_invite_works_without_restart_and_is_consumed_once(self):
        code = "ppi_managed-invitation-code-000000000001"
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_new", None),
            BetaAuthorizationStatus.INVITATION_REQUIRED,
        )
        self.create_invite(code, "inv_managedinvite0001")
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_first", code),
            BetaAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_second", code),
            BetaAuthorizationStatus.INVITATION_UNAVAILABLE,
        )
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_first", None),
            BetaAuthorizationStatus.AUTHORIZED,
        )
        invitation = self.state.list_beta_invites()[0]
        self.assertEqual(invitation.state, "consumed")
        self.assertEqual(invitation.consumed_by_user_id, "pinpoint_first")
        self.assertNotIn(code, repr(invitation))
        self.assertNotIn(code.encode("utf-8"), Path(self.database_path).read_bytes())

    def test_expired_and_revoked_invitations_are_unavailable(self):
        expired_code = "ppi_expiring-invitation-code-0000000001"
        self.create_invite(expired_code, "inv_expiredinvite001", expires_in=10)
        self.clock[0] += 11
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_expired", expired_code),
            BetaAuthorizationStatus.INVITATION_UNAVAILABLE,
        )
        self.assertEqual(self.state.list_beta_invites()[0].state, "expired")

        revoked_code = "ppi_revoked-invitation-code-0000000001"
        self.create_invite(revoked_code, "inv_revokedinvite001")
        revoked = self.state.revoke_beta_invite("inv_revokedinvite001")
        self.assertEqual(revoked.state, "revoked")
        self.assertEqual(
            self.security.beta_authorization_status("pinpoint_revoked", revoked_code),
            BetaAuthorizationStatus.INVITATION_UNAVAILABLE,
        )

    def test_concurrent_consumption_authorizes_exactly_one_user(self):
        code = "ppi_concurrent-invitation-code-000000001"
        self.create_invite(code, "inv_concurrent000001")
        barrier = threading.Barrier(2)

        def authorize(user_id: str) -> BetaAuthorizationStatus:
            barrier.wait()
            return self.security.beta_authorization_status(user_id, code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(authorize, ("pinpoint_one", "pinpoint_two")))
        self.assertEqual(results.count(BetaAuthorizationStatus.AUTHORIZED), 1)
        self.assertEqual(results.count(BetaAuthorizationStatus.INVITATION_UNAVAILABLE), 1)

    def test_revoke_and_consume_race_has_one_terminal_winner(self):
        code = "ppi_revoke-consume-race-code-00000000001"
        invite_id = "inv_revokerace000001"
        self.create_invite(code, invite_id)
        barrier = threading.Barrier(2)

        def authorize():
            barrier.wait()
            return self.security.beta_authorization_status("pinpoint_race", code)

        def revoke():
            barrier.wait()
            try:
                return self.state.revoke_beta_invite(invite_id).state
            except BetaInvitationUnavailable:
                return "consumed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            authorization_future = pool.submit(authorize)
            revocation_future = pool.submit(revoke)
            authorization = authorization_future.result()
            revocation = revocation_future.result()
        final_state = self.state.list_beta_invites()[0].state
        if authorization is BetaAuthorizationStatus.AUTHORIZED:
            self.assertEqual(revocation, "consumed")
            self.assertEqual(final_state, "consumed")
        else:
            self.assertEqual(authorization, BetaAuthorizationStatus.INVITATION_UNAVAILABLE)
            self.assertEqual(revocation, "revoked")
            self.assertEqual(final_state, "revoked")

    def test_apple_session_returns_stable_forbidden_enrollment_codes(self):
        class EnrollmentSecurity:
            def __init__(self, result: BetaAuthorizationStatus):
                self.result = result

            def verify_apple_identity(self, identity_token: str, nonce: str):
                return VerifiedAppleIdentity(subject="apple-subject")

            def stable_user_id(self, apple_subject: str) -> str:
                return "pinpoint_new"

            def beta_authorization_status(self, user_id: str, invite_code: str | None):
                return self.result

        cases = {
            BetaAuthorizationStatus.INVITATION_REQUIRED: "invitation_required",
            BetaAuthorizationStatus.INVITATION_UNAVAILABLE: "invitation_unavailable",
            BetaAuthorizationStatus.MEMBERSHIP_DISABLED: "membership_disabled",
        }
        for authorization, expected_code in cases.items():
            with self.subTest(authorization=authorization):
                runtime = SimpleNamespace(security=EnrollmentSecurity(authorization))
                with self.assertRaises(HTTPException) as failure:
                    asyncio.run(create_apple_session(
                        AppleSessionRequest(
                            identity_token="i" * 20,
                            nonce="n" * 20,
                            invite_code=None,
                        ),
                        active=runtime,
                    ))
                self.assertEqual(failure.exception.status_code, 403)
                self.assertEqual(failure.exception.detail["code"], expected_code)

    def test_apple_identity_failure_returns_stable_machine_code(self):
        class InvalidIdentitySecurity:
            def verify_apple_identity(self, identity_token: str, nonce: str):
                raise AuthenticationError("internal verification detail")

        runtime = SimpleNamespace(security=InvalidIdentitySecurity())
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(create_apple_session(
                AppleSessionRequest(
                    identity_token="i" * 20,
                    nonce="n" * 20,
                    invite_code=None,
                ),
                active=runtime,
            ))
        self.assertEqual(failure.exception.status_code, 401)
        self.assertEqual(failure.exception.detail["code"], "apple_identity_invalid")
        self.assertNotIn("internal verification detail", str(failure.exception.detail))

    def test_enabled_apple_custody_requires_the_authorization_code(self):
        class CustodySecurity:
            apple_token_custody_enabled = True

            def __init__(self):
                self.authorization_checked = False

            def verify_apple_identity(self, identity_token: str, nonce: str):
                return VerifiedAppleIdentity(subject="apple-subject")

            def stable_user_id(self, apple_subject: str) -> str:
                return "pinpoint_new"

            def beta_authorization_status(self, user_id: str, invite_code: str | None):
                self.authorization_checked = True
                return BetaAuthorizationStatus.AUTHORIZED

        security = CustodySecurity()
        runtime = SimpleNamespace(security=security)
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(create_apple_session(
                AppleSessionRequest(
                    identity_token="i" * 20,
                    nonce="n" * 20,
                ),
                active=runtime,
            ))
        self.assertEqual(failure.exception.status_code, 401)
        self.assertEqual(
            failure.exception.detail["code"],
            "apple_authorization_code_required",
        )
        self.assertFalse(security.authorization_checked)

    def test_enabled_apple_custody_passes_code_and_never_surfaces_it(self):
        authorization_code = "single-use-apple-authorization-code"

        class FailingCustodySecurity:
            apple_token_custody_enabled = True

            def __init__(self):
                self.captured: tuple[str, str, str] | None = None

            def verify_apple_identity(self, identity_token: str, nonce: str):
                return VerifiedAppleIdentity(subject="apple-subject")

            def stable_user_id(self, apple_subject: str) -> str:
                return "pinpoint_new"

            def beta_authorization_status(self, user_id: str, invite_code: str | None):
                return BetaAuthorizationStatus.AUTHORIZED

            def capture_apple_refresh_token(
                self,
                *,
                user_id: str,
                apple_subject: str,
                authorization_code: str,
            ) -> None:
                self.captured = (user_id, apple_subject, authorization_code)
                raise AuthenticationError("sensitive failure: " + authorization_code)

        security = FailingCustodySecurity()
        runtime = SimpleNamespace(security=security)
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(create_apple_session(
                AppleSessionRequest(
                    identity_token="i" * 20,
                    authorization_code=authorization_code,
                    nonce="n" * 20,
                ),
                active=runtime,
            ))
        self.assertEqual(
            security.captured,
            ("pinpoint_new", "apple-subject", authorization_code),
        )
        self.assertEqual(failure.exception.status_code, 401)
        self.assertEqual(failure.exception.detail["code"], "apple_token_custody_failed")
        self.assertNotIn(authorization_code, str(failure.exception.detail))


class FakePlaudClient:
    def __init__(self):
        self.submitted: list[str] = []
        self.polled: list[str] = []
        self.generated = 0
        self.completed = 0
        self.bound: list[tuple[str, str, str]] = []
        self.unbound: list[tuple[str, str, str]] = []
        self.before_bind = None
        self.before_unbind = None
        self.bind_error: PlaudServiceError | None = None
        self.unbind_error: PlaudServiceError | None = None

    def bind_device(self, *, user_id: str, serial_number: str, device_type: str):
        self.bound.append((user_id, serial_number, device_type))
        if self.before_bind:
            self.before_bind()
        if self.bind_error:
            raise self.bind_error

    def unbind_device(self, *, user_id: str, serial_number: str, device_type: str):
        self.unbound.append((user_id, serial_number, device_type))
        if self.before_unbind:
            self.before_unbind()
        if self.unbind_error:
            raise self.unbind_error

    def generate_upload(self, *, user_id: str, file_size: int, file_type: str):
        self.generated += 1
        return {
            "FileId": "file_owner_123",
            "UploadId": "upload_owner_123",
            "ChunkSize": 5,
            "Parts": [
                {"PartNumber": 1, "PresignedUrl": "https://pinpoint-test.s3.amazonaws.com/part-1?sig=one"},
                {"PartNumber": 2, "PresignedUrl": "https://pinpoint-test.s3.amazonaws.com/part-2?sig=two"},
            ],
        }

    def complete_upload(self, **kwargs):
        self.completed += 1
        return {"DownloadUrl": "https://pinpoint-test.s3.amazonaws.com/audio.mp3?sig=download"}

    def probe_audio_size(self, file_url: str):
        return file_url, 10

    def submit_transcription(self, *, file_url: str, params):
        self.submitted.append(file_url)
        return {"data": {"task_id": "task_owner_123"}}

    def get_transcription(self, transcription_id: str):
        self.polled.append(transcription_id)
        return {"status": "SUCCESS", "data": {"text": "owner transcript"}}


class DeviceLifecycleTests(unittest.TestCase):
    serial_number = "882TESTRECORDER"

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path)
        self.security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            session_absolute_ttl_seconds=7200,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            invite_codes=("device-ledger-invite-0001",),
        )
        self.assertTrue(self.security.authorize_beta_user(
            "pinpoint_owner",
            "device-ledger-invite-0001",
        ))
        self.token = self.security.issue_session("pinpoint_owner")[0]
        self.plaud = FakePlaudClient()
        self.active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=self.database_path,
            ),
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        self.request = DeviceBindingRequest(
            serial_number=self.serial_number,
            device_type="notepins",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def ledger_state(self, *, database_path: str | None = None) -> str | None:
        with closing(sqlite3.connect(database_path or self.database_path)) as connection:
            row = connection.execute(
                "SELECT state FROM device_bindings WHERE user_id = ? AND serial_number = ?",
                ("pinpoint_owner", self.serial_number),
            ).fetchone()
        return None if row is None else str(row[0])

    def test_bind_persists_intent_before_plaud_and_marks_success_bound(self):
        side_effect_states: list[str | None] = []
        self.plaud.before_bind = lambda: side_effect_states.append(self.ledger_state())
        result = asyncio.run(bind_device(
            self.request,
            authorization=f"Bearer {self.token}",
            active=self.active,
        ))
        self.assertEqual(side_effect_states, ["binding"])
        self.assertEqual(result, {"status": "bound"})
        self.assertEqual(self.ledger_state(), "bound")
        self.assertEqual(
            self.plaud.bound,
            [("pinpoint_owner", self.serial_number, "notepins")],
        )

    def test_bind_403_marks_recorder_released_not_owned(self):
        self.plaud.bind_error = PlaudServiceError("DEVICE_BOUND", status_code=403)
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(bind_device(
                self.request,
                authorization=f"Bearer {self.token}",
                active=self.active,
            ))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(
            failure.exception.detail["code"],
            "recorder_claimed_elsewhere",
        )
        self.assertEqual(self.ledger_state(), "released")

    def test_ambiguous_bind_remains_binding_for_safe_reconciliation(self):
        self.plaud.bind_error = PlaudServiceError("response lost")
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(bind_device(
                self.request,
                authorization=f"Bearer {self.token}",
                active=self.active,
            ))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(
            failure.exception.detail["code"],
            "recorder_ownership_unconfirmed",
        )
        self.assertEqual(self.ledger_state(), "binding")

    def test_unbind_persists_release_pending_and_retries_idempotently(self):
        asyncio.run(bind_device(
            self.request,
            authorization=f"Bearer {self.token}",
            active=self.active,
        ))
        side_effect_states: list[str | None] = []
        self.plaud.before_unbind = lambda: side_effect_states.append(self.ledger_state())
        self.plaud.unbind_error = PlaudServiceError("response lost")
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(unbind_device(
                self.request,
                authorization=f"Bearer {self.token}",
                active=self.active,
            ))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(
            failure.exception.detail["code"],
            "recorder_release_unconfirmed",
        )
        self.assertEqual(side_effect_states, ["release_pending"])
        self.assertEqual(self.ledger_state(), "release_pending")

        self.plaud.unbind_error = None
        result = asyncio.run(unbind_device(
            self.request,
            authorization=f"Bearer {self.token}",
            active=self.active,
        ))
        self.assertEqual(result, {"status": "released"})
        self.assertEqual(side_effect_states, ["release_pending", "release_pending"])
        self.assertEqual(self.ledger_state(), "released")
        self.assertEqual(len(self.plaud.unbound), 2)

    def test_unbind_without_a_ledger_claim_is_idempotent_without_side_effect(self):
        result = asyncio.run(unbind_device(
            self.request,
            authorization=f"Bearer {self.token}",
            active=self.active,
        ))
        self.assertEqual(result, {"status": "released"})
        self.assertEqual(self.ledger_state(), None)
        self.assertEqual(self.plaud.unbound, [])

    def test_unbind_cannot_create_a_claim_for_the_wrong_user(self):
        self.state.begin_device_binding(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        )
        other_digest = "3" * 64
        self.assertTrue(self.state.authorize_beta_user(
            user_id="pinpoint_other",
            invite_digest=other_digest,
            allowed_invite_digests={other_digest},
        ))

        with self.assertRaises(RecorderClaimConflict):
            self.state.begin_device_release(
                user_id="pinpoint_other",
                serial_number=self.serial_number,
                device_type="notepins",
            )

        self.assertEqual(self.ledger_state(), "binding")
        self.assertEqual(self.state.list_device_bindings("pinpoint_other"), [])

    def test_cross_user_bind_and_unbind_return_stable_claimed_elsewhere_code(self):
        self.state.begin_device_binding(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        )
        other_digest = "3" * 64
        self.assertTrue(self.state.authorize_beta_user(
            user_id="pinpoint_other",
            invite_digest=other_digest,
            allowed_invite_digests={other_digest},
        ))
        other_token = self.security.issue_session("pinpoint_other")[0]

        for endpoint in (bind_device, unbind_device):
            with self.subTest(endpoint=endpoint.__name__):
                with self.assertRaises(HTTPException) as failure:
                    asyncio.run(endpoint(
                        self.request,
                        authorization=f"Bearer {other_token}",
                        active=self.active,
                    ))
                self.assertEqual(failure.exception.status_code, 409)
                self.assertEqual(failure.exception.detail, {
                    "code": "recorder_claimed_elsewhere",
                    "message": "This recorder is already claimed by another PinPoint user.",
                })

        self.assertEqual(self.ledger_state(), "binding")
        self.assertEqual(self.state.list_device_bindings("pinpoint_other"), [])
        self.assertEqual(self.plaud.bound, [])
        self.assertEqual(self.plaud.unbound, [])

    def test_unbind_retries_after_lost_success_are_idempotently_released(self):
        self.state.begin_device_binding(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        )
        self.state.mark_device_released(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
        )

        result = asyncio.run(unbind_device(
            self.request,
            authorization=f"Bearer {self.token}",
            active=self.active,
        ))
        self.assertEqual(result, {"status": "released"})
        self.assertEqual(self.ledger_state(), "released")
        self.assertEqual(self.plaud.unbound, [])

    def test_bind_does_not_overwrite_release_pending_intent(self):
        self.state.begin_device_binding(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        )
        self.assertTrue(self.state.begin_device_release(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        ))

        with self.assertRaises(HTTPException) as failure:
            asyncio.run(bind_device(
                self.request,
                authorization=f"Bearer {self.token}",
                active=self.active,
            ))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(
            failure.exception.detail["code"],
            "recorder_lifecycle_conflict",
        )
        self.assertIn("unfinished release", failure.exception.detail["message"].lower())
        self.assertEqual(self.ledger_state(), "release_pending")
        self.assertEqual(self.plaud.bound, [])

    def test_unbind_model_mismatch_returns_lifecycle_conflict_without_side_effect(self):
        self.state.begin_device_binding(
            user_id="pinpoint_owner",
            serial_number=self.serial_number,
            device_type="notepins",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE device_bindings SET device_type = 'notepro' WHERE user_id = ? AND serial_number = ?",
                ("pinpoint_owner", self.serial_number),
            )
            connection.commit()

        with self.assertRaises(HTTPException) as failure:
            asyncio.run(unbind_device(
                self.request,
                authorization=f"Bearer {self.token}",
                active=self.active,
            ))

        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(
            failure.exception.detail["code"],
            "recorder_lifecycle_conflict",
        )
        self.assertEqual(self.plaud.unbound, [])
        self.assertEqual(self.ledger_state(), "binding")

    def test_migrated_tester_remains_active_and_can_bind_and_unbind(self):
        legacy_directory = tempfile.TemporaryDirectory()
        self.addCleanup(legacy_directory.cleanup)
        legacy_path = str(Path(legacy_directory.name) / "legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE beta_users (
                    user_id TEXT PRIMARY KEY,
                    invite_digest TEXT NOT NULL UNIQUE,
                    authorized_at REAL NOT NULL
                );
                INSERT INTO beta_users (user_id, invite_digest, authorized_at)
                VALUES ('pinpoint_owner', 'old-invite-digest', 1);
                """
            )
        legacy_state = StateStore(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            membership = connection.execute(
                """
                SELECT membership_state, membership_updated_at
                FROM beta_users WHERE user_id = 'pinpoint_owner'
                """
            ).fetchone()
        self.assertEqual(membership, ("active", 1.0))
        legacy_security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=legacy_state,
            jwks_client=FakeJWKClient(),
        )
        legacy_token = legacy_security.issue_session("pinpoint_owner")[0]
        legacy_plaud = FakePlaudClient()
        legacy_runtime = Runtime(
            settings=self.active.settings,
            security=legacy_security,
            plaud=legacy_plaud,
            state=legacy_state,
        )
        self.assertEqual(
            asyncio.run(bind_device(
                self.request,
                authorization=f"Bearer {legacy_token}",
                active=legacy_runtime,
            )),
            {"status": "bound"},
        )
        self.assertEqual(
            asyncio.run(unbind_device(
                self.request,
                authorization=f"Bearer {legacy_token}",
                active=legacy_runtime,
            )),
            {"status": "released"},
        )
        self.assertEqual(len(legacy_plaud.bound), 1)
        self.assertEqual(len(legacy_plaud.unbound), 1)


class TranscriptionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path)
        self.security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            invite_codes=(
                "transcription-owner-invite-0001",
                "transcription-other-invite-0002",
            ),
        )
        self.plaud = FakePlaudClient()
        self.settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
            state_db_path=self.database_path,
            max_audio_bytes=1_000_000,
        )
        self.active = Runtime(
            settings=self.settings,
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        self.assertTrue(self.security.authorize_beta_user(
            "pinpoint_owner",
            "transcription-owner-invite-0001",
        ))
        self.assertTrue(self.security.authorize_beta_user(
            "pinpoint_other",
            "transcription-other-invite-0002",
        ))
        self.owner_token = self.security.issue_session("pinpoint_owner")[0]
        self.other_token = self.security.issue_session("pinpoint_other")[0]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_only_submitter_can_poll_transcription(self):
        source_id = hashlib.sha256(b"request-owner-000000000001").hexdigest()
        plan = asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        submitted = asyncio.run(
            complete_upload(
                plan["UploadJobId"],
                upload_complete_request(part_list=[
                    CompletedPartRequest(PartNumber=1, ETag="etag-1"),
                    CompletedPartRequest(PartNumber=2, ETag="etag-2"),
                ]),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.assertEqual(submitted["data"]["task_id"], "task_owner_123")
        duplicate_safe = asyncio.run(
            complete_upload(
                plan["UploadJobId"],
                upload_complete_request(part_list=[
                    CompletedPartRequest(PartNumber=1, ETag="etag-1"),
                    CompletedPartRequest(PartNumber=2, ETag="etag-2"),
                ]),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.assertEqual(duplicate_safe["transcription_id"], "task_owner_123")
        self.assertEqual(len(self.plaud.submitted), 1)

        # A new Mac has no local checkpoint and exports may not be byte-for-byte
        # identical. The stable source ledger must return the existing Plaud
        # task before comparing local upload metadata or allocating quota.
        recovered = asyncio.run(
            create_upload(
                upload_create_request(
                    file_size=11,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000002",
                    source_id=source_id,
                ),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.assertEqual(recovered["TranscriptionId"], "task_owner_123")
        self.assertEqual(self.plaud.generated, 1)
        self.assertEqual(len(self.plaud.submitted), 1)

        own_result = asyncio.run(
            get_transcription(
                "task_owner_123",
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.assertEqual(own_result["data"]["text"], "owner transcript")

        with self.assertRaises(HTTPException) as failure:
            asyncio.run(
                get_transcription(
                    "task_owner_123",
                    authorization=f"Bearer {self.other_token}",
                    active=self.active,
                )
            )
        self.assertEqual(failure.exception.status_code, 404)
        self.assertEqual(self.plaud.polled, ["task_owner_123"])

    def test_unapproved_presigned_host_is_rejected(self):
        def unsafe_plan(**kwargs):
            return {
                "FileId": "file",
                "UploadId": "upload",
                "ChunkSize": 10,
                "Parts": [{"PartNumber": 1, "PresignedUrl": "https://attacker.example/audio.mp3"}],
            }

        self.plaud.generate_upload = unsafe_plan
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(
                create_upload(
                    upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                    authorization=f"Bearer {self.owner_token}",
                    active=self.active,
                )
            )
        self.assertEqual(failure.exception.status_code, 422)
        self.assertEqual(self.plaud.submitted, [])

    def test_daily_quota_is_enforced_before_second_plaud_upload(self):
        self.active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=self.database_path,
                max_audio_bytes=1_000_000,
                user_daily_recording_limit=1,
                global_daily_recording_limit=2,
            ),
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(
                create_upload(
                    upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000002"),
                    authorization=f"Bearer {self.owner_token}",
                    active=self.active,
                )
            )
        self.assertEqual(failure.exception.status_code, 429)
        self.assertEqual(self.plaud.generated, 1)

    def test_daily_audio_bytes_quota_is_enforced_before_second_plaud_upload(self):
        self.active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=self.database_path,
                max_audio_bytes=1_000_000,
                user_daily_recording_limit=20,
                global_daily_recording_limit=20,
                user_daily_audio_bytes_limit=15,
                global_daily_audio_bytes_limit=100,
            ),
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(
                create_upload(
                    upload_create_request(file_size=6, file_type="mp3", idempotency_key="request-owner-000000000002"),
                    authorization=f"Bearer {self.owner_token}",
                    active=self.active,
                )
            )
        self.assertEqual(failure.exception.status_code, 429)
        self.assertEqual(self.plaud.generated, 1)

    def test_lost_create_response_replays_the_same_upload_job(self):
        request = upload_create_request(
            file_size=10,
            file_type="mp3",
            idempotency_key="request-owner-000000000001",
        )
        first = asyncio.run(
            create_upload(request, authorization=f"Bearer {self.owner_token}", active=self.active)
        )
        replay = asyncio.run(
            create_upload(request, authorization=f"Bearer {self.owner_token}", active=self.active)
        )
        self.assertEqual(first, replay)
        self.assertEqual(self.plaud.generated, 1)

    def test_nonterminal_source_metadata_conflict_is_frozen(self):
        source_id = "a" * 64
        first = asyncio.run(create_upload(
            upload_create_request(
                file_size=10,
                file_type="mp3",
                idempotency_key="request-owner-000000000001",
                source_id=source_id,
            ),
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(create_upload(
                upload_create_request(
                    file_size=11,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000002",
                    source_id=source_id,
                ),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            ))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(failure.exception.detail["code"], "recording_source_conflict")
        self.assertEqual(self.plaud.generated, 1)
        self.assertEqual(first["UploadJobId"], self.state.upload_job(
            first["UploadJobId"], "pinpoint_owner"
        ).job_id)

    def test_only_canonical_attempt_can_complete_or_abandon(self):
        plan = asyncio.run(create_upload(
            upload_create_request(
                file_size=10,
                file_type="mp3",
                idempotency_key="request-owner-000000000001",
                source_id="b" * 64,
            ),
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        with self.assertRaises(HTTPException) as completion_failure:
            asyncio.run(complete_upload(
                plan["UploadJobId"],
                upload_complete_request(
                    idempotency_key="request-owner-000000000002",
                    part_list=[
                        CompletedPartRequest(PartNumber=1, ETag="etag-1"),
                        CompletedPartRequest(PartNumber=2, ETag="etag-2"),
                    ],
                ),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            ))
        self.assertEqual(completion_failure.exception.detail["code"], "upload_attempt_mismatch")
        with self.assertRaises(HTTPException) as abandon_failure:
            asyncio.run(abandon_upload(
                plan["UploadJobId"],
                UploadAbandonRequest(idempotency_key="request-owner-000000000002"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            ))
        self.assertEqual(abandon_failure.exception.detail["code"], "upload_attempt_mismatch")
        self.assertEqual(self.plaud.completed, 0)

    def test_uploaded_crash_checkpoint_resumes_submission_without_recompletion(self):
        plan = asyncio.run(create_upload(
            upload_create_request(
                file_size=10,
                file_type="mp3",
                idempotency_key="request-owner-000000000001",
                source_id="c" * 64,
            ),
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        job_id = plan["UploadJobId"]
        self.state.begin_upload_completion(job_id, "pinpoint_owner")
        self.state.save_completed_upload(
            job_id,
            "pinpoint_owner",
            "https://pinpoint-test.s3.amazonaws.com/audio.mp3?sig=download",
        )
        resumed = asyncio.run(resume_upload_submission(
            job_id,
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        replayed = asyncio.run(resume_upload_submission(
            job_id,
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        self.assertEqual(resumed["data"]["task_id"], "task_owner_123")
        self.assertEqual(replayed["transcription_id"], "task_owner_123")
        self.assertEqual(self.plaud.completed, 0)
        self.assertEqual(len(self.plaud.submitted), 1)

    def test_concurrent_probe_failure_cannot_retire_an_active_submission(self):
        plan = asyncio.run(create_upload(
            upload_create_request(
                file_size=10,
                file_type="mp3",
                idempotency_key="request-owner-000000000001",
                source_id="f" * 64,
            ),
            authorization=f"Bearer {self.owner_token}",
            active=self.active,
        ))
        job_id = plan["UploadJobId"]
        self.state.begin_upload_completion(job_id, "pinpoint_owner")
        self.state.save_completed_upload(
            job_id,
            "pinpoint_owner",
            "https://pinpoint-test.s3.amazonaws.com/audio.mp3?sig=download",
        )

        probe_lock = threading.Lock()
        probe_count = 0
        first_probe_started = threading.Event()
        second_probe_started = threading.Event()
        submission_started = threading.Event()
        allow_submission_to_finish = threading.Event()

        def racing_probe(file_url: str):
            nonlocal probe_count
            with probe_lock:
                probe_count += 1
                sequence = probe_count
            if sequence == 1:
                first_probe_started.set()
                self.assertTrue(second_probe_started.wait(timeout=5))
                return file_url, 10
            second_probe_started.set()
            self.assertTrue(submission_started.wait(timeout=5))
            raise PlaudServiceError("download expired", status_code=410)

        def blocked_submission(*, file_url: str, params):
            self.plaud.submitted.append(file_url)
            submission_started.set()
            self.assertTrue(allow_submission_to_finish.wait(timeout=5))
            return {"data": {"task_id": "task_owner_123"}}

        self.plaud.probe_audio_size = racing_probe
        self.plaud.submit_transcription = blocked_submission

        def run_resume():
            return asyncio.run(resume_upload_submission(
                job_id,
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            ))

        with ThreadPoolExecutor(max_workers=2) as executor:
            submitter = executor.submit(run_resume)
            if not first_probe_started.wait(timeout=5):
                submitter.result(timeout=0)
                self.fail("The first resume never reached its audio probe")
            stale_probe = executor.submit(run_resume)
            try:
                with self.assertRaises(HTTPException) as failure:
                    stale_probe.result(timeout=5)
                self.assertEqual(failure.exception.status_code, 409)
                self.assertEqual(
                    failure.exception.detail["code"],
                    "transcription_submission_in_progress",
                )
                self.assertEqual(
                    self.state.upload_job(job_id, "pinpoint_owner").state,
                    "submitting",
                )
            finally:
                allow_submission_to_finish.set()
            result = submitter.result(timeout=5)

        self.assertEqual(result["data"]["task_id"], "task_owner_123")
        self.assertEqual(
            self.state.upload_job(job_id, "pinpoint_owner").state,
            "submitted",
        )
        self.assertEqual(len(self.plaud.submitted), 1)

    def test_abandoning_an_incomplete_upload_releases_the_active_slot(self):
        self.active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=self.database_path,
                max_audio_bytes=1_000_000,
                user_daily_recording_limit=3,
                global_daily_recording_limit=3,
                user_active_recording_limit=1,
            ),
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        first = asyncio.run(
            create_upload(
                upload_create_request(
                    file_size=10,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000001",
                ),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        asyncio.run(
            abandon_upload(
                first["UploadJobId"],
                UploadAbandonRequest(idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        second = asyncio.run(
            create_upload(
                upload_create_request(
                    file_size=10,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000002",
                ),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.assertNotEqual(first["UploadJobId"], second["UploadJobId"])

    def test_completed_object_size_must_match_issued_upload_job(self):
        plan = asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )
        self.plaud.probe_audio_size = lambda url: (url, 11)
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(
                complete_upload(
                    plan["UploadJobId"],
                    upload_complete_request(part_list=[
                        CompletedPartRequest(PartNumber=1, ETag="etag-1"),
                        CompletedPartRequest(PartNumber=2, ETag="etag-2"),
                    ]),
                    authorization=f"Bearer {self.owner_token}",
                    active=self.active,
                )
            )
        self.assertEqual(failure.exception.status_code, 422)
        self.assertEqual(self.plaud.submitted, [])

    def test_ambiguous_submission_is_frozen_instead_of_retried(self):
        plan = asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )

        def ambiguous_submit(*, file_url: str, params):
            self.plaud.submitted.append(file_url)
            raise PlaudServiceError("response was lost")

        self.plaud.submit_transcription = ambiguous_submit
        request = upload_complete_request(part_list=[
            CompletedPartRequest(PartNumber=1, ETag="etag-1"),
            CompletedPartRequest(PartNumber=2, ETag="etag-2"),
        ])
        for _ in range(2):
            with self.assertRaises(HTTPException) as failure:
                asyncio.run(
                    complete_upload(
                        plan["UploadJobId"],
                        request,
                        authorization=f"Bearer {self.owner_token}",
                        active=self.active,
                    )
                )
            self.assertEqual(failure.exception.status_code, 409)
            self.assertEqual(
                failure.exception.detail["code"],
                "transcription_submission_unconfirmed",
            )
        self.assertEqual(len(self.plaud.submitted), 1)

    def test_lost_completion_response_is_frozen_instead_of_completed_twice(self):
        plan = asyncio.run(
            create_upload(
                upload_create_request(file_size=10, file_type="mp3", idempotency_key="request-owner-000000000001"),
                authorization=f"Bearer {self.owner_token}",
                active=self.active,
            )
        )

        def ambiguous_complete(**kwargs):
            self.plaud.completed += 1
            raise PlaudServiceError("completion response was lost")

        self.plaud.complete_upload = ambiguous_complete
        request = upload_complete_request(part_list=[
            CompletedPartRequest(PartNumber=1, ETag="etag-1"),
            CompletedPartRequest(PartNumber=2, ETag="etag-2"),
        ])
        for _ in range(2):
            with self.assertRaises(HTTPException) as failure:
                asyncio.run(
                    complete_upload(
                        plan["UploadJobId"],
                        request,
                        authorization=f"Bearer {self.owner_token}",
                        active=self.active,
                    )
                )
            self.assertEqual(failure.exception.status_code, 409)
            self.assertEqual(
                failure.exception.detail["code"],
                "upload_completion_unconfirmed",
            )
        self.assertEqual(self.plaud.completed, 1)


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1_900_000_000.0]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state = StateStore(
            str(Path(self.temporary_directory.name) / "state.sqlite3"),
            now=lambda: self.clock[0],
        )
        self.assertTrue(self.state.authorize_beta_user(
            user_id="pinpoint_owner",
            invite_digest="a" * 64,
            allowed_invite_digests={"a" * 64},
        ))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_stale_submitting_is_frozen_and_releases_active_quota(self):
        reservation = dict(
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=1,
        )
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="job-one",
            request_id="request-owner-000000000001",
            source_id="1" * 64,
            **reservation,
        ))
        self.state.attach_upload(
            job_id="job-one",
            user_id="pinpoint_owner",
            file_id="file-one",
            plaud_upload_id="upload-one",
            part_count=1,
            upload_plan_json='{"ChunkSize":10,"Parts":[]}',
        )
        self.state.begin_upload_completion("job-one", "pinpoint_owner")
        self.state.save_completed_upload(
            "job-one",
            "pinpoint_owner",
            "https://pinpoint-test.s3.amazonaws.com/audio.mp3?sig=download",
        )
        self.state.begin_transcription_submission("job-one", "pinpoint_owner")

        self.clock[0] += 181
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="job-two",
            request_id="request-owner-000000000002",
            source_id="2" * 64,
            **reservation,
        ))
        self.assertEqual(
            self.state.upload_job("job-one", "pinpoint_owner").state,
            "submit_unknown",
        )
        self.assertEqual(
            self.state.upload_job("job-two", "pinpoint_owner").state,
            "generating",
        )

    def test_expired_fresh_completion_is_ambiguous_and_never_retryable(self):
        source_id = "9" * 64
        request_id = "request-owner-000000000001"
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="completing-job",
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            request_id=request_id,
            source_id=source_id,
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=3,
        ))
        self.state.attach_upload(
            job_id="completing-job",
            user_id="pinpoint_owner",
            file_id="file-one",
            plaud_upload_id="upload-one",
            part_count=1,
            upload_plan_json='{"ChunkSize":10,"Parts":[]}',
        )
        self.state.begin_upload_completion(
            "completing-job",
            "pinpoint_owner",
            600,
        )
        with closing(sqlite3.connect(self.state.path)) as connection:
            connection.execute(
                "UPDATE uploads SET expires_at = ? WHERE job_id = ?",
                (self.clock[0] - 1, "completing-job"),
            )
            connection.commit()

        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            session_absolute_ttl_seconds=7200,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock[0],
        )
        token = security.issue_session("pinpoint_owner")[0]
        plaud = FakePlaudClient()
        active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=str(self.state.path),
                max_audio_bytes=1_000_000,
                upload_attempt_lease_seconds=600,
            ),
            security=security,
            plaud=plaud,
            state=self.state,
        )

        with self.assertRaises(HTTPException) as status_failure:
            asyncio.run(get_upload_job(
                "completing-job",
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(status_failure.exception.status_code, 409)
        self.assertEqual(
            status_failure.exception.detail["code"],
            "upload_completion_unconfirmed",
        )

        with self.assertRaises(HTTPException) as completion_failure:
            asyncio.run(complete_upload(
                "completing-job",
                upload_complete_request(
                    idempotency_key=request_id,
                    part_list=[CompletedPartRequest(PartNumber=1, ETag="etag-1")],
                ),
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(completion_failure.exception.status_code, 409)
        self.assertEqual(
            completion_failure.exception.detail["code"],
            "upload_completion_unconfirmed",
        )

        with self.assertRaises(HTTPException) as replacement_failure:
            asyncio.run(create_upload(
                upload_create_request(
                    file_size=10,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000002",
                    source_id=source_id,
                ),
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(replacement_failure.exception.status_code, 409)
        self.assertEqual(
            replacement_failure.exception.detail["code"],
            "upload_completion_unconfirmed",
        )
        self.assertTrue(self.state.completion_is_unknown(
            "completing-job",
            "pinpoint_owner",
        ))
        self.assertEqual(plaud.completed, 0)
        self.assertEqual(plaud.submitted, [])

    def test_stale_generating_replay_is_gone_but_a_fresh_request_can_start(self):
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            session_absolute_ttl_seconds=7200,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock[0],
        )
        token = security.issue_session("pinpoint_owner")[0]
        reservation = dict(
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=1,
        )
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="stale-job",
            request_id="request-owner-000000000001",
            source_id=hashlib.sha256(b"request-owner-000000000001").hexdigest(),
            **reservation,
        ))
        plaud = FakePlaudClient()
        active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=str(self.state.path),
                max_audio_bytes=1_000_000,
                user_active_recording_limit=1,
            ),
            security=security,
            plaud=plaud,
            state=self.state,
        )
        self.clock[0] += 181
        with self.assertRaises(HTTPException) as replay_failure:
            asyncio.run(create_upload(
                upload_create_request(
                    file_size=10,
                    file_type="mp3",
                    idempotency_key="request-owner-000000000001",
                ),
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(replay_failure.exception.status_code, 410)
        self.assertEqual(plaud.generated, 0)

        fresh = asyncio.run(create_upload(
            upload_create_request(
                file_size=10,
                file_type="mp3",
                idempotency_key="request-owner-000000000002",
            ),
            authorization=f"Bearer {token}",
            active=active,
        ))
        self.assertNotEqual(fresh["UploadJobId"], "stale-job")
        self.assertEqual(plaud.generated, 1)

    def test_live_heartbeat_prevents_takeover_then_stale_attempt_retires(self):
        reservation = dict(
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            source_id="c" * 64,
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=3,
            upload_attempt_lease_seconds=600,
        )
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="old-job",
            request_id="request-owner-000000000001",
            **reservation,
        ))
        self.state.attach_upload(
            job_id="old-job",
            user_id="pinpoint_owner",
            file_id="old-file",
            plaud_upload_id="old-upload",
            part_count=1,
            upload_plan_json='{"ChunkSize":10,"Parts":[]}',
        )

        self.clock[0] += 590
        self.state.heartbeat_upload_attempt(
            job_id="old-job",
            user_id="pinpoint_owner",
            request_id="request-owner-000000000001",
            upload_attempt_lease_seconds=600,
        )
        self.clock[0] += 590
        still_live = self.state.reserve_upload_job(
            job_id="new-job",
            request_id="request-owner-000000000002",
            **reservation,
        )
        self.assertIsNotNone(still_live)
        self.assertEqual(still_live.job_id, "old-job")

        self.clock[0] += 11
        with self.assertRaises(UploadAttemptRetired):
            self.state.heartbeat_upload_attempt(
                job_id="old-job",
                user_id="pinpoint_owner",
                request_id="request-owner-000000000001",
                upload_attempt_lease_seconds=600,
            )
        retired = self.state.upload_job("old-job", "pinpoint_owner")
        self.assertIsNotNone(retired)
        self.assertEqual(retired.state, "expired")
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="new-job",
            request_id="request-owner-000000000002",
            **reservation,
        ))
        with self.assertRaises(UploadAttemptRetired):
            self.state.heartbeat_upload_attempt(
                job_id="old-job",
                user_id="pinpoint_owner",
                request_id="request-owner-000000000001",
                upload_attempt_lease_seconds=600,
            )
        self.assertEqual(
            self.state.upload_job("new-job", "pinpoint_owner").state,
            "generating",
        )

    def test_stale_owner_cannot_complete_and_retirement_is_committed(self):
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="stale-complete",
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            request_id="request-owner-000000000001",
            source_id="b" * 64,
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=3,
            upload_attempt_lease_seconds=600,
        ))
        self.state.attach_upload(
            job_id="stale-complete",
            user_id="pinpoint_owner",
            file_id="file-stale",
            plaud_upload_id="upload-stale",
            part_count=1,
            upload_plan_json='{"ChunkSize":10,"Parts":[]}',
        )
        self.clock[0] += 601
        with self.assertRaises(UploadJobUnavailable):
            self.state.begin_upload_completion(
                "stale-complete",
                "pinpoint_owner",
                600,
            )
        self.assertEqual(
            self.state.upload_job("stale-complete", "pinpoint_owner").state,
            "expired",
        )

    def test_direct_complete_freezes_stale_completion_and_submission(self):
        security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            session_absolute_ttl_seconds=7200,
            nonce_ttl_seconds=300,
            state_store=self.state,
            jwks_client=FakeJWKClient(),
            now=lambda: self.clock[0],
        )
        token = security.issue_session("pinpoint_owner")[0]
        plaud = FakePlaudClient()
        active = Runtime(
            settings=Settings(
                session_secret=SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                state_db_path=str(self.state.path),
                max_audio_bytes=1_000_000,
                upload_attempt_lease_seconds=600,
            ),
            security=security,
            plaud=plaud,
            state=self.state,
        )

        def reserve_attached(job_id: str, request_id: str, source_id: str) -> None:
            self.assertIsNone(self.state.reserve_upload_job(
                job_id=job_id,
                user_id="pinpoint_owner",
                file_size=10,
                file_type="mp3",
                request_id=request_id,
                source_id=source_id,
                user_daily_limit=10,
                global_daily_limit=10,
                user_active_limit=3,
                upload_attempt_lease_seconds=600,
            ))
            self.state.attach_upload(
                job_id=job_id,
                user_id="pinpoint_owner",
                file_id=f"file-{job_id}",
                plaud_upload_id=f"upload-{job_id}",
                part_count=1,
                upload_plan_json='{"ChunkSize":10,"Parts":[]}',
            )

        reserve_attached(
            "ambiguous-complete",
            "request-owner-000000000001",
            "7" * 64,
        )
        self.state.begin_upload_completion(
            "ambiguous-complete",
            "pinpoint_owner",
            600,
        )
        self.clock[0] += 181
        with self.assertRaises(HTTPException) as completion_failure:
            asyncio.run(complete_upload(
                "ambiguous-complete",
                upload_complete_request(
                    idempotency_key="request-owner-000000000001",
                    part_list=[CompletedPartRequest(PartNumber=1, ETag="etag-1")],
                ),
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(completion_failure.exception.status_code, 409)
        self.assertEqual(
            completion_failure.exception.detail["code"],
            "upload_completion_unconfirmed",
        )

        reserve_attached(
            "ambiguous-submit",
            "request-owner-000000000002",
            "8" * 64,
        )
        self.state.begin_upload_completion(
            "ambiguous-submit",
            "pinpoint_owner",
            600,
        )
        self.state.save_completed_upload(
            "ambiguous-submit",
            "pinpoint_owner",
            "https://pinpoint-test.s3.amazonaws.com/audio.mp3?sig=download",
        )
        self.state.begin_transcription_submission("ambiguous-submit", "pinpoint_owner")
        self.clock[0] += 181
        with self.assertRaises(HTTPException) as submission_failure:
            asyncio.run(complete_upload(
                "ambiguous-submit",
                upload_complete_request(
                    idempotency_key="request-owner-000000000002",
                    part_list=[CompletedPartRequest(PartNumber=1, ETag="etag-1")],
                ),
                authorization=f"Bearer {token}",
                active=active,
            ))
        self.assertEqual(submission_failure.exception.status_code, 409)
        self.assertEqual(
            submission_failure.exception.detail["code"],
            "transcription_submission_unconfirmed",
        )
        self.assertEqual(plaud.completed, 0)
        self.assertEqual(plaud.submitted, [])

    def test_concurrent_safe_retry_repoints_source_exactly_once(self):
        reservation = dict(
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            source_id="d" * 64,
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=3,
        )
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="old-job",
            request_id="request-owner-000000000001",
            **reservation,
        ))
        self.assertTrue(self.state.fail_upload_job(
            "old-job",
            "pinpoint_owner",
            expected_state="generating",
        ))

        barrier = threading.Barrier(2)

        def reserve(index: int):
            barrier.wait()
            return self.state.reserve_upload_job(
                job_id=f"new-job-{index}",
                request_id=f"request-owner-00000000000{index + 1}",
                **reservation,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, [1, 2]))

        self.assertEqual(sum(result is None for result in results), 1)
        with closing(sqlite3.connect(self.state.path)) as connection:
            mapped_job = connection.execute(
                "SELECT job_id FROM recording_sources WHERE user_id = ? AND source_id = ?",
                ("pinpoint_owner", "d" * 64),
            ).fetchone()[0]
            live_jobs = connection.execute(
                """
                SELECT COUNT(*) FROM uploads
                WHERE user_id = ? AND source_id = ?
                  AND state IN ('generating', 'uploading', 'completing', 'uploaded', 'submitting')
                """,
                ("pinpoint_owner", "d" * 64),
            ).fetchone()[0]
        self.assertIn(mapped_job, {"new-job-1", "new-job-2"})
        self.assertEqual(live_jobs, 1)

    def test_retired_attempt_cannot_complete_after_source_repoint(self):
        reservation = dict(
            user_id="pinpoint_owner",
            file_size=10,
            file_type="mp3",
            source_id="e" * 64,
            user_daily_limit=10,
            global_daily_limit=10,
            user_active_limit=3,
        )
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="old-job",
            request_id="request-owner-000000000001",
            **reservation,
        ))
        self.state.attach_upload(
            job_id="old-job",
            user_id="pinpoint_owner",
            file_id="old-file",
            plaud_upload_id="old-upload",
            part_count=1,
            upload_plan_json='{"ChunkSize":10,"Parts":[]}',
        )
        self.assertTrue(self.state.fail_upload_job(
            "old-job",
            "pinpoint_owner",
            expected_state="uploading",
        ))
        self.assertIsNone(self.state.reserve_upload_job(
            job_id="new-job",
            request_id="request-owner-000000000002",
            **reservation,
        ))
        with self.assertRaises(UploadJobUnavailable):
            self.state.begin_upload_completion("old-job", "pinpoint_owner")
        self.assertEqual(
            self.state.upload_job("new-job", "pinpoint_owner").state,
            "generating",
        )


class PlaudTokenParsingTests(unittest.TestCase):
    def test_expiry_is_required(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 2_000_000_000}).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        parsed = PlaudPartnerClient._jwt_expiry(token)
        self.assertEqual(parsed.timestamp(), 2_000_000_000)

    @staticmethod
    def token(claims: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    def test_user_token_must_match_requested_user_and_partner_client(self):
        client = PlaudPartnerClient(
            client_id="client",
            client_secret="secret",
            api_key="api",
            domain="platform-us.plaud.ai",
        )
        valid = self.token({
            "sub": "handshake-token",
            "user_id": "pinpoint_owner",
            "client_id": "client",
            "exp": int(time.time()) + 3600,
        })
        self.assertGreater(
            client._validated_user_token(valid, expected_user_id="pinpoint_owner").timestamp(),
            time.time(),
        )
        for claims in (
            {"sub": "handshake", "user_id": "pinpoint_other", "client_id": "client", "exp": int(time.time()) + 3600},
            {"sub": "handshake", "user_id": "pinpoint_owner", "client_id": "other", "exp": int(time.time()) + 3600},
            {"sub": "", "user_id": "pinpoint_owner", "client_id": "client", "exp": int(time.time()) + 3600},
        ):
            with self.assertRaises(PlaudServiceError):
                client._validated_user_token(
                    self.token(claims),
                    expected_user_id="pinpoint_owner",
                )

    def test_malformed_jwt_payloads_fail_with_a_controlled_service_error(self):
        non_utf8 = base64.urlsafe_b64encode(b"\xff").decode().rstrip("=")
        non_object = base64.urlsafe_b64encode(json.dumps(["not", "claims"]).encode()).decode().rstrip("=")
        for token in (
            "only.two-parts",
            "header.%%%.signature",
            f"header.{non_utf8}.signature",
            f"header.{non_object}.signature",
        ):
            with self.subTest(token=token):
                with self.assertRaises(PlaudServiceError):
                    PlaudPartnerClient._jwt_claims(token)

    def test_missing_expired_and_excessive_user_token_expiries_are_rejected(self):
        client = PlaudPartnerClient(
            client_id="client",
            client_secret="secret",
            api_key="api",
            domain="platform-us.plaud.ai",
        )
        now = int(time.time())
        for expiry in (None, now - 1, now + 49 * 60 * 60):
            claims = {
                "sub": "handshake-token",
                "user_id": "pinpoint_owner",
                "client_id": "client",
            }
            if expiry is not None:
                claims["exp"] = expiry
            with self.subTest(expiry=expiry):
                with self.assertRaises(PlaudServiceError):
                    client._validated_user_token(
                        self.token(claims),
                        expected_user_id="pinpoint_owner",
                    )

    def test_user_token_request_uses_configured_short_lifetime(self):
        requested_ttl = 30 * 60
        client = PlaudPartnerClient(
            client_id="client",
            client_secret="secret",
            api_key="api",
            domain="platform-us.plaud.ai",
            user_token_ttl_seconds=requested_ttl,
        )
        token = self.token({
            "sub": "handshake-token",
            "user_id": "pinpoint_owner",
            "client_id": "client",
            "exp": int(time.time()) + requested_ttl,
        })
        calls: list[dict] = []
        client._get_partner_token = lambda: "partner-token"

        def post_json(url, *, authorization, body):
            calls.append({
                "url": url,
                "authorization": authorization,
                "body": body,
            })
            return {"access_token": token}

        client._post_json = post_json
        issued = client.issue_user_token("pinpoint_owner")
        self.assertEqual(issued.access_token, token)
        self.assertEqual(calls[0]["body"]["expires_in"], requested_ttl)


class MembershipAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path)
        self.assertTrue(self.state.authorize_beta_user(
            user_id="pinpoint_operator_test",
            invite_digest="b" * 64,
            allowed_invite_digests={"b" * 64},
        ))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_admin(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "PINPOINT_STATE_DB_PATH": self.database_path,
                "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
            },
            clear=False,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            result = admin_main(list(arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_operator_can_disable_list_and_reenable_without_exposing_invite(self):
        disabled = self.run_admin("disable", "pinpoint_operator_test")
        self.assertEqual(disabled[0], 0)
        self.assertIn("disabled", disabled[1])

        listed = self.run_admin("list")
        self.assertEqual(listed[0], 0)
        record = json.loads(listed[1])
        self.assertEqual(record["user_id"], "pinpoint_operator_test")
        self.assertEqual(record["state"], "disabled")
        self.assertNotIn("invite", listed[1].lower())

        enabled = self.run_admin("enable", "pinpoint_operator_test")
        self.assertEqual(enabled[0], 0)
        self.assertTrue(StateStore(self.database_path).beta_user_is_active(
            "pinpoint_operator_test"
        ))

    def test_operator_can_create_list_consume_and_revoke_managed_invites(self):
        live_security = SecurityService(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            session_ttl_seconds=3600,
            nonce_ttl_seconds=300,
            state_store=StateStore(self.database_path),
            jwks_client=FakeJWKClient(),
        )
        created = self.run_admin(
            "invite", "create", "--label", "First member", "--expires-in-hours", "24"
        )
        self.assertEqual(created[0], 0, created[2])
        created_record = json.loads(created[1])
        raw_code = created_record["invite_code"]
        invite_id = created_record["invite_id"]
        self.assertTrue(raw_code.startswith("ppi_"))
        self.assertEqual(
            created_record["activation_url"],
            f"pinpoint://invite?code={raw_code}",
        )
        self.assertEqual(created_record["state"], "available")

        listed = self.run_admin("invite", "list")
        self.assertEqual(listed[0], 0)
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_verifier = connection.execute(
                "SELECT invite_verifier FROM beta_invites WHERE invite_id = ?",
                (invite_id,),
            ).fetchone()[0]
        self.assertNotIn(raw_code, listed[1])
        self.assertNotIn(stored_verifier, created[1])
        self.assertNotIn(stored_verifier, listed[1])
        self.assertNotIn("invite_code", listed[1])
        self.assertNotIn("activation_url", listed[1])
        self.assertNotIn("verifier", listed[1])
        self.assertNotIn("digest", listed[1])
        self.assertEqual(json.loads(listed[1])["invite_id"], invite_id)
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.database_path + suffix)
            if path.exists():
                self.assertNotIn(raw_code.encode("utf-8"), path.read_bytes())

        self.assertEqual(
            live_security.beta_authorization_status("pinpoint_invited", raw_code),
            BetaAuthorizationStatus.AUTHORIZED,
        )
        consumed = json.loads(self.run_admin("invite", "list")[1])
        self.assertEqual(consumed["state"], "consumed")
        self.assertEqual(consumed["consumed_by_user_id"], "pinpoint_invited")
        cannot_revoke = self.run_admin("invite", "revoke", invite_id)
        self.assertEqual(cannot_revoke[0], 1)
        self.assertIn("disable the member", cannot_revoke[2])

        second = json.loads(self.run_admin(
            "invite", "create", "--label", "Second member"
        )[1])
        revoked = self.run_admin("invite", "revoke", second["invite_id"])
        self.assertEqual(revoked[0], 0)
        revoked_record = json.loads(revoked[1])
        self.assertEqual(revoked_record["state"], "revoked")
        self.assertNotIn(second["invite_code"], revoked[1])
        self.assertNotIn("activation_url", revoked[1])
        self.assertNotIn("verifier", revoked[1])
        self.assertNotIn("digest", revoked[1])

    def test_operator_typo_does_not_create_a_new_ledger(self):
        wrong_path = str(Path(self.temporary_directory.name) / "wrong.sqlite3")
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {"PINPOINT_STATE_DB_PATH": wrong_path},
            clear=False,
        ), redirect_stderr(stderr):
            result = admin_main(["list"])
        self.assertEqual(result, 1)
        self.assertIn("not found", stderr.getvalue())
        self.assertFalse(Path(wrong_path).exists())

    def test_disable_refuses_bound_recorder_until_confirmed_operator_release(self):
        serial_number = "882OPERATORTEST"
        self.state.begin_device_binding(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
            device_type="notepins",
        )
        self.state.mark_device_bound(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
        )

        refused = self.run_admin("disable", "pinpoint_operator_test")
        self.assertEqual(refused[0], 1)
        self.assertIn("release active recorder", refused[2])
        self.assertIn(serial_number, refused[2])
        self.assertTrue(self.state.beta_user_is_active("pinpoint_operator_test"))

        listed = self.run_admin("bindings", "pinpoint_operator_test")
        binding = json.loads(listed[1])
        self.assertEqual(binding["serial_number"], serial_number)
        self.assertEqual(binding["state"], "bound")

        class OperatorPlaud:
            def __init__(self):
                self.calls = []

            def unbind_device(self, **kwargs):
                self.calls.append(kwargs)

        plaud = OperatorPlaud()
        with patch("pinpoint_backend.admin._operator_plaud_client", return_value=plaud):
            released = self.run_admin(
                "release",
                "pinpoint_operator_test",
                serial_number,
            )
        self.assertEqual(released[0], 0)
        self.assertEqual(plaud.calls, [{
            "user_id": "pinpoint_operator_test",
            "serial_number": serial_number,
            "device_type": "notepins",
        }])
        self.assertEqual(self.state.list_device_bindings(
            "pinpoint_operator_test"
        )[0].state, "released")

        disabled = self.run_admin("disable", "pinpoint_operator_test")
        self.assertEqual(disabled[0], 0)
        self.assertFalse(self.state.beta_user_is_active("pinpoint_operator_test"))

    def test_disable_atomically_refuses_every_nonreleased_binding_state(self):
        serial_number = "882ALLACTIVESTATES"
        self.state.begin_device_binding(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
            device_type="notepins",
        )
        with self.assertRaises(ActiveDeviceBindings):
            self.state.set_beta_user_enabled("pinpoint_operator_test", enabled=False)

        self.state.mark_device_bound(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
        )
        with self.assertRaises(ActiveDeviceBindings):
            self.state.set_beta_user_enabled("pinpoint_operator_test", enabled=False)

        self.state.begin_device_release(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
            device_type="notepins",
        )
        with self.assertRaises(ActiveDeviceBindings):
            self.state.set_beta_user_enabled("pinpoint_operator_test", enabled=False)
        self.assertTrue(self.state.beta_user_is_active("pinpoint_operator_test"))

    def test_operator_release_works_while_disabled_and_failure_stays_pending(self):
        serial_number = "882DISABLEDTEST"
        self.state.begin_device_binding(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
            device_type="notepins",
        )
        self.state.mark_device_bound(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE beta_users SET membership_state = 'disabled'
                WHERE user_id = 'pinpoint_operator_test'
                """
            )
            connection.commit()

        class OperatorPlaud:
            should_fail = True

            def unbind_device(self, **kwargs):
                if self.should_fail:
                    raise PlaudServiceError("upstream unavailable")

        plaud = OperatorPlaud()
        with patch("pinpoint_backend.admin._operator_plaud_client", return_value=plaud):
            failed = self.run_admin(
                "release",
                "pinpoint_operator_test",
                serial_number,
            )
            self.assertEqual(failed[0], 1)
            self.assertIn("release_pending", failed[2])
            self.assertEqual(self.state.list_device_bindings(
                "pinpoint_operator_test"
            )[0].state, "release_pending")
            self.assertFalse(self.state.beta_user_is_active("pinpoint_operator_test"))

            plaud.should_fail = False
            succeeded = self.run_admin(
                "release",
                "pinpoint_operator_test",
                serial_number,
            )
        self.assertEqual(succeeded[0], 0)
        self.assertEqual(self.state.list_device_bindings(
            "pinpoint_operator_test"
        )[0].state, "released")
        self.assertFalse(self.state.beta_user_is_active("pinpoint_operator_test"))

    def test_operator_release_refuses_a_different_ledger_owner(self):
        serial_number = "882OWNERMATCH"
        self.state.begin_device_binding(
            user_id="pinpoint_operator_test",
            serial_number=serial_number,
            device_type="notepins",
        )
        with patch("pinpoint_backend.admin._operator_plaud_client") as plaud_factory:
            plaud_factory.return_value.unbind_device.return_value = None
            result = self.run_admin(
                "release",
                "pinpoint_someone_else",
                serial_number,
            )
        self.assertEqual(result[0], 1)
        self.assertIn("ambiguous ledger ownership", result[2])
        plaud_factory.return_value.unbind_device.assert_not_called()


class RecorderClaimInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.first = StateStore(self.database_path)
        self.second = StateStore(self.database_path)
        for user_id, digest in (
            ("pinpoint_first_owner", "1" * 64),
            ("pinpoint_second_owner", "2" * 64),
        ):
            self.assertTrue(self.first.authorize_beta_user(
                user_id=user_id,
                invite_digest=digest,
                allowed_invite_digests={digest},
            ))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_cross_user_sequential_claim_is_rejected(self):
        serial_number = "882SHAREDRECORDER"
        self.first.begin_device_binding(
            user_id="pinpoint_first_owner",
            serial_number=serial_number,
            device_type="notepins",
        )

        with self.assertRaises(RecorderClaimConflict):
            self.second.begin_device_binding(
                user_id="pinpoint_second_owner",
                serial_number=serial_number,
                device_type="notepins",
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT user_id, state FROM device_bindings WHERE serial_number = ?",
                (serial_number,),
            ).fetchall()
        self.assertEqual(rows, [("pinpoint_first_owner", "binding")])

    def test_concurrent_cross_user_claim_allows_exactly_one_owner(self):
        serial_number = "882RACERECORDER"
        barrier = threading.Barrier(2)

        def claim(state: StateStore, user_id: str) -> str:
            barrier.wait(timeout=2)
            try:
                state.begin_device_binding(
                    user_id=user_id,
                    serial_number=serial_number,
                    device_type="notepins",
                )
            except RecorderClaimConflict:
                return "conflict"
            return user_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                executor.submit(claim, self.first, "pinpoint_first_owner"),
                executor.submit(claim, self.second, "pinpoint_second_owner"),
            ]
            outcomes = [future.result(timeout=5) for future in results]

        self.assertEqual(outcomes.count("conflict"), 1)
        winners = [outcome for outcome in outcomes if outcome != "conflict"]
        self.assertEqual(len(winners), 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT user_id, state FROM device_bindings WHERE serial_number = ?",
                (serial_number,),
            ).fetchall()
        self.assertEqual(rows, [(winners[0], "binding")])

    def test_global_serial_lock_blocks_a_different_user(self):
        serial_number = "882GLOBALLOCK"
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def hold_first() -> None:
            with self.first.device_lifecycle_lock(
                user_id="pinpoint_first_owner",
                serial_number=serial_number,
            ):
                first_entered.set()
                self.assertTrue(release_first.wait(timeout=2))

        def hold_second() -> None:
            self.assertTrue(first_entered.wait(timeout=2))
            with self.second.device_lifecycle_lock(
                user_id="pinpoint_second_owner",
                serial_number=serial_number,
            ):
                second_entered.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(hold_first)
            second_future = executor.submit(hold_second)
            self.assertTrue(first_entered.wait(timeout=2))
            self.assertFalse(second_entered.wait(timeout=0.1))
            release_first.set()
            first_future.result(timeout=2)
            second_future.result(timeout=2)
        self.assertTrue(second_entered.is_set())

    def test_released_recorder_can_be_claimed_by_another_user(self):
        serial_number = "882REUSABLERECORDER"
        self.first.begin_device_binding(
            user_id="pinpoint_first_owner",
            serial_number=serial_number,
            device_type="notepins",
        )
        self.first.mark_device_released(
            user_id="pinpoint_first_owner",
            serial_number=serial_number,
        )
        self.second.begin_device_binding(
            user_id="pinpoint_second_owner",
            serial_number=serial_number,
            device_type="notepins",
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT user_id, state FROM device_bindings
                WHERE serial_number = ? ORDER BY user_id
                """,
                (serial_number,),
            ).fetchall()
        self.assertEqual(rows, [
            ("pinpoint_first_owner", "released"),
            ("pinpoint_second_owner", "binding"),
        ])

    def test_legacy_duplicate_claims_fail_startup_without_changing_rows(self):
        legacy_path = str(Path(self.temporary_directory.name) / "legacy.sqlite3")
        serial_number = "882LEGACYDUPLICATE"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE device_bindings (
                    user_id TEXT NOT NULL,
                    serial_number TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('binding', 'bound', 'release_pending', 'released')
                    ),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, serial_number)
                )
                """
            )
            connection.executemany(
                "INSERT INTO device_bindings VALUES (?, ?, 'notepins', ?, 1)",
                [
                    ("pinpoint_first_owner", serial_number, "bound"),
                    ("pinpoint_second_owner", serial_number, "release_pending"),
                ],
            )
            connection.commit()

        with self.assertRaises(RecorderLedgerReconciliationRequired) as failure:
            StateStore(legacy_path)
        self.assertIn(serial_number, str(failure.exception))
        self.assertIn("reconcile ownership explicitly", str(failure.exception))
        self.assertIn("No claim was changed", str(failure.exception))

        with closing(sqlite3.connect(legacy_path)) as connection:
            rows = connection.execute(
                "SELECT user_id, state FROM device_bindings ORDER BY user_id"
            ).fetchall()
            index = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'index' AND name = 'device_bindings_one_active_owner'
                """
            ).fetchone()
        self.assertEqual(rows, [
            ("pinpoint_first_owner", "bound"),
            ("pinpoint_second_owner", "release_pending"),
        ])
        self.assertIsNone(index)


if __name__ == "__main__":
    unittest.main()
