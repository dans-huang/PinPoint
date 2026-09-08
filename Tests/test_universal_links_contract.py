#!/usr/bin/env python3
"""Deployment packaging and Universal Link contracts for PinPoint."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UniversalLinkAppContractTests(unittest.TestCase):
    def test_hosted_app_accepts_https_invites_and_custom_scheme_fallback(self):
        parser = (ROOT / "App/BetaInvitationLink.swift").read_text()
        scene = (ROOT / "App/SceneDelegate.swift").read_text()
        info = (ROOT / "App/Info.plist").read_text()
        self.assertIn('static let scheme = "pinpoint"', parser)
        self.assertIn("allowedHTTPSBaseURL", parser)
        self.assertIn('inviteBaseURLInfoKey = "PinpointInviteBaseURL"', parser)
        self.assertIn(
            "urlComponents.percentEncodedPath == baseComponents.percentEncodedPath",
            parser,
        )
        self.assertIn("NSUserActivityTypeBrowsingWeb", scene)
        self.assertIn("userActivity.webpageURL", scene)
        self.assertIn("PinpointInviteBaseURL", info)

    def test_associated_domain_is_release_configurable(self):
        entitlements = (ROOT / "App/PinPointHosted.entitlements").read_text()
        configuration = (ROOT / "Config/PinPointHosted.xcconfig").read_text()
        example = (ROOT / "Config/PinPointHosted.local.xcconfig.example").read_text()
        self.assertIn("com.apple.developer.associated-domains", entitlements)
        self.assertIn("$(PINPOINT_ASSOCIATED_DOMAIN)", entitlements)
        self.assertIn("PINPOINT_INVITE_BASE_URL =", configuration)
        self.assertIn("PINPOINT_ASSOCIATED_DOMAIN =", configuration)
        self.assertIn("applinks:pinpoint.your-domain.example", example)


class BackendDeploymentContractTests(unittest.TestCase):
    def test_container_runs_unprivileged_as_one_worker_with_a_live_healthcheck(self):
        dockerfile = (ROOT / "PublicBackend/Dockerfile").read_text()
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('"--workers", "1"', dockerfile)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/healthz", dockerfile)
        self.assertIn("--no-binary=cryptography", dockerfile)
        self.assertIn("FROM rust:1.83-slim-bookworm AS rust-toolchain", dockerfile)
        self.assertNotIn("COPY .", dockerfile)

    def test_compose_keeps_one_read_only_replica_and_one_persistent_ledger(self):
        compose = (ROOT / "PublicBackend/compose.yaml").read_text()
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("pinpoint-state:/var/lib/pinpoint", compose)
        self.assertNotIn("replicas:", compose)
        self.assertIn("Do not scale this service", compose)


if __name__ == "__main__":
    unittest.main()
