from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_backend.admin import main as admin_main
from pinpoint_backend.app import (
    get_apple_app_site_association,
    get_invitation_landing,
    healthz,
)
from pinpoint_backend.config import ConfigurationError, Settings
from pinpoint_backend.public_links import (
    apple_app_site_association,
    custom_scheme_activation_url,
    invitation_landing_html,
    public_activation_url,
)
from pinpoint_backend.state import StateStore, StateStoreError


SECRET = "s" * 48
USER_ID_SECRET = "u" * 48
INVITE_CODE = "ppi_" + "a" * 43


def service_environment() -> dict[str, str]:
    return {
        "PINPOINT_SESSION_SECRET": SECRET,
        "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
        "PINPOINT_APPLE_AUDIENCE": "com.example.pinpoint",
        "PLAUD_CLIENT_ID": "client",
        "PLAUD_CLIENT_SECRET": "secret",
        "PLAUD_API_KEY": "api",
    }


class PublicLinkConfigurationTests(unittest.TestCase):
    def test_public_links_are_optional_together_for_local_development(self):
        with patch.dict(os.environ, service_environment(), clear=True):
            settings = Settings.from_environment()
        self.assertIsNone(settings.invite_base_url)
        self.assertIsNone(settings.apple_app_id)

    def test_https_invite_and_apple_app_id_load_as_one_configuration(self):
        environment = service_environment() | {
            "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite",
            "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.pinpoint",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.invite_base_url, "https://pinpoint.example.com/invite")
        self.assertEqual(
            settings.apple_app_id,
            "ABCDE12345.com.example.pinpoint",
        )

    def test_partial_mismatched_or_non_https_configuration_fails_closed(self):
        cases = (
            {"PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite"},
            {
                "PINPOINT_INVITE_BASE_URL": "http://pinpoint.example.com/invite",
                "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.pinpoint",
            },
            {
                "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/other",
                "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.pinpoint",
            },
            {
                "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite",
                "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.other",
            },
        )
        for extra in cases:
            with self.subTest(extra=extra), patch.dict(
                os.environ,
                service_environment() | extra,
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_environment()


class PublicLinkRenderingTests(unittest.TestCase):
    def test_url_builders_encode_only_one_valid_invitation_code(self):
        self.assertEqual(
            custom_scheme_activation_url(INVITE_CODE),
            f"pinpoint://invite?code={INVITE_CODE}",
        )
        self.assertEqual(
            public_activation_url("https://pinpoint.example.com/invite", INVITE_CODE),
            f"https://pinpoint.example.com/invite?code={INVITE_CODE}",
        )
        with self.assertRaises(ValueError):
            custom_scheme_activation_url("short")

    def test_aasa_claims_only_the_invitation_path_for_the_configured_app(self):
        payload = apple_app_site_association("ABCDE12345.com.example.pinpoint")
        detail = payload["applinks"]["details"][0]
        self.assertEqual(detail["appIDs"], ["ABCDE12345.com.example.pinpoint"])
        self.assertEqual(detail["components"][0]["/"], "/invite")

    def test_landing_page_has_no_script_or_external_resource_and_keeps_code_out_of_copy(self):
        page = invitation_landing_html(INVITE_CODE)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("https://", page)
        self.assertEqual(page.count(INVITE_CODE), 1)
        self.assertIn("Invitation ready.", page)
        self.assertIn("Open PinPoint", page)

    def test_public_routes_set_safe_content_types_and_cache_policies(self):
        active = SimpleNamespace(
            settings=SimpleNamespace(
                apple_app_id="ABCDE12345.com.example.pinpoint",
                invite_base_url="https://pinpoint.example.com/invite",
            )
        )
        association = get_apple_app_site_association(active)
        self.assertEqual(association.media_type, "application/json")
        self.assertEqual(association.headers["cache-control"], "public, max-age=3600")

        landing = get_invitation_landing(INVITE_CODE, active)
        self.assertEqual(landing.media_type, "text/html")
        self.assertEqual(landing.headers["cache-control"], "no-store, private")
        self.assertEqual(landing.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", landing.headers["content-security-policy"])

        with self.assertRaises(HTTPException) as invalid:
            get_invitation_landing("short", active)
        self.assertEqual(invalid.exception.status_code, 400)


class DeploymentHealthAndInviteTests(unittest.TestCase):
    def test_health_check_requires_a_working_durable_ledger(self):
        healthy_state = SimpleNamespace(assert_healthy=lambda: None)
        with patch(
            "pinpoint_backend.app.runtime",
            return_value=SimpleNamespace(state=healthy_state),
        ):
            self.assertEqual(healthz(), {"status": "ok"})

        def unavailable() -> None:
            raise StateStoreError("database unavailable")

        with patch(
            "pinpoint_backend.app.runtime",
            return_value=SimpleNamespace(
                state=SimpleNamespace(assert_healthy=unavailable)
            ),
        ):
            with self.assertRaises(HTTPException) as failure:
                healthz()
        self.assertEqual(failure.exception.status_code, 503)
        self.assertNotIn("database", str(failure.exception.detail).lower())

    def test_operator_emits_https_primary_and_custom_scheme_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = str(Path(temporary_directory) / "pinpoint.sqlite3")
            StateStore(database_path)
            environment = {
                "PINPOINT_STATE_DB_PATH": database_path,
                "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
                "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite",
                "PINPOINT_APPLE_APP_ID": "ABCDE12345.com.example.pinpoint",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                result = admin_main(
                    ["invite", "create", "--label", "Example User", "--expires-in-hours", "24"]
                )
            self.assertEqual(result, 0, stderr.getvalue())
            record = json.loads(stdout.getvalue())
            self.assertTrue(record["activation_url"].startswith(
                "https://pinpoint.example.com/invite?code=ppi_"
            ))
            self.assertTrue(record["custom_scheme_activation_url"].startswith(
                "pinpoint://invite?code=ppi_"
            ))
            self.assertEqual(
                record["activation_url"].split("code=", 1)[1],
                record["invite_code"],
            )

    def test_invalid_public_link_configuration_does_not_create_an_invitation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = str(Path(temporary_directory) / "pinpoint.sqlite3")
            state = StateStore(database_path)
            environment = {
                "PINPOINT_STATE_DB_PATH": database_path,
                "PINPOINT_USER_ID_SECRET_V1": USER_ID_SECRET,
                "PINPOINT_INVITE_BASE_URL": "https://pinpoint.example.com/invite",
            }
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()):
                result = admin_main(["invite", "create", "--label", "Member"])
            self.assertEqual(result, 1)
            self.assertEqual(state.list_beta_invites(), [])


if __name__ == "__main__":
    unittest.main()
