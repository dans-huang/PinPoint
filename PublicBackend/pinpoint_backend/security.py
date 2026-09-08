from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jwt import PyJWKClient

from .config import ConfigurationError
from .state import (
    BetaAuthorizationStatus,
    SessionUnavailable,
    StateStore,
    StateStoreError,
    TesterMembershipUnavailable,
)


APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_TOKEN_RESPONSE_LIMIT = 64 * 1024


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedAppleIdentity:
    subject: str


class SecurityService:
    def __init__(
        self,
        *,
        session_secret: str,
        user_id_secret_v1: str,
        apple_audience: str,
        session_ttl_seconds: int,
        nonce_ttl_seconds: int,
        state_store: StateStore,
        jwks_client: PyJWKClient | None = None,
        now: Callable[[], float] = time.time,
        session_absolute_ttl_seconds: int | None = None,
        invite_codes: tuple[str, ...] = (),
        apple_token_custody_enabled: bool = False,
        apple_team_id: str = "",
        apple_key_id: str = "",
        apple_private_key_path: str = "",
        apple_refresh_token_key_v1: str = "",
        apple_token_exchange: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self._secret = session_secret
        self._user_id_secret_v1 = user_id_secret_v1
        self._audience = apple_audience
        self._session_ttl = session_ttl_seconds
        self._session_absolute_ttl = session_absolute_ttl_seconds or session_ttl_seconds
        self._nonce_ttl = nonce_ttl_seconds
        self._jwks = jwks_client or PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
        self._now = now
        self._state = state_store
        self._allowed_invite_digests = {
            self._invite_digest(code) for code in invite_codes if code.strip()
        }
        self._apple_token_custody_enabled = apple_token_custody_enabled
        self._apple_team_id = apple_team_id
        self._apple_key_id = apple_key_id
        self._apple_private_key: ec.EllipticCurvePrivateKey | None = None
        self._apple_refresh_token_cipher: AESGCM | None = None
        self._apple_token_exchange = apple_token_exchange or self._exchange_apple_token
        if apple_token_custody_enabled:
            try:
                key_data = Path(apple_private_key_path).read_bytes()
                private_key = serialization.load_pem_private_key(key_data, password=None)
                encryption_key = _urlsafe_base64_decode(apple_refresh_token_key_v1)
            except Exception as exc:
                raise ConfigurationError(
                    "Apple token custody cryptographic material could not be loaded"
                ) from exc
            if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
                private_key.curve, ec.SECP256R1
            ):
                raise ConfigurationError(
                    "PINPOINT_APPLE_PRIVATE_KEY_PATH must contain an Apple P-256 private key"
                )
            if len(encryption_key) != 32:
                raise ConfigurationError(
                    "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1 must decode to exactly 32 bytes"
                )
            self._apple_private_key = private_key
            self._apple_refresh_token_cipher = AESGCM(encryption_key)

    @property
    def apple_token_custody_enabled(self) -> bool:
        return self._apple_token_custody_enabled

    def issue_nonce(self) -> str:
        now = int(self._now())
        payload = {
            "iss": "pinpoint",
            "typ": "pinpoint_signin_nonce",
            "jti": secrets.token_urlsafe(18),
            "rnd": secrets.token_urlsafe(24),
            "iat": now,
            "exp": now + self._nonce_ttl,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify_apple_identity(self, identity_token: str, raw_nonce: str) -> VerifiedAppleIdentity:
        nonce_claim = self._decode_nonce(raw_nonce)
        nonce_id = str(nonce_claim["jti"])
        if not self._state.reserve_nonce(nonce_id, float(nonce_claim["exp"])):
            raise AuthenticationError("Sign-in nonce was already used")
        try:
            key = self._jwks.get_signing_key_from_jwt(identity_token)
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=APPLE_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
        except Exception as exc:
            self._state.release_nonce(nonce_id)
            raise AuthenticationError("Apple identity token verification failed") from exc

        expected_nonce = hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()
        actual_nonce = str(claims.get("nonce", ""))
        if not hmac.compare_digest(expected_nonce, actual_nonce):
            self._state.release_nonce(nonce_id)
            raise AuthenticationError("Apple sign-in nonce did not match")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            self._state.release_nonce(nonce_id)
            raise AuthenticationError("Apple identity has no subject")
        try:
            self._state.consume_nonce(nonce_id)
        except StateStoreError as exc:
            raise AuthenticationError("Sign-in nonce could not be consumed") from exc
        return VerifiedAppleIdentity(subject=subject)

    def stable_user_id(self, apple_subject: str) -> str:
        digest = hmac.new(
            self._user_id_secret_v1.encode("utf-8"),
            ("v1:apple:" + apple_subject).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return "pinpoint_" + token[:32]

    def local_user_id(self) -> str:
        """Return the one stable, private identity for a self-hosted install."""
        digest = hmac.new(
            self._user_id_secret_v1.encode("utf-8"),
            b"v1:local:primary",
            hashlib.sha256,
        ).digest()
        token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return "pinpoint_" + token[:32]

    def local_activation_verifier(self, code: str) -> str:
        return local_activation_verifier(self._user_id_secret_v1, code)

    def local_membership_verifier(self) -> str:
        return local_membership_verifier(self._user_id_secret_v1)

    def capture_apple_refresh_token(
        self,
        *,
        user_id: str,
        apple_subject: str,
        authorization_code: str | None,
    ) -> None:
        """Exchange and encrypt Apple's revocation credential when custody is enabled.

        The authorization code and refresh token are deliberately never returned,
        persisted in plaintext, or included in an exception message.
        """
        if not self._apple_token_custody_enabled:
            return
        if (
            not authorization_code
            or not authorization_code.strip()
            or len(authorization_code) > 4_096
        ):
            raise AuthenticationError("Apple authorization code is required")
        private_key = self._apple_private_key
        cipher = self._apple_refresh_token_cipher
        if private_key is None or cipher is None:
            raise AuthenticationError("Apple token custody is unavailable")

        now = int(self._now())
        client_secret = jwt.encode(
            {
                "iss": self._apple_team_id,
                "iat": now,
                "exp": now + 5 * 60,
                "aud": APPLE_ISSUER,
                "sub": self._audience,
            },
            private_key,
            algorithm="ES256",
            headers={"kid": self._apple_key_id},
        )
        try:
            response = self._apple_token_exchange(
                {
                    "client_id": self._audience,
                    "client_secret": client_secret,
                    "code": authorization_code,
                    "grant_type": "authorization_code",
                }
            )
        except Exception as exc:
            raise AuthenticationError("Apple authorization code exchange failed") from exc
        if not isinstance(response, dict) or response.get("error"):
            raise AuthenticationError("Apple authorization code exchange failed")

        exchanged_identity_token = response.get("id_token")
        if not isinstance(exchanged_identity_token, str) or not exchanged_identity_token:
            raise AuthenticationError("Apple authorization code exchange was incomplete")
        self._verify_exchanged_apple_identity(
            exchanged_identity_token,
            expected_subject=apple_subject,
        )

        refresh_token = response.get("refresh_token")
        if isinstance(refresh_token, str) and 0 < len(refresh_token) <= 16_384:
            nonce = secrets.token_bytes(12)
            associated_data = self._apple_token_associated_data(user_id)
            ciphertext = cipher.encrypt(
                nonce,
                refresh_token.encode("utf-8"),
                associated_data,
            )
            encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
            try:
                self._state.upsert_apple_refresh_token(
                    user_id=user_id,
                    encrypted_refresh_token="v1." + encoded,
                )
            except StateStoreError as exc:
                raise AuthenticationError("Apple refresh token could not be secured") from exc
            return
        if refresh_token is not None:
            raise AuthenticationError("Apple authorization code exchange was incomplete")
        if not self._state.has_apple_refresh_token(user_id):
            raise AuthenticationError("Apple did not provide a revocation credential")

    def apple_refresh_token_for_user(self, user_id: str) -> str | None:
        """Decrypt a held refresh token for a future server-side Apple revoke call.

        No API route currently exposes this method. It exists only so future
        deletion work does not need to weaken the at-rest custody boundary.
        """
        if not self._apple_token_custody_enabled:
            return None
        encrypted = self._state.apple_refresh_token_ciphertext(user_id)
        if encrypted is None:
            return None
        if not encrypted.startswith("v1.") or self._apple_refresh_token_cipher is None:
            raise AuthenticationError("Apple refresh token custody record is invalid")
        try:
            packed = _urlsafe_base64_decode(encrypted[3:])
            nonce, ciphertext = packed[:12], packed[12:]
            if len(nonce) != 12 or len(ciphertext) < 17:
                raise ValueError("invalid encrypted refresh token")
            plaintext = self._apple_refresh_token_cipher.decrypt(
                nonce,
                ciphertext,
                self._apple_token_associated_data(user_id),
            )
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise AuthenticationError("Apple refresh token custody record is invalid") from exc

    def authorize_beta_user(self, user_id: str, invite_code: str | None) -> bool:
        return self.beta_authorization_status(
            user_id,
            invite_code,
        ) is BetaAuthorizationStatus.AUTHORIZED

    def beta_authorization_status(
        self,
        user_id: str,
        invite_code: str | None,
    ) -> BetaAuthorizationStatus:
        digest = self._invite_digest(invite_code) if invite_code and invite_code.strip() else None
        managed_verifier = (
            managed_invite_verifier(self._user_id_secret_v1, invite_code)
            if invite_code and invite_code.strip()
            else None
        )
        return self._state.beta_authorization_status(
            user_id=user_id,
            invite_digest=digest,
            allowed_invite_digests=self._allowed_invite_digests,
            managed_invite_verifier=managed_verifier,
        )

    def issue_session(self, user_id: str) -> tuple[str, datetime]:
        now_timestamp = int(self._now())
        expires_timestamp = now_timestamp + self._session_ttl
        absolute_expires = now_timestamp + self._session_absolute_ttl
        jti = secrets.token_urlsafe(24)
        family_id = secrets.token_urlsafe(24)
        try:
            self._state.create_session(
                jti=jti,
                family_id=family_id,
                user_id=user_id,
                expires_at=expires_timestamp,
                absolute_expires_at=absolute_expires,
            )
        except TesterMembershipUnavailable as exc:
            raise AuthenticationError("PinPoint access is disabled") from exc
        expires = datetime.fromtimestamp(expires_timestamp, timezone.utc)
        token = jwt.encode(
            {
                "iss": "pinpoint",
                "aud": "pinpoint",
                "typ": "pinpoint_session",
                "sub": user_id,
                "jti": jti,
                "sid": family_id,
                "iat": now_timestamp,
                "exp": expires_timestamp,
                "aexp": absolute_expires,
            },
            self._secret,
            algorithm="HS256",
        )
        return token, expires

    def refresh_session(self, token: str) -> tuple[str, datetime]:
        claims = self._decode_session(token, require_active=True)
        now_timestamp = int(self._now())
        absolute_expires = int(claims["aexp"])
        expires_timestamp = min(now_timestamp + self._session_ttl, absolute_expires)
        if expires_timestamp <= now_timestamp + 60:
            raise AuthenticationError("PinPoint session reached its maximum lifetime")
        try:
            self._state.refresh_session(
                jti=str(claims["jti"]),
                family_id=str(claims["sid"]),
                user_id=str(claims["sub"]),
                expires_at=expires_timestamp,
                absolute_expires_at=absolute_expires,
            )
        except SessionUnavailable as exc:
            raise AuthenticationError("PinPoint session can no longer be refreshed") from exc
        refreshed = jwt.encode(
            {
                "iss": "pinpoint",
                "aud": "pinpoint",
                "typ": "pinpoint_session",
                "sub": str(claims["sub"]),
                "jti": str(claims["jti"]),
                "sid": str(claims["sid"]),
                "iat": now_timestamp,
                "exp": expires_timestamp,
                "aexp": absolute_expires,
            },
            self._secret,
            algorithm="HS256",
        )
        return refreshed, datetime.fromtimestamp(expires_timestamp, timezone.utc)

    def verify_session(self, token: str) -> str:
        return str(self._decode_session(token, require_active=True)["sub"])

    def revoke_session(self, token: str) -> None:
        claims = self._decode_session(token, require_active=True)
        try:
            self._state.revoke_session_family(
                jti=str(claims["jti"]),
                family_id=str(claims["sid"]),
                user_id=str(claims["sub"]),
            )
        except SessionUnavailable as exc:
            raise AuthenticationError("PinPoint session could not be revoked") from exc

    def _decode_session(self, token: str, *, require_active: bool) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience="pinpoint",
                issuer="pinpoint",
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub", "typ", "jti", "sid", "aexp"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except Exception as exc:
            raise AuthenticationError("PinPoint session is invalid or expired") from exc
        if claims.get("typ") != "pinpoint_session":
            raise AuthenticationError("PinPoint token has the wrong type")
        user_id = claims.get("sub")
        if not isinstance(user_id, str) or not user_id.startswith("pinpoint_"):
            raise AuthenticationError("PinPoint session has an invalid user")
        try:
            expires_at = int(claims["exp"])
            issued_at = int(claims["iat"])
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("PinPoint session has invalid timestamps") from exc
        now = int(self._now())
        if expires_at <= now or issued_at > now + 60:
            raise AuthenticationError("PinPoint session is invalid or expired")
        jti = claims.get("jti")
        family_id = claims.get("sid")
        absolute_expiry = claims.get("aexp")
        if (
            not isinstance(jti, str)
            or not jti
            or not isinstance(family_id, str)
            or not family_id
            or not isinstance(absolute_expiry, int)
            or absolute_expiry < expires_at
        ):
            raise AuthenticationError("PinPoint session has invalid lifecycle claims")
        if require_active and not self._state.session_is_active(
            jti=jti,
            family_id=family_id,
            user_id=user_id,
        ):
            raise AuthenticationError("PinPoint session is no longer active")
        return claims

    def _invite_digest(self, code: str) -> str:
        return hmac.new(
            self._user_id_secret_v1.encode("utf-8"),
            ("v1:invite:" + code.strip()).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _decode_nonce(self, raw_nonce: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                raw_nonce,
                self._secret,
                algorithms=["HS256"],
                issuer="pinpoint",
                options={"require": ["exp", "iat", "iss", "jti", "typ"]},
            )
        except Exception as exc:
            raise AuthenticationError("Sign-in nonce is invalid or expired") from exc
        if claims.get("typ") != "pinpoint_signin_nonce":
            raise AuthenticationError("Sign-in nonce has the wrong type")
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise AuthenticationError("Sign-in nonce has no identifier")
        return claims

    def _verify_exchanged_apple_identity(
        self,
        identity_token: str,
        *,
        expected_subject: str,
    ) -> None:
        try:
            key = self._jwks.get_signing_key_from_jwt(identity_token)
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=APPLE_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            raise AuthenticationError("Apple token exchange identity could not be verified") from exc
        actual_subject = claims.get("sub")
        if not isinstance(actual_subject, str) or not hmac.compare_digest(
            actual_subject,
            expected_subject,
        ):
            raise AuthenticationError("Apple token exchange identity did not match")

    @staticmethod
    def _apple_token_associated_data(user_id: str) -> bytes:
        return ("pinpoint:apple-refresh-token:v1:" + user_id).encode("utf-8")

    @staticmethod
    def _exchange_apple_token(form: dict[str, str]) -> dict[str, Any]:
        body = urlparse.urlencode(form).encode("ascii")
        request = urlrequest.Request(
            APPLE_TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlrequest.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise AuthenticationError("Apple authorization code exchange failed")
                raw = response.read(APPLE_TOKEN_RESPONSE_LIMIT + 1)
        except (OSError, urlerror.URLError, urlerror.HTTPError) as exc:
            raise AuthenticationError("Apple authorization code exchange failed") from exc
        if len(raw) > APPLE_TOKEN_RESPONSE_LIMIT:
            raise AuthenticationError("Apple authorization code exchange failed")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthenticationError("Apple authorization code exchange failed") from exc
        if not isinstance(payload, dict):
            raise AuthenticationError("Apple authorization code exchange failed")
        return payload


def managed_invite_verifier(secret: str, code: str) -> str:
    """Derive the verifier stored for a managed one-time invitation."""
    normalized = code.strip()
    if len(secret.encode("utf-8")) < 32 or not normalized:
        raise ValueError("Managed invitation verifier input is invalid")
    return hmac.new(
        secret.encode("utf-8"),
        ("v2:managed-invite:" + normalized).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def local_activation_verifier(secret: str, code: str) -> str:
    """Derive the private verifier stored for a local one-time activation."""
    normalized = code.strip()
    if len(secret.encode("utf-8")) < 32 or not normalized:
        raise ValueError("Local activation verifier input is invalid")
    return hmac.new(
        secret.encode("utf-8"),
        ("v1:local-activation:" + normalized).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def local_membership_verifier(secret: str) -> str:
    """Derive a stable ledger key that never contains the activation secret."""
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("Local membership verifier input is invalid")
    return hmac.new(
        secret.encode("utf-8"),
        b"v1:local:membership",
        hashlib.sha256,
    ).hexdigest()


def _urlsafe_base64_decode(value: str) -> bytes:
    normalized = value.strip()
    if not normalized:
        raise ValueError("empty URL-safe base64")
    return base64.b64decode(
        normalized + "=" * (-len(normalized) % 4),
        altchars=b"-_",
        validate=True,
    )
