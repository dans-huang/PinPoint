#!/usr/bin/env python3
"""Source contracts for the responsive PinPoint intelligence surfaces."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONVERSATION = ROOT / "App" / "BetaConversationViewController.swift"
HOME = ROOT / "App" / "BetaHomeViewController.swift"
SETTINGS = ROOT / "App" / "BetaIntelligenceSettingsViewController.swift"
MODELS = ROOT / "App" / "BetaIntelligenceModels.swift"
CLIENT = ROOT / "App" / "BetaIntelligenceAPIClient.swift"


class BetaIntelligenceUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conversation = CONVERSATION.read_text(encoding="utf-8")
        cls.home = HOME.read_text(encoding="utf-8")
        cls.settings = SETTINGS.read_text(encoding="utf-8")
        cls.models = MODELS.read_text(encoding="utf-8")
        cls.client = CLIENT.read_text(encoding="utf-8")

    def test_home_preserves_horizontal_readability_without_a_fixed_minimum_width(self):
        self.assertIn("content.widthAnchor.constraint(equalTo: viewport.frameLayoutGuide.widthAnchor", self.home)
        self.assertIn("columnsStack.axis = stackedColumns ? .vertical : .horizontal", self.home)
        self.assertIn("headerActionsStack.axis = verticalActions ? .vertical : .horizontal", self.home)
        self.assertNotIn("greaterThanOrEqualToConstant: 980", self.home)
        self.assertIn("Array(recordings.prefix(8))", self.home)
        self.assertIn("recordingsScroll.isScrollEnabled = expanded", self.home)

    def test_conversation_and_review_keep_primary_actions_reachable(self):
        self.assertIn("viewport.contentLayoutGuide", self.conversation)
        self.assertIn("actionsStack.axis = compact ? .vertical : .horizontal", self.conversation)
        self.assertIn("viewport.bottomAnchor.constraint(equalTo: buttons.topAnchor", self.conversation)
        self.assertIn("buttons.axis = compact ? .vertical : .horizontal", self.conversation)
        self.assertIn("review.isModalInPresentation = true", self.conversation)

    def test_wording_review_permission_is_tied_to_the_explicit_action(self):
        improve = self.conversation.split("@objc private func improveTapped()", 1)[1]
        improve = improve.split("private func presentReview", 1)[0]
        self.assertIn("prepareForExplicitAction()", improve)
        self.assertIn('content.title = "Review improved wording"', self.conversation)
        self.assertIn("A wording proposal is ready for your approval in PinPoint.", self.conversation)
        notification = self.conversation.split("func notifyReviewReady", 1)[1]
        notification = notification.split("func clear", 1)[0]
        self.assertNotIn("proposedSummary", notification)
        self.assertIn("BetaPendingWordingReviews.shared.remove", self.conversation)
        self.assertIn("resumePendingWordingReview()", self.conversation)
        self.assertIn("Resuming your background wording review", self.conversation)

    def test_custom_templates_have_visible_edit_and_archive_management(self):
        self.assertIn('title: "Edit"', self.settings)
        self.assertIn('title: "Archive"', self.settings)
        self.assertIn("guard !template.isBuiltIn", self.settings)
        self.assertIn("BetaTemplateEditorViewController", self.settings)
        self.assertIn("promptTextView.heightAnchor.constraint(greaterThanOrEqualToConstant: 250)", self.settings)
        self.assertIn("actionsStack.axis = compact ? .vertical : .horizontal", self.settings)
        self.assertIn("maximumPromptLength = 4_000", self.settings)

    def test_template_update_and_archive_routes_are_declared_by_the_client_contract(self):
        for method in ("updateTemplate", "archiveTemplate"):
            self.assertIn("func " + method, self.models)
            self.assertIn("func " + method, self.client)
        self.assertIn('method: "PATCH"', self.client)
        self.assertIn('method: "DELETE"', self.client)
        self.assertIn("guard Self.safeIdentifier(id)", self.client)
        self.assertIn('path: "v1/intelligence/templates/\\(id)"', self.client)


if __name__ == "__main__":
    unittest.main()
