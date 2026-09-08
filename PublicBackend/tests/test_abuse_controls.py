from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_backend.abuse import AbuseControlMiddleware
from pinpoint_backend.app import AppleSessionRequest, UploadCompleteRequest, app
from pinpoint_backend.config import Settings
from pinpoint_backend.state import StateStore, StateStoreError


SECRET = "s" * 48
USER_ID_SECRET = "u" * 48


class RequestModelLimitTests(unittest.TestCase):
    def test_apple_credentials_have_finite_character_limits(self):
        AppleSessionRequest(
            identity_token="i" * 16_384,
            authorization_code="c" * 4_096,
            nonce="n" * 4_096,
        )
        with self.assertRaises(ValidationError):
            AppleSessionRequest(identity_token="i" * 16_385, nonce="n" * 20)
        with self.assertRaises(ValidationError):
            AppleSessionRequest(identity_token="i" * 20, nonce="n" * 4_097)
        with self.assertRaises(ValidationError):
            AppleSessionRequest(
                identity_token="i" * 20,
                authorization_code="c" * 4_097,
                nonce="n" * 20,
            )

    def test_public_backend_installs_abuse_control_middleware(self):
        self.assertTrue(
            any(middleware.cls is AbuseControlMiddleware for middleware in app.user_middleware)
        )

    def test_largest_schema_valid_completion_payload_fits_default_body_cap(self):
        encoded = json.dumps(
            {
                "idempotency_key": "request-owner-000000000001",
                "part_list": [
                    {"PartNumber": part_number, "ETag": "e" * 256}
                    for part_number in range(1, 10_001)
                ]
            },
            separators=(",", ":"),
        ).encode("utf-8")
        UploadCompleteRequest.model_validate_json(encoded)
        settings = Settings(
            session_secret=SECRET,
            user_id_secret_v1=USER_ID_SECRET,
            apple_audience="com.example.pinpoint",
            plaud_client_id="client",
            plaud_client_secret="secret",
            plaud_api_key="api",
        )
        self.assertLessEqual(len(encoded), settings.max_request_body_bytes)


class AbuseSettingsTests(unittest.TestCase):
    def test_abuse_controls_load_from_environment(self):
        environment = {
            "PINPOINT_SESSION_SECRET": SECRET,
            "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
            "PINPOINT_APPLE_AUDIENCE": "com.example.pinpoint",
            "PINPOINT_INVITE_CODES": "invite-code-000000000001",
            "PLAUD_CLIENT_ID": "client",
            "PLAUD_CLIENT_SECRET": "secret",
            "PLAUD_API_KEY": "api",
            "PLAUD_USER_TOKEN_TTL_SECONDS": "1800",
            "PINPOINT_MAX_REQUEST_BODY_BYTES": "32768",
            "PINPOINT_GLOBAL_REQUEST_LIMIT": "700",
            "PINPOINT_GLOBAL_REQUEST_WINDOW_SECONDS": "45",
            "PINPOINT_NONCE_IP_LIMIT": "21",
            "PINPOINT_NONCE_IP_WINDOW_SECONDS": "50",
            "PINPOINT_APPLE_SIGNIN_IP_LIMIT": "7",
            "PINPOINT_APPLE_SIGNIN_IP_WINDOW_SECONDS": "240",
            "PINPOINT_UPLOAD_ATTEMPT_LEASE_SECONDS": "900",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.max_request_body_bytes, 32_768)
        self.assertEqual(settings.plaud_user_token_ttl_seconds, 1800)
        self.assertEqual(settings.global_request_limit, 700)
        self.assertEqual(settings.global_request_window_seconds, 45)
        self.assertEqual(settings.nonce_ip_limit, 21)
        self.assertEqual(settings.nonce_ip_window_seconds, 50)
        self.assertEqual(settings.apple_signin_ip_limit, 7)
        self.assertEqual(settings.apple_signin_ip_window_seconds, 240)
        self.assertEqual(settings.upload_attempt_lease_seconds, 900)


class AbuseMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1_900_000_000.0]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite3")
        self.state = StateStore(self.database_path, now=lambda: self.clock[0])
        self.settings = SimpleNamespace(
            session_secret=SECRET,
            max_request_body_bytes=8,
            global_request_limit=100,
            global_request_window_seconds=60,
            nonce_ip_limit=10,
            nonce_ip_window_seconds=60,
            apple_signin_ip_limit=10,
            apple_signin_ip_window_seconds=300,
        )
        self.downstream_bodies: list[bytes] = []

        async def downstream(scope, receive, send):
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            self.downstream_bodies.append(bytes(body))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": bytes(body)})

        runtime = SimpleNamespace(settings=self.settings, state=self.state)
        self.middleware = AbuseControlMiddleware(
            downstream,
            runtime_provider=lambda: runtime,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def request(
        self,
        *,
        path: str = "/v1/test",
        method: str = "POST",
        chunks: list[bytes] | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
        client: tuple[str, int] | None = ("203.0.113.10", 12345),
    ) -> tuple[int, dict[str, str], bytes]:
        body_chunks = [b""] if chunks is None else chunks
        messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(body_chunks) - 1,
            }
            for index, chunk in enumerate(body_chunks)
        ]
        sent: list[dict] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers or [],
            "client": client,
            "server": ("api.example", 443),
        }

        async def receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(self.middleware(scope, receive, send))
        start = next(message for message in sent if message["type"] == "http.response.start")
        response_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start.get("headers", [])
        }
        response_body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        return start["status"], response_headers, response_body

    def test_body_cap_rejects_declared_and_chunked_bodies(self):
        declared = self.request(
            chunks=[b"not-read"],
            headers=[(b"content-length", b"9")],
        )
        self.assertEqual(declared[0], 413)

        chunked = self.request(
            chunks=[b'{"a":', b'"123"}'],
            headers=[(b"transfer-encoding", b"chunked")],
        )
        self.assertEqual(chunked[0], 413)
        self.assertEqual(self.downstream_bodies, [])

        accepted = self.request(
            chunks=[b"1234", b"5678"],
            headers=[(b"transfer-encoding", b"chunked")],
        )
        self.assertEqual(accepted[0], 200)
        self.assertEqual(self.downstream_bodies, [b"12345678"])

    def test_invalid_or_mismatched_content_length_fails_closed(self):
        invalid = self.request(headers=[(b"content-length", b"not-a-number")])
        self.assertEqual(invalid[0], 400)
        excessive_digits = self.request(headers=[(b"content-length", b"9" * 5_000)])
        self.assertEqual(excessive_digits[0], 400)
        duplicate = self.request(
            headers=[(b"content-length", b"0"), (b"content-length", b"0")]
        )
        self.assertEqual(duplicate[0], 400)
        mismatch = self.request(chunks=[b"123"], headers=[(b"content-length", b"2")])
        self.assertEqual(mismatch[0], 400)

    def test_global_fixed_window_is_durable_and_returns_retry_after(self):
        self.settings.global_request_limit = 2
        self.assertEqual(self.request(path="/v1/one")[0], 200)
        self.assertEqual(self.request(path="/v1/two")[0], 200)
        limited = self.request(path="/v1/three")
        self.assertEqual(limited[0], 429)
        self.assertEqual(limited[1]["retry-after"], "20")

        # Health checks do not consume or depend on the shared public burst
        # bucket, while the next fixed window starts cleanly.
        self.assertEqual(self.request(path="/healthz", method="GET")[0], 200)
        self.clock[0] += 20
        self.assertEqual(self.request(path="/v1/four")[0], 200)

    def test_endpoint_limits_use_socket_ip_and_never_store_raw_ip(self):
        self.settings.nonce_ip_limit = 1
        first = self.request(
            path="/v1/session/nonce",
            headers=[(b"x-forwarded-for", b"198.51.100.20")],
        )
        self.assertEqual(first[0], 200)
        same_socket_new_header = self.request(
            path="/v1/session/nonce",
            headers=[(b"x-forwarded-for", b"198.51.100.21")],
        )
        self.assertEqual(same_socket_new_header[0], 429)
        other_socket_same_header = self.request(
            path="/v1/session/nonce",
            headers=[(b"x-forwarded-for", b"198.51.100.20")],
            client=("203.0.113.11", 12345),
        )
        self.assertEqual(other_socket_same_header[0], 200)

        self.settings.apple_signin_ip_limit = 1
        self.assertEqual(self.request(path="/v1/session/apple")[0], 200)
        self.assertEqual(self.request(path="/v1/session/apple")[0], 429)

        connection = sqlite3.connect(self.database_path)
        try:
            subjects = [
                row[0]
                for row in connection.execute(
                    "SELECT subject_digest FROM fixed_window_limits WHERE bucket = 'nonce_ip'"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(len(subjects), 2)
        self.assertTrue(all(len(value) == 64 for value in subjects))
        self.assertTrue(all("203.0.113" not in value for value in subjects))

    def test_missing_socket_client_uses_one_private_unknown_bucket(self):
        self.settings.nonce_ip_limit = 1
        self.assertEqual(self.request(
            path="/v1/session/nonce",
            client=None,
            headers=[(b"x-forwarded-for", b"198.51.100.1")],
        )[0], 200)
        self.assertEqual(self.request(
            path="/v1/session/nonce",
            client=None,
            headers=[(b"x-forwarded-for", b"198.51.100.2")],
        )[0], 429)

    def test_rate_limit_storage_failure_returns_503(self):
        with patch.object(
            self.state,
            "consume_fixed_window",
            side_effect=StateStoreError("database unavailable"),
        ):
            response = self.request()
        self.assertEqual(response[0], 503)


class FixedWindowAtomicityTests(unittest.TestCase):
    def test_limit_is_atomic_across_store_instances_and_threads(self):
        clock = [1_900_000_000.0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = str(Path(temporary_directory) / "state.sqlite3")
            stores = [
                StateStore(path, now=lambda: clock[0]),
                StateStore(path, now=lambda: clock[0]),
            ]

            barrier = threading.Barrier(16)

            def consume(index: int) -> bool:
                barrier.wait()
                return stores[index % 2].consume_fixed_window(
                    bucket="global_request",
                    subject_digest="a" * 64,
                    limit=7,
                    window_seconds=60,
                ).allowed

            with ThreadPoolExecutor(max_workers=16) as executor:
                allowed = list(executor.map(consume, range(16)))
            self.assertEqual(sum(allowed), 7)
            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    """
                    SELECT request_count FROM fixed_window_limits
                    WHERE bucket = 'global_request' AND subject_digest = ?
                    """,
                    ("a" * 64,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 7)

    def test_state_store_refuses_to_persist_a_raw_ip_subject(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StateStore(str(Path(temporary_directory) / "state.sqlite3"))
            with self.assertRaises(StateStoreError):
                state.consume_fixed_window(
                    bucket="nonce_ip",
                    subject_digest="203.0.113.10",
                    limit=1,
                    window_seconds=60,
                )

    def test_sqlite_failure_is_wrapped_as_state_store_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StateStore(str(Path(temporary_directory) / "state.sqlite3"))
            with patch.object(
                state,
                "_connection",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(StateStoreError):
                    state.consume_fixed_window(
                        bucket="global_request",
                        subject_digest="a" * 64,
                        limit=1,
                        window_seconds=60,
                    )


if __name__ == "__main__":
    unittest.main()
