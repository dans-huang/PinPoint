#!/usr/bin/env python3
"""Executable contracts for PinPoint self-hosted activation."""

from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "App"


class LocalAppSourceContractTests(unittest.TestCase):
    def test_configuration_and_session_bind_to_the_same_deployment_mode(self):
        configuration = (APP / "BetaConfiguration.swift").read_text(encoding="utf-8")
        session = (APP / "BetaSession.swift").read_text(encoding="utf-8")
        coordinator = (APP / "BetaAppCoordinator.swift").read_text(encoding="utf-8")
        client = (APP / "PinpointAPIClient.swift").read_text(encoding="utf-8")

        self.assertIn('case selfHosted = "self_hosted"', configuration)
        self.assertIn('forInfoDictionaryKey: "PinpointDeploymentMode"', configuration)
        self.assertIn("let deploymentMode: DeploymentMode", session)
        self.assertIn("session.deploymentMode == configuration.deploymentMode", coordinator)
        self.assertIn("persisted.deploymentMode == session.deploymentMode", coordinator)
        self.assertIn('path: "v1/session/local"', client)
        self.assertIn('body: ["activation_code": activationCode]', client)
        self.assertIn("deploymentMode: response.deploymentMode", client)

    def test_self_hosted_welcome_has_a_local_path_and_hosted_keeps_apple_sign_in(self):
        welcome = (APP / "BetaWelcomeViewController.swift").read_text(encoding="utf-8")
        coordinator = (APP / "BetaAppCoordinator.swift").read_text(encoding="utf-8")

        self.assertIn('BetaTheme.primaryButton(title: "Connect this Mac"', welcome)
        self.assertIn("guard deploymentMode == .selfHosted", welcome)
        self.assertIn("onCreateLocalSession?(activationCode)", welcome)
        self.assertIn("ASAuthorizationAppleIDButton", welcome)
        self.assertIn("guard deploymentMode == .hosted", welcome)
        self.assertIn("apiClient.signInWithApple", coordinator)
        self.assertIn("apiClient.createLocalSession", coordinator)

    def test_info_and_entitlements_keep_local_build_free_of_identity_capabilities(self):
        info = plistlib.loads((APP / "Info.plist").read_bytes())
        schemes = info["CFBundleURLTypes"][0]["CFBundleURLSchemes"]
        self.assertEqual(info["CFBundleDisplayName"], "PinPoint")
        self.assertEqual(info["CFBundleName"], "PinPoint")
        self.assertEqual(info["PinpointDeploymentMode"], "$(PINPOINT_DEPLOYMENT_MODE)")
        self.assertIn("pinpoint", schemes)

        local = plistlib.loads((APP / "PinPoint.entitlements").read_bytes())
        hosted = plistlib.loads((APP / "PinPointHosted.entitlements").read_bytes())
        self.assertNotIn("com.apple.developer.applesignin", local)
        self.assertNotIn("com.apple.developer.associated-domains", local)
        self.assertTrue(local["com.apple.developer.networking.HotspotConfiguration"])
        self.assertIn("com.apple.developer.applesignin", hosted)
        self.assertIn("com.apple.developer.associated-domains", hosted)


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler is unavailable")
class LocalAppExecutableContractTests(unittest.TestCase):
    def test_strict_loopback_url_link_and_session_encoding(self):
        harness = textwrap.dedent(
            r"""
            import Foundation

            func expectInvalid(_ rawValue: String) {
                do {
                    _ = try BetaConfiguration.validatedAPIBaseURL(
                        rawValue: rawValue,
                        deploymentMode: .selfHosted
                    )
                    fatalError("Accepted unsafe self-host URL: \(rawValue)")
                } catch {
                    // Expected.
                }
            }

            let valid = try BetaConfiguration.validatedAPIBaseURL(
                rawValue: "http://127.0.0.1:8787",
                deploymentMode: .selfHosted
            )
            precondition(valid.absoluteString == "http://127.0.0.1:8787")

            [
                "http://127.0.0.1",
                "https://127.0.0.1:8787",
                "http://localhost:8787",
                "http://user@127.0.0.1:8787",
                "http://127.0.0.1:8787/",
                "http://127.0.0.1:8787/api",
                "http://127.0.0.1:8787?code=secret",
                "http://127.0.0.1:8787#fragment",
                "http://127.0.0.1:0",
                "http://127.0.0.1:65536",
            ].forEach(expectInvalid)

            _ = try BetaConfiguration.validatedAPIBaseURL(
                rawValue: "https://pinpoint.example.com",
                deploymentMode: .hosted
            )

            let activation = "local_activation_123456789"
            guard let link = URL(string: "pinpoint://local?code=\(activation)") else {
                fatalError("Could not build activation URL")
            }
            precondition(PinpointLocalActivationLink.code(from: link) == activation)
            [
                "pinpoint://local",
                "pinpoint://local/?code=local_activation_123456789",
                "pinpoint://local?code=short",
                "pinpoint://local?code=local_activation_123456789&extra=x",
                "pinpoint://local?code=local_activation_123456789#fragment",
                "pinpoint://user@local?code=local_activation_123456789",
                "pinpoint://local:42?code=local_activation_123456789",
                "pinpoint://remote?code=local_activation_123456789",
            ].forEach { rawValue in
                precondition(PinpointLocalActivationLink.code(from: URL(string: rawValue)!) == nil)
            }

            let session = BetaSession(
                sessionToken: "session",
                plaudUserAccessToken: "plaud",
                userID: "pinpoint_local",
                plaudDomain: "api.plaud.ai",
                deploymentMode: .selfHosted,
                sessionExpiresAt: Date(timeIntervalSince1970: 2_000_000_000),
                plaudTokenExpiresAt: Date(timeIntervalSince1970: 2_000_000_000)
            )
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            let encoded = try encoder.encode(session)
            let object = try JSONSerialization.jsonObject(with: encoded) as! [String: Any]
            precondition(object["deployment_mode"] as? String == "self_hosted")
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let decoded = try decoder.decode(BetaSession.self, from: encoded)
            precondition(decoded == session)
            let response = try decoder.decode(BetaSessionResponse.self, from: encoded)
            precondition(response.userID == "pinpoint_local")
            precondition(response.deploymentMode == .selfHosted)
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            main = Path(temporary) / "main.swift"
            binary = Path(temporary) / "local-app-contract"
            main.write_text(harness, encoding="utf-8")
            subprocess.run(
                [
                    "swiftc",
                    str(APP / "BetaConfiguration.swift"),
                    str(APP / "PinpointLocalActivationLink.swift"),
                    str(APP / "BetaSession.swift"),
                    str(main),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(binary)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
