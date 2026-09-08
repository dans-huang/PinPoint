from __future__ import annotations

import base64
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(RuntimeError):
    pass


AuthenticationMode = Literal["hosted", "self_hosted"]


def authentication_mode_from_environment() -> AuthenticationMode:
    """Load the deployment's mutually exclusive authentication surface.

    Hosted mode is the fail-closed default so an existing internet-facing
    deployment never acquires a loopback activation endpoint merely by
    upgrading PinPoint.
    """
    value = os.environ.get("PINPOINT_AUTH_MODE", "hosted").strip().lower()
    if value not in {"hosted", "self_hosted"}:
        raise ConfigurationError("PINPOINT_AUTH_MODE must be hosted or self_hosted")
    return value  # type: ignore[return-value]


def public_link_configuration_from_environment() -> tuple[str | None, str | None]:
    """Load the public invitation origin without requiring service secrets.

    The operator CLI uses the same parser as the web service so it cannot emit
    an HTTPS invitation that the deployed AASA endpoint will not claim.
    """
    invite_base_url = os.environ.get("PINPOINT_INVITE_BASE_URL", "").strip() or None
    apple_app_id = os.environ.get("PINPOINT_APPLE_APP_ID", "").strip() or None
    if bool(invite_base_url) != bool(apple_app_id):
        raise ConfigurationError(
            "PINPOINT_INVITE_BASE_URL and PINPOINT_APPLE_APP_ID must be configured together"
        )
    if invite_base_url is None:
        return None, None

    parts = urlsplit(invite_base_url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError(
            "PINPOINT_INVITE_BASE_URL must use a valid HTTPS port"
        ) from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.netloc != parts.netloc.lower()
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.path != "/invite"
        or parts.query
        or parts.fragment
    ):
        raise ConfigurationError(
            "PINPOINT_INVITE_BASE_URL must be a lowercase HTTPS URL ending exactly in /invite"
        )
    if not re.fullmatch(r"[A-Z0-9]{10}\.[A-Za-z0-9][A-Za-z0-9.-]{2,254}", apple_app_id):
        raise ConfigurationError(
            "PINPOINT_APPLE_APP_ID must be the Apple Team ID and bundle id"
        )
    canonical_base_url = urlunsplit(("https", parts.netloc, "/invite", "", ""))
    return canonical_base_url, apple_app_id


@dataclass(frozen=True)
class Settings:
    session_secret: str
    user_id_secret_v1: str
    apple_audience: str
    plaud_client_id: str
    plaud_client_secret: str
    plaud_api_key: str
    auth_mode: AuthenticationMode = "hosted"
    invite_base_url: str | None = None
    apple_app_id: str | None = None
    apple_token_custody_enabled: bool = False
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key_path: str = ""
    apple_refresh_token_key_v1: str = ""
    beta_invite_codes: tuple[str, ...] = ()
    plaud_api_domain: str = "platform-us.plaud.ai"
    plaud_user_token_ttl_seconds: int = 60 * 60
    state_db_path: str = str(Path(__file__).resolve().parents[1] / ".data" / "pinpoint.sqlite3")
    audio_host_suffixes: tuple[str, ...] = ("amazonaws.com", "plaud.ai")
    max_audio_bytes: int = 256 * 1024 * 1024
    user_daily_recording_limit: int = 20
    global_daily_recording_limit: int = 200
    user_active_recording_limit: int = 3
    user_daily_audio_bytes_limit: int = 1024 * 1024 * 1024
    global_daily_audio_bytes_limit: int = 10 * 1024 * 1024 * 1024
    upload_attempt_lease_seconds: int = 10 * 60
    session_ttl_seconds: int = 24 * 60 * 60
    session_absolute_ttl_seconds: int = 30 * 24 * 60 * 60
    nonce_ttl_seconds: int = 5 * 60
    max_request_body_bytes: int = 4 * 1024 * 1024
    global_request_limit: int = 600
    global_request_window_seconds: int = 60
    nonce_ip_limit: int = 30
    nonce_ip_window_seconds: int = 60
    apple_signin_ip_limit: int = 10
    apple_signin_ip_window_seconds: int = 5 * 60
    intelligence_base_url: str | None = None
    intelligence_api_key: str | None = None
    intelligence_model: str | None = None
    intelligence_timeout_seconds: int = 60
    intelligence_max_transcript_chars: int = 200_000
    intelligence_max_output_chars: int = 20_000
    intelligence_user_active_generation_limit: int = 2
    intelligence_user_daily_generation_limit: int = 40

    @classmethod
    def from_environment(cls) -> "Settings":
        auth_mode = authentication_mode_from_environment()
        invite_base_url, apple_app_id = public_link_configuration_from_environment()
        settings = cls(
            session_secret=os.environ.get("PINPOINT_SESSION_SECRET", ""),
            user_id_secret_v1=os.environ.get("PINPOINT_USER_ID_SECRET_V1", ""),
            apple_audience=os.environ.get("PINPOINT_APPLE_AUDIENCE", ""),
            plaud_client_id=os.environ.get("PLAUD_CLIENT_ID", ""),
            plaud_client_secret=os.environ.get("PLAUD_CLIENT_SECRET", ""),
            plaud_api_key=os.environ.get("PLAUD_API_KEY", ""),
            auth_mode=auth_mode,
            invite_base_url=invite_base_url,
            apple_app_id=apple_app_id,
            apple_token_custody_enabled=_environment_bool(
                "PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED",
                os.environ.get("PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED", "false"),
            ),
            apple_team_id=os.environ.get("PINPOINT_APPLE_TEAM_ID", ""),
            apple_key_id=os.environ.get("PINPOINT_APPLE_KEY_ID", ""),
            apple_private_key_path=os.environ.get("PINPOINT_APPLE_PRIVATE_KEY_PATH", ""),
            apple_refresh_token_key_v1=os.environ.get(
                "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1", ""
            ),
            beta_invite_codes=tuple(
                part.strip()
                for part in os.environ.get("PINPOINT_INVITE_CODES", "").split(",")
                if part.strip()
            ),
            plaud_api_domain=os.environ.get("PLAUD_API_DOMAIN", "platform-us.plaud.ai"),
            plaud_user_token_ttl_seconds=int(
                os.environ.get("PLAUD_USER_TOKEN_TTL_SECONDS", 60 * 60)
            ),
            state_db_path=os.environ.get(
                "PINPOINT_STATE_DB_PATH",
                str(Path(__file__).resolve().parents[1] / ".data" / "pinpoint.sqlite3"),
            ),
            audio_host_suffixes=tuple(
                part.strip().lower()
                for part in os.environ.get(
                    "PINPOINT_AUDIO_HOST_SUFFIXES", "amazonaws.com,plaud.ai"
                ).split(",")
                if part.strip()
            ),
            max_audio_bytes=int(os.environ.get("PINPOINT_MAX_AUDIO_BYTES", 256 * 1024 * 1024)),
            user_daily_recording_limit=int(os.environ.get("PINPOINT_USER_DAILY_RECORDING_LIMIT", 20)),
            global_daily_recording_limit=int(os.environ.get("PINPOINT_GLOBAL_DAILY_RECORDING_LIMIT", 200)),
            user_active_recording_limit=int(os.environ.get("PINPOINT_USER_ACTIVE_RECORDING_LIMIT", 3)),
            user_daily_audio_bytes_limit=int(
                os.environ.get("PINPOINT_USER_DAILY_AUDIO_BYTES_LIMIT", 1024 * 1024 * 1024)
            ),
            global_daily_audio_bytes_limit=int(
                os.environ.get("PINPOINT_GLOBAL_DAILY_AUDIO_BYTES_LIMIT", 10 * 1024 * 1024 * 1024)
            ),
            upload_attempt_lease_seconds=int(
                os.environ.get("PINPOINT_UPLOAD_ATTEMPT_LEASE_SECONDS", 10 * 60)
            ),
            session_ttl_seconds=int(os.environ.get("PINPOINT_SESSION_TTL_SECONDS", 24 * 60 * 60)),
            session_absolute_ttl_seconds=int(
                os.environ.get("PINPOINT_SESSION_ABSOLUTE_TTL_SECONDS", 30 * 24 * 60 * 60)
            ),
            nonce_ttl_seconds=int(os.environ.get("PINPOINT_NONCE_TTL_SECONDS", 5 * 60)),
            max_request_body_bytes=int(
                os.environ.get("PINPOINT_MAX_REQUEST_BODY_BYTES", 4 * 1024 * 1024)
            ),
            global_request_limit=int(os.environ.get("PINPOINT_GLOBAL_REQUEST_LIMIT", 600)),
            global_request_window_seconds=int(
                os.environ.get("PINPOINT_GLOBAL_REQUEST_WINDOW_SECONDS", 60)
            ),
            nonce_ip_limit=int(os.environ.get("PINPOINT_NONCE_IP_LIMIT", 30)),
            nonce_ip_window_seconds=int(
                os.environ.get("PINPOINT_NONCE_IP_WINDOW_SECONDS", 60)
            ),
            apple_signin_ip_limit=int(os.environ.get("PINPOINT_APPLE_SIGNIN_IP_LIMIT", 10)),
            apple_signin_ip_window_seconds=int(
                os.environ.get("PINPOINT_APPLE_SIGNIN_IP_WINDOW_SECONDS", 5 * 60)
            ),
            intelligence_base_url=os.environ.get(
                "PINPOINT_INTELLIGENCE_BASE_URL", ""
            ).strip() or None,
            intelligence_api_key=os.environ.get(
                "PINPOINT_INTELLIGENCE_API_KEY", ""
            ).strip() or None,
            intelligence_model=os.environ.get(
                "PINPOINT_INTELLIGENCE_MODEL", ""
            ).strip() or None,
            intelligence_timeout_seconds=int(
                os.environ.get("PINPOINT_INTELLIGENCE_TIMEOUT_SECONDS", 60)
            ),
            intelligence_max_transcript_chars=int(
                os.environ.get("PINPOINT_INTELLIGENCE_MAX_TRANSCRIPT_CHARS", 200_000)
            ),
            intelligence_max_output_chars=int(
                os.environ.get("PINPOINT_INTELLIGENCE_MAX_OUTPUT_CHARS", 20_000)
            ),
            intelligence_user_active_generation_limit=int(
                os.environ.get("PINPOINT_INTELLIGENCE_USER_ACTIVE_GENERATION_LIMIT", 2)
            ),
            intelligence_user_daily_generation_limit=int(
                os.environ.get("PINPOINT_INTELLIGENCE_USER_DAILY_GENERATION_LIMIT", 40)
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("PINPOINT_SESSION_SECRET", self.session_secret),
                ("PINPOINT_USER_ID_SECRET_V1", self.user_id_secret_v1),
                ("PLAUD_CLIENT_ID", self.plaud_client_id),
                ("PLAUD_CLIENT_SECRET", self.plaud_client_secret),
                ("PLAUD_API_KEY", self.plaud_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError("Missing required settings: " + ", ".join(missing))
        if self.auth_mode not in {"hosted", "self_hosted"}:
            raise ConfigurationError("PINPOINT_AUTH_MODE must be hosted or self_hosted")
        if self.auth_mode == "hosted" and not self.apple_audience:
            raise ConfigurationError("Missing required settings: PINPOINT_APPLE_AUDIENCE")
        if self.auth_mode == "self_hosted":
            hosted_only = [
                name
                for name, configured in (
                    ("PINPOINT_INVITE_BASE_URL", self.invite_base_url is not None),
                    ("PINPOINT_APPLE_APP_ID", self.apple_app_id is not None),
                    ("PINPOINT_INVITE_CODES", bool(self.beta_invite_codes)),
                    (
                        "PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED",
                        self.apple_token_custody_enabled,
                    ),
                )
                if configured
            ]
            if hosted_only:
                raise ConfigurationError(
                    "Hosted authentication settings cannot be used in self_hosted mode: "
                    + ", ".join(hosted_only)
                )
        if len(set(self.beta_invite_codes)) != len(self.beta_invite_codes) or any(
            len(code) < 16 for code in self.beta_invite_codes
        ):
            raise ConfigurationError(
                "Legacy invitation codes must be unique and at least 16 characters"
            )
        if len(self.session_secret.encode("utf-8")) < 32:
            raise ConfigurationError("PINPOINT_SESSION_SECRET must be at least 32 bytes")
        if len(self.user_id_secret_v1.encode("utf-8")) < 32:
            raise ConfigurationError("PINPOINT_USER_ID_SECRET_V1 must be at least 32 bytes")
        if hmac_compare(self.session_secret, self.user_id_secret_v1):
            raise ConfigurationError("PINPOINT_USER_ID_SECRET_V1 must differ from PINPOINT_SESSION_SECRET")
        if self.apple_app_id is not None and not self.apple_app_id.endswith(
            "." + self.apple_audience
        ):
            raise ConfigurationError(
                "PINPOINT_APPLE_APP_ID bundle id must match PINPOINT_APPLE_AUDIENCE"
            )
        if self.apple_token_custody_enabled:
            self._validate_apple_token_custody()
        if (
            self.plaud_api_domain != self.plaud_api_domain.lower()
            or "://" in self.plaud_api_domain
            or "/" in self.plaud_api_domain
            or not self.plaud_api_domain.endswith(".plaud.ai")
        ):
            raise ConfigurationError("PLAUD_API_DOMAIN must be a Plaud host name")
        if not 15 * 60 <= self.plaud_user_token_ttl_seconds <= 24 * 60 * 60:
            raise ConfigurationError(
                "PLAUD_USER_TOKEN_TTL_SECONDS must be between 15 minutes and 24 hours"
            )
        if not 60 <= self.nonce_ttl_seconds <= 600:
            raise ConfigurationError("PINPOINT_NONCE_TTL_SECONDS must be between 60 and 600")
        if not self.state_db_path.strip():
            raise ConfigurationError("PINPOINT_STATE_DB_PATH must not be empty")
        if not self.audio_host_suffixes:
            raise ConfigurationError("PINPOINT_AUDIO_HOST_SUFFIXES must contain at least one host suffix")
        for suffix in self.audio_host_suffixes:
            if (
                suffix.startswith(".")
                or "://" in suffix
                or "/" in suffix
                or suffix in {"localhost", "local"}
                or "." not in suffix
            ):
                raise ConfigurationError("PINPOINT_AUDIO_HOST_SUFFIXES contains an invalid host suffix")
        if not 1_000_000 <= self.max_audio_bytes <= 2 * 1024 * 1024 * 1024:
            raise ConfigurationError("PINPOINT_MAX_AUDIO_BYTES must be between 1 MB and 2 GB")
        if not 1 <= self.user_daily_recording_limit <= 1_000:
            raise ConfigurationError("PINPOINT_USER_DAILY_RECORDING_LIMIT must be between 1 and 1000")
        if not self.user_daily_recording_limit <= self.global_daily_recording_limit <= 100_000:
            raise ConfigurationError("PINPOINT_GLOBAL_DAILY_RECORDING_LIMIT must cover the per-user limit")
        if not 1 <= self.user_active_recording_limit <= 20:
            raise ConfigurationError("PINPOINT_USER_ACTIVE_RECORDING_LIMIT must be between 1 and 20")
        if not self.max_audio_bytes <= self.user_daily_audio_bytes_limit:
            raise ConfigurationError("PINPOINT_USER_DAILY_AUDIO_BYTES_LIMIT must cover one recording")
        if not self.user_daily_audio_bytes_limit <= self.global_daily_audio_bytes_limit:
            raise ConfigurationError("PINPOINT_GLOBAL_DAILY_AUDIO_BYTES_LIMIT must cover the per-user limit")
        if not 5 * 60 <= self.upload_attempt_lease_seconds <= 60 * 60:
            raise ConfigurationError(
                "PINPOINT_UPLOAD_ATTEMPT_LEASE_SECONDS must be between 5 and 60 minutes"
            )
        if not 15 * 60 <= self.session_ttl_seconds <= 7 * 24 * 60 * 60:
            raise ConfigurationError("PINPOINT_SESSION_TTL_SECONDS must be between 15 minutes and 7 days")
        if not self.session_ttl_seconds <= self.session_absolute_ttl_seconds <= 90 * 24 * 60 * 60:
            raise ConfigurationError("PINPOINT_SESSION_ABSOLUTE_TTL_SECONDS must cover the session TTL and be at most 90 days")
        if not 16 * 1024 <= self.max_request_body_bytes <= 16 * 1024 * 1024:
            raise ConfigurationError(
                "PINPOINT_MAX_REQUEST_BODY_BYTES must be between 16 KB and 16 MB"
            )
        for name, value in (
            ("PINPOINT_GLOBAL_REQUEST_LIMIT", self.global_request_limit),
            ("PINPOINT_NONCE_IP_LIMIT", self.nonce_ip_limit),
            ("PINPOINT_APPLE_SIGNIN_IP_LIMIT", self.apple_signin_ip_limit),
        ):
            if not 1 <= value <= 1_000_000:
                raise ConfigurationError(f"{name} must be between 1 and 1000000")
        for name, value in (
            ("PINPOINT_GLOBAL_REQUEST_WINDOW_SECONDS", self.global_request_window_seconds),
            ("PINPOINT_NONCE_IP_WINDOW_SECONDS", self.nonce_ip_window_seconds),
            ("PINPOINT_APPLE_SIGNIN_IP_WINDOW_SECONDS", self.apple_signin_ip_window_seconds),
        ):
            if not 1 <= value <= 60 * 60:
                raise ConfigurationError(f"{name} must be between 1 and 3600 seconds")
        intelligence_values = (
            self.intelligence_base_url,
            self.intelligence_api_key,
            self.intelligence_model,
        )
        if any(intelligence_values) and not all(intelligence_values):
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_BASE_URL, PINPOINT_INTELLIGENCE_API_KEY, and "
                "PINPOINT_INTELLIGENCE_MODEL must be configured together"
            )
        if self.intelligence_base_url is not None:
            parts = urlsplit(self.intelligence_base_url)
            try:
                port = parts.port
            except ValueError as exc:
                raise ConfigurationError(
                    "PINPOINT_INTELLIGENCE_BASE_URL must be a valid HTTPS URL"
                ) from exc
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or port not in (None, 443)
                or parts.query
                or parts.fragment
            ):
                raise ConfigurationError(
                    "PINPOINT_INTELLIGENCE_BASE_URL must be a valid HTTPS URL"
                )
            if len(self.intelligence_api_key or "") < 16:
                raise ConfigurationError(
                    "PINPOINT_INTELLIGENCE_API_KEY must be at least 16 characters"
                )
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", self.intelligence_model or ""):
                raise ConfigurationError("PINPOINT_INTELLIGENCE_MODEL is invalid")
        if not 5 <= self.intelligence_timeout_seconds <= 120:
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_TIMEOUT_SECONDS must be between 5 and 120"
            )
        if not 10_000 <= self.intelligence_max_transcript_chars <= 500_000:
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_MAX_TRANSCRIPT_CHARS must be between 10000 and 500000"
            )
        if not 1_000 <= self.intelligence_max_output_chars <= 30_000:
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_MAX_OUTPUT_CHARS must be between 1000 and 30000"
            )
        if not 1 <= self.intelligence_user_active_generation_limit <= 20:
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_USER_ACTIVE_GENERATION_LIMIT must be between 1 and 20"
            )
        if not 1 <= self.intelligence_user_daily_generation_limit <= 1_000:
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_USER_DAILY_GENERATION_LIMIT must be between 1 and 1000"
            )
        if (
            self.intelligence_user_active_generation_limit
            > self.intelligence_user_daily_generation_limit
        ):
            raise ConfigurationError(
                "PINPOINT_INTELLIGENCE_USER_DAILY_GENERATION_LIMIT must cover the active limit"
            )

    def _validate_apple_token_custody(self) -> None:
        missing = [
            name
            for name, value in (
                ("PINPOINT_APPLE_TEAM_ID", self.apple_team_id),
                ("PINPOINT_APPLE_KEY_ID", self.apple_key_id),
                ("PINPOINT_APPLE_PRIVATE_KEY_PATH", self.apple_private_key_path),
                ("PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1", self.apple_refresh_token_key_v1),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Apple token custody is enabled but settings are missing: "
                + ", ".join(missing)
            )
        if not re.fullmatch(r"[A-Z0-9]{10}", self.apple_team_id):
            raise ConfigurationError(
                "PINPOINT_APPLE_TEAM_ID must be a 10-character Apple Team ID"
            )
        if not re.fullmatch(r"[A-Z0-9]{10}", self.apple_key_id):
            raise ConfigurationError(
                "PINPOINT_APPLE_KEY_ID must be a 10-character Apple key ID"
            )
        private_key_path = Path(self.apple_private_key_path).expanduser()
        if not private_key_path.is_absolute():
            raise ConfigurationError("PINPOINT_APPLE_PRIVATE_KEY_PATH must be absolute")
        try:
            key_stat = private_key_path.stat()
        except OSError as exc:
            raise ConfigurationError(
                "PINPOINT_APPLE_PRIVATE_KEY_PATH is not readable"
            ) from exc
        if not stat.S_ISREG(key_stat.st_mode):
            raise ConfigurationError(
                "PINPOINT_APPLE_PRIVATE_KEY_PATH must be a regular file"
            )
        if stat.S_IMODE(key_stat.st_mode) & 0o077:
            raise ConfigurationError(
                "PINPOINT_APPLE_PRIVATE_KEY_PATH must not be accessible to group or other users"
            )
        try:
            encryption_key = _decode_urlsafe_base64(self.apple_refresh_token_key_v1)
        except ValueError as exc:
            raise ConfigurationError(
                "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1 must be URL-safe base64"
            ) from exc
        if len(encryption_key) != 32:
            raise ConfigurationError(
                "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1 must decode to exactly 32 bytes"
            )
        if hmac_compare(self.apple_refresh_token_key_v1, self.session_secret) or hmac_compare(
            self.apple_refresh_token_key_v1, self.user_id_secret_v1
        ):
            raise ConfigurationError(
                "PINPOINT_APPLE_REFRESH_TOKEN_KEY_V1 must be a separate secret"
            )


def hmac_compare(left: str, right: str) -> bool:
    # Avoid accidentally coupling these two independently rotated secrets.
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _environment_bool(name: str, raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _decode_urlsafe_base64(value: str) -> bytes:
    normalized = value.strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", normalized):
        raise ValueError("invalid URL-safe base64")
    padded = normalized + "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid URL-safe base64") from exc
