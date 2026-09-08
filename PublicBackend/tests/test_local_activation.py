from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_backend.admin import main as admin_main
from pinpoint_backend.app import (
    LocalSessionRequest,
    Runtime,
    create_apple_session,
    create_local_session,
    create_nonce,
    get_apple_app_site_association,
    get_invitation_landing,
)
from pinpoint_backend.config import ConfigurationError, Settings
from pinpoint_backend.plaud import PlaudServiceError, PlaudUserToken
from pinpoint_backend.security import SecurityService, local_activation_verifier
from pinpoint_backend.state import StateStore


SESSION_SECRET = "s" * 48
USER_ID_SECRET = "u" * 48
ACTIVATION_CODE = "ppl_" + "a" * 43


def settings_environment(*, mode: str | None, apple_audience: str = "") -> dict[str, str]:
    environment = {
        "PINPOINT_SESSION_SECRET": SESSION_SECRET,
        "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
        "PLAUD_CLIENT_ID": "client",
        "PLAUD_CLIENT_SECRET": "secret",
        "PLAUD_API_KEY": "api",
    }
    if mode is not None:
        environment["PINPOINT_AUTH_MODE"] = mode
    if apple_audience:
        environment["PINPOINT_APPLE_AUDIENCE"] = apple_audience
    return environment


def local_request(
    *,
    client_host: str = "127.0.0.1",
    host: str = "127.0.0.1:8787",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/session/local",
            "raw_path": b"/v1/session/local",
            "query_string": b"",
            "headers": ((b"host", host.encode("ascii")),) + extra_headers,
            "client": (client_host, 51_234),
            "server": ("127.0.0.1", 8_787),
        }
    )


class AuthenticationModeConfigurationTests(unittest.TestCase):
    def test_omitted_mode_defaults_to_hosted_and_requires_apple(self):
        with patch.dict(
            os.environ,
            settings_environment(
                mode=None,
                apple_audience="com.example.pinpoint",
            ),
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.auth_mode, "hosted")

        with patch.dict(
            os.environ,
            settings_environment(mode=None),
            clear=True,
        ):
            with self.assertRaisesRegex(
                ConfigurationError,
                "PINPOINT_APPLE_AUDIENCE",
            ):
                Settings.from_environment()

    def test_self_hosted_mode_needs_no_apple_configuration(self):
        with patch.dict(
            os.environ,
            settings_environment(mode="self_hosted"),
            clear=True,
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.auth_mode, "self_hosted")
        self.assertEqual(settings.apple_audience, "")

    def test_invalid_mode_and_hosted_only_self_hosted_combinations_fail(self):
        invalid_environments = [
            settings_environment(mode="public"),
            {
                **settings_environment(mode="self_hosted"),
                "PINPOINT_INVITE_CODES": "invite-code-000000000001",
            },
            {
                **settings_environment(mode="self_hosted"),
                "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite",
                "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.pinpoint",
            },
            {
                **settings_environment(mode="self_hosted"),
                "PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED": "true",
            },
        ]
        for environment in invalid_environments:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ConfigurationError):
                        Settings.from_environment()


class FakePlaud:
    def __init__(self, *, now: float):
        self.now = now
        self.calls: list[str] = []
        self.failure: PlaudServiceError | None = None

    def issue_user_token(self, user_id: str) -> PlaudUserToken:
        self.calls.append(user_id)
        if self.failure is not None:
            raise self.failure
        return PlaudUserToken(
            access_token="plaud-user-access-token",
            expires_at=datetime.fromtimestamp(self.now + 3_600, timezone.utc),
        )


class LocalActivationTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1_900_000_000.0]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.temporary_directory.name) / "pinpoint.sqlite3"
        )
        self.state = StateStore(self.database_path, now=lambda: self.clock[0])
        self.security = SecurityService(
            session_secret=SESSION_SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="",
            session_ttl_seconds=3_600,
            session_absolute_ttl_seconds=30 * 24 * 60 * 60,
            nonce_ttl_seconds=300,
            state_store=self.state,
            now=lambda: self.clock[0],
        )
        self.settings = Settings(
            session_secret=SESSION_SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
            auth_mode="self_hosted",
            state_db_path=self.database_path,
        )
        self.plaud = FakePlaud(now=self.clock[0])
        self.active = Runtime(
            settings=self.settings,
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    @property
    def verifier(self) -> str:
        return self.security.local_activation_verifier(ACTIVATION_CODE)

    def create_activation(self, *, code: str = ACTIVATION_CODE) -> None:
        self.state.create_local_activation(
            activation_id="act_abcdefghijklmnop",
            activation_verifier=self.security.local_activation_verifier(code),
            expires_at=self.clock[0] + 600,
        )

    def activation_state(self) -> str:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return str(
                connection.execute(
                    "SELECT state FROM local_activation_codes"
                ).fetchone()[0]
            )

    def test_local_user_id_is_fixed_private_hmac_identity(self):
        expected_digest = hmac.new(
            USER_ID_SECRET.encode("utf-8"),
            b"v1:local:primary",
            hashlib.sha256,
        ).digest()
        self.assertEqual(self.security.local_user_id(), self.security.local_user_id())
        self.assertNotEqual(
            self.security.local_user_id(),
            self.security.stable_user_id("primary"),
        )
        self.assertEqual(len(expected_digest), 32)
        self.assertTrue(self.security.local_user_id().startswith("pinpoint_"))
        self.assertNotIn("primary", self.security.local_user_id())

    def test_one_code_creates_one_session_and_cannot_be_reused(self):
        self.create_activation()
        response = asyncio.run(
            create_local_session(
                LocalSessionRequest(activation_code=ACTIVATION_CODE),
                local_request(),
                active=self.active,
            )
        )
        self.assertEqual(response.deployment_mode, "self_hosted")
        self.assertEqual(response.user_id, self.security.local_user_id())
        self.assertEqual(
            self.security.verify_session(response.session_token),
            response.user_id,
        )
        self.assertEqual(self.activation_state(), "consumed")

        with self.assertRaises(HTTPException) as reused:
            asyncio.run(
                create_local_session(
                    LocalSessionRequest(activation_code=ACTIVATION_CODE),
                    local_request(),
                    active=self.active,
                )
            )
        self.assertEqual(reused.exception.status_code, 401)
        self.assertEqual(len(self.plaud.calls), 1)

    def test_expired_code_is_rejected_before_plaud(self):
        self.create_activation()
        self.clock[0] += 601
        with self.assertRaises(HTTPException) as expired:
            asyncio.run(
                create_local_session(
                    LocalSessionRequest(activation_code=ACTIVATION_CODE),
                    local_request(),
                    active=self.active,
                )
            )
        self.assertEqual(expired.exception.status_code, 401)
        self.assertEqual(self.plaud.calls, [])

    def test_concurrent_reservation_has_exactly_one_winner(self):
        self.create_activation()

        def reserve(index: int) -> bool:
            return StateStore(
                self.database_path,
                now=lambda: self.clock[0],
            ).reserve_local_activation(
                activation_verifier=self.verifier,
                reservation_id=f"res_concurrent_{index:012d}",
                user_id=self.security.local_user_id(),
                membership_verifier=self.security.local_membership_verifier(),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def test_plaud_failure_releases_code_for_one_safe_retry(self):
        self.create_activation()
        self.plaud.failure = PlaudServiceError("temporary failure")
        with self.assertRaises(HTTPException) as failed:
            asyncio.run(
                create_local_session(
                    LocalSessionRequest(activation_code=ACTIVATION_CODE),
                    local_request(),
                    active=self.active,
                )
            )
        self.assertEqual(failed.exception.status_code, 502)
        self.assertEqual(self.activation_state(), "available")

        self.plaud.failure = None
        response = asyncio.run(
            create_local_session(
                LocalSessionRequest(activation_code=ACTIVATION_CODE),
                local_request(),
                active=self.active,
            )
        )
        self.assertEqual(response.deployment_mode, "self_hosted")
        self.assertEqual(self.activation_state(), "consumed")

    def test_nonloopback_proxy_and_public_host_are_rejected_without_consuming(self):
        requests = (
            local_request(client_host="192.0.2.10"),
            local_request(host="pinpoint.example.com"),
            local_request(extra_headers=((b"forwarded", b"for=192.0.2.10"),)),
            local_request(
                extra_headers=((b"x-forwarded-for", b"127.0.0.1"),)
            ),
        )
        for index, request in enumerate(requests):
            code = "ppl_" + chr(ord("b") + index) * 43
            self.state.create_local_activation(
                activation_id=f"act_nonloopback_{index:012d}",
                activation_verifier=self.security.local_activation_verifier(code),
                expires_at=self.clock[0] + 600,
            )
            with self.subTest(index=index):
                with self.assertRaises(HTTPException) as rejected:
                    asyncio.run(
                        create_local_session(
                            LocalSessionRequest(activation_code=code),
                            request,
                            active=self.active,
                        )
                    )
                self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(self.plaud.calls, [])
        with closing(sqlite3.connect(self.database_path)) as connection:
            states = connection.execute(
                "SELECT state FROM local_activation_codes"
            ).fetchall()
        self.assertTrue(all(row[0] == "available" for row in states))

    def test_ipv4_ipv6_and_localhost_loopback_hosts_are_accepted(self):
        accepted = (
            local_request(host="localhost"),
            local_request(host="localhost:8787"),
            local_request(host="127.0.0.1"),
            local_request(client_host="::1", host="[::1]"),
            local_request(client_host="::ffff:127.0.0.1", host="[::1]:8787"),
        )
        for index, request in enumerate(accepted):
            code = "ppl_" + chr(ord("g") + index) * 43
            self.state.create_local_activation(
                activation_id=f"act_loopback_{index:012d}",
                activation_verifier=self.security.local_activation_verifier(code),
                expires_at=self.clock[0] + 600,
            )
            response = asyncio.run(
                create_local_session(
                    LocalSessionRequest(activation_code=code),
                    request,
                    active=self.active,
                )
            )
            self.assertEqual(response.deployment_mode, "self_hosted")

    def test_modes_expose_only_their_own_enrollment_routes(self):
        hosted = Runtime(
            settings=Settings(
                session_secret=SESSION_SECRET,
                user_id_secret_v1=USER_ID_SECRET,
                apple_audience="com.example.pinpoint",
                plaud_client_id="client",
                plaud_client_secret="secret",
                plaud_api_key="api",
                auth_mode="hosted",
                state_db_path=self.database_path,
            ),
            security=self.security,
            plaud=self.plaud,
            state=self.state,
        )
        with self.assertRaises(HTTPException) as local_hidden:
            asyncio.run(
                create_local_session(
                    LocalSessionRequest(activation_code=ACTIVATION_CODE),
                    local_request(),
                    active=hosted,
                )
            )
        self.assertEqual(local_hidden.exception.status_code, 404)

        hidden_hosted_calls = (
            lambda: create_nonce(active=self.active),
            lambda: get_apple_app_site_association(active=self.active),
            lambda: get_invitation_landing(ACTIVATION_CODE, active=self.active),
        )
        for call in hidden_hosted_calls:
            with self.assertRaises(HTTPException) as hidden:
                call()
            self.assertEqual(hidden.exception.status_code, 404)

        with self.assertRaises(HTTPException) as apple_hidden:
            asyncio.run(
                create_apple_session(
                    {
                        "identity_token": "i" * 20,
                        "nonce": "n" * 20,
                    },
                    active=self.active,
                )
            )
        self.assertEqual(apple_hidden.exception.status_code, 404)


class LocalActivationAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.temporary_directory.name) / "pinpoint.sqlite3"
        )
        StateStore(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_admin(self, mode: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "PINPOINT_STATE_DB_PATH": self.database_path,
                "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
                "PINPOINT_AUTH_MODE": mode,
            },
            clear=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            result = admin_main(["activation", "create"])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_admin_prints_secret_once_with_exact_ten_minute_expiry(self):
        before = time.time()
        result, stdout, stderr = self.run_admin("self_hosted")
        after = time.time()
        self.assertEqual(result, 0, stderr)
        record = json.loads(stdout)
        raw_code = record["activation_code"]
        self.assertTrue(raw_code.startswith("ppl_"))
        self.assertEqual(record["expires_in_seconds"], 600)
        self.assertGreaterEqual(record["expires_at"], before + 600)
        self.assertLessEqual(record["expires_at"], after + 600)

        with closing(sqlite3.connect(self.database_path)) as connection:
            stored = connection.execute(
                """
                SELECT activation_verifier, created_at, expires_at
                FROM local_activation_codes
                """
            ).fetchone()
        self.assertEqual(
            stored[0],
            local_activation_verifier(USER_ID_SECRET, raw_code),
        )
        self.assertAlmostEqual(stored[2] - stored[1], 600, delta=1)
        self.assertNotIn(raw_code, Path(self.database_path).read_bytes().decode(
            "latin-1",
            errors="ignore",
        ))

    def test_admin_refuses_local_code_in_hosted_mode(self):
        result, stdout, stderr = self.run_admin("hosted")
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("configuration", stderr.lower())
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM local_activation_codes"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
