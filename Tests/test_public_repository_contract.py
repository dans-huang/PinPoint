#!/usr/bin/env python3
"""Contracts that keep the public PinPoint repository clean and reproducible."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def public_candidate_files() -> list[Path]:
    """Return tracked and trackable files while honoring the real .gitignore."""
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
    )
    return [ROOT / raw.decode("utf-8") for raw in output.split(b"\0") if raw]


class PublicRepositoryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_text = (ROOT / "project.yml").read_text(encoding="utf-8")
        cls.project = yaml.safe_load(cls.project_text)

    def test_project_has_only_the_public_self_hosted_and_hosted_targets(self):
        self.assertEqual(self.project["name"], "PinPoint")
        self.assertEqual(set(self.project["targets"]), {"PinPoint", "PinPointHosted"})
        self.assertEqual(set(self.project["schemes"]), {"PinPoint", "PinPointHosted"})
        self.assertNotIn("PlaudMacBridge", self.project_text)
        self.assertNotIn("BetaSources", self.project_text)
        self.assertNotIn("MacWiFiHelper", self.project_text)

    def test_targets_share_app_code_but_keep_auth_and_entitlements_separate(self):
        self_hosted = self.project["targets"]["PinPoint"]
        hosted = self.project["targets"]["PinPointHosted"]
        self.assertEqual(self_hosted["configFiles"]["Debug"], "Config/PinPoint.xcconfig")
        self.assertEqual(hosted["configFiles"]["Debug"], "Config/PinPointHosted.xcconfig")
        self.assertEqual(
            self_hosted["settings"]["base"]["CODE_SIGN_ENTITLEMENTS"],
            "$(PINPOINT_CODE_SIGN_ENTITLEMENTS)",
        )
        self.assertEqual(
            hosted["settings"]["base"]["CODE_SIGN_ENTITLEMENTS"],
            "App/PinPointHosted.entitlements",
        )
        template = self.project["targetTemplates"]["PinPointApp"]
        paths = [item["path"] if isinstance(item, dict) else item for item in template["sources"]]
        self.assertIn("App", paths)
        self.assertIn("Shared", paths)
        self.assertNotIn("Sources", paths)

    def test_visible_product_and_bundle_defaults_are_account_neutral(self):
        template = self.project["targetTemplates"]["PinPointApp"]
        settings = template["settings"]["base"]
        self.assertEqual(settings["PRODUCT_NAME"], "PinPoint")
        self.assertEqual(settings["EXECUTABLE_NAME"], "PinPoint")
        configs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in public_candidate_files()
            if path.parent == ROOT / "Config" and ".xcconfig" in path.name
        )
        self.assertIn("PINPOINT_DEPLOYMENT_MODE = self_hosted", configs)
        self.assertIn("PINPOINT_DEPLOYMENT_MODE = hosted", configs)
        self.assertIn("PINPOINT_CODE_SIGN_ENTITLEMENTS =", configs)
        self.assertIn("PINPOINT_BUNDLE_ID = org.pinpoint.community.app", configs)
        self.assertIn("PINPOINT_BUNDLE_ID = org.pinpoint.community.hosted", configs)
        self.assertIn("PINPOINT_BUNDLE_ID = dev.example.pinpoint", configs)
        self.assertIn("PINPOINT_BUNDLE_ID = com.yourcompany.pinpoint", configs)

    def test_fast_wifi_entitlements_are_an_explicit_self_hosted_opt_in(self):
        base = (ROOT / "Config/PinPoint.xcconfig").read_text(encoding="utf-8")
        example = (ROOT / "Config/PinPoint.local.xcconfig.example").read_text(
            encoding="utf-8"
        )
        setup = (ROOT / "scripts/setup.sh").read_text(encoding="utf-8")
        self.assertIn("PINPOINT_CODE_SIGN_ENTITLEMENTS =\n", base)
        self.assertIn("App/PinPoint.entitlements", example)
        self.assertIn("--fast-wifi", setup)

    def test_official_sdk_is_pinned_and_never_vendored(self):
        bootstrap = (ROOT / "scripts/bootstrap-sdk.sh").read_text(encoding="utf-8")
        self.assertIn("https://github.com/Plaud-AI/plaud-sdk-public.git", bootstrap)
        self.assertIn("81c7cecbcf7476e8263abbf3b937a261a4ea8893", bootstrap)
        self.assertIn("SDK commit mismatch", bootstrap)
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/.vendor/", gitignore)
        redistributed = [
            path
            for path in public_candidate_files()
            if path.suffix == ".framework" or path.name.endswith("SDK.bundle")
        ]
        self.assertEqual(redistributed, [])

    def test_self_host_setup_preserves_the_strict_loopback_boundary(self):
        setup = (ROOT / "scripts/setup.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts/backend_runtime.py").read_text(encoding="utf-8")
        activation = (ROOT / "scripts/activate.sh").read_text(encoding="utf-8")
        self.assertIn("PINPOINT_AUTH_MODE self_hosted", setup)
        self.assertIn('host="127.0.0.1"', runtime)
        self.assertIn("activation create", activation)
        self.assertNotIn("docker compose", setup)
        self.assertIn("org.pinpoint.community.backend", setup)

    def test_setup_rejects_unsupported_toolchain_versions_early(self):
        setup = (ROOT / "scripts/setup.sh").read_text(encoding="utf-8")
        self.assertIn("sys.version_info >= (3, 10)", setup)
        self.assertIn("xcode_major < 16", setup)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Xcode 16 or newer", readme)
        self.assertIn("Python 3.10 or newer", readme)

    def test_public_docs_state_material_prerequisites_and_limits(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        for required in (
            "Developer Preview",
            "Plaud Partner",
            "Apple silicon",
            "NotePin S",
            "Note Pro",
            "physical end-to-end verification is still pending",
            "Fast Wi-Fi",
            "BLE",
            "not an official Plaud product",
        ):
            self.assertIn(required, normalized_readme)
        notices = (ROOT / "docs/THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("proprietary", notices)
        self.assertIn("not included in this repository", notices)
        self.assertIn("Apache License", (ROOT / "LICENSE").read_text(encoding="utf-8"))

    def test_no_machine_paths_emails_or_realistic_device_serials(self):
        suspicious_patterns = {
            "absolute macOS user path": re.compile(r"/Users/[^/\s]+/"),
            "email address": re.compile(
                r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
            ),
            "numeric Plaud serial": re.compile(r"\b(?:881|882)\d{10}\b"),
            "masked numeric serial": re.compile(r"••••\d{4}\b"),
        }
        allowed_suffixes = {".md", ".py", ".swift", ".m", ".h", ".plist", ".yml", ".yaml", ".sh", ".xcconfig"}
        findings: list[str] = []
        for path in public_candidate_files():
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            if path.relative_to(ROOT).as_posix() == "Tests/test_public_repository_contract.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for label, pattern in suspicious_patterns.items():
                if pattern.search(text):
                    findings.append(f"{path.relative_to(ROOT)}: {label}")
        self.assertEqual(findings, [])

    def test_no_high_confidence_credentials_are_committed(self):
        patterns = (
            re.compile(r"gh[pousr]_[A-Za-z0-9_]{30,}"),
            re.compile(r"sk_[A-Za-z0-9_-]{20,}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        )
        findings: list[str] = []
        for path in public_candidate_files():
            if not path.is_file():
                continue
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in patterns):
                findings.append(str(path.relative_to(ROOT)))
        self.assertEqual(findings, [])

    def test_ci_builds_both_modes_and_tests_the_backend(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        mac = (ROOT / ".github/workflows/mac-build.yml").read_text(encoding="utf-8")
        self.assertIn("unittest discover -s Tests", ci)
        self.assertIn("unittest discover -s PublicBackend/tests", ci)
        self.assertIn("docker build", ci)
        self.assertIn("PINPOINT_AUTH_MODE=hosted", ci)
        self.assertIn("scripts/build.sh --ios", mac)
        self.assertIn("scripts/build.sh --hosted --ios", mac)
        self.assertIn("unittest discover -s Tests", mac)


if __name__ == "__main__":
    unittest.main()
