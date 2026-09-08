#!/usr/bin/env python3
"""Cross-layer contracts for account-neutral PinPoint intelligence."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BetaIntelligenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = (ROOT / "App/BetaIntelligenceModels.swift").read_text()
        cls.client = (ROOT / "App/BetaIntelligenceAPIClient.swift").read_text()
        cls.coordinator = (ROOT / "App/BetaIntelligenceCoordinator.swift").read_text()
        cls.conversation = (ROOT / "App/BetaConversationViewController.swift").read_text()
        cls.settings = (ROOT / "App/BetaIntelligenceSettingsViewController.swift").read_text()
        cls.home = (ROOT / "App/BetaHomeViewController.swift").read_text()
        cls.app = (ROOT / "App/BetaAppCoordinator.swift").read_text()

    def test_summary_jobs_run_server_side_and_resume_by_durable_identity(self):
        self.assertIn('"execution": background ? "background" : "inline"', self.client)
        self.assertIn("background: true", self.coordinator)
        self.assertIn("getSummaryJob(id: id", self.coordinator)
        self.assertIn("PinPoint.Intelligence.", self.coordinator)
        self.assertIn('durableKey + ".request"', self.coordinator)
        self.assertIn('durableKey + ".job"', self.coordinator)
        self.assertIn('durableKey + ".payload"', self.coordinator)
        self.assertIn("findSummaryJob", self.models)
        self.assertIn("findSummaryJob", self.client)
        self.assertIn("resumePendingWordingReview", self.coordinator)
        self.assertIn("resumePendingWordingReview()", self.conversation)
        self.assertIn("attempt < 180", self.coordinator)

    def test_summary_custom_words_templates_and_review_are_user_visible(self):
        for token in (
            "Improve wording",
            "Regenerate",
            "Latest approved summary",
            "BetaSummaryReviewViewController",
        ):
            self.assertIn(token, self.conversation)
        for token in (
            "Generate summaries automatically",
            "Default summary template",
            "Custom words",
            "promptForTemplate",
            "promptForWord",
            "updateTemplate",
            "archiveTemplate",
        ):
            self.assertIn(token, self.settings + self.models + self.client)

    def test_every_ready_recording_exposes_summary_and_assistant_actions(self):
        self.assertIn("onOpenRecording?(recording)", self.home)
        self.assertIn("onHandoffRecording?(recording)", self.home)
        self.assertIn("recording.status == .ready", self.home)
        self.assertIn("presentConversation(recording", self.app)
        self.assertIn("presentAssistantHandoff(recording", self.app)

    def test_assistant_receives_approved_summary_and_marked_moments(self):
        self.assertIn("intelligence.loadSummary(for: recording)", self.app)
        self.assertIn("approvedSummary: approvedSummary", self.app)
        self.assertIn("markedMoments: markedMoments", self.app)
        self.assertIn("markedMomentsProvider", self.coordinator)
        self.assertIn("marked_moments", self.client)

    def test_retries_replay_the_original_immutable_request_payload(self):
        # A lost create response must never let a retry rebuild template or
        # marked moments: the same idempotency key with a different payload
        # would permanently 409 on the backend fingerprint.
        self.assertIn("struct DurableJobRequest: Codable", self.coordinator)
        self.assertIn("let templateID: String?", self.coordinator)
        self.assertIn("let markedMoments: [Int]", self.coordinator)
        self.assertIn("durableRequest(durableKey: durableKey) ?? DurableJobRequest(", self.coordinator)
        self.assertIn("persist(request, durableKey: durableKey)", self.coordinator)
        self.assertIn("templateID: request.templateID", self.coordinator)
        self.assertIn("markedMoments: request.markedMoments", self.coordinator)
        self.assertIn("idempotencyKey: request.idempotencyKey", self.coordinator)
        # Retries first recover the committed job by its original key before
        # ever issuing another create/model call.
        self.assertIn("findSummaryJob(\n                idempotencyKey: request.idempotencyKey", self.coordinator)

    def test_conversation_restores_persisted_reviews_when_opened(self):
        # After an app restart the conversation view must restore a pending or
        # awaiting-approval Improve job from durable coordinator state, not
        # only from the in-memory pending-approval inbox.
        view_did_load = self.conversation.split("override func viewDidLoad()", 1)[1]
        view_did_load = view_did_load.split("override func", 1)[0]
        self.assertIn("resumePendingWordingReview()", view_did_load)
        self.assertIn("intelligence.hasPendingWordingReview(for: recording)", self.conversation)
        self.assertIn("intelligence.resumePendingWordingReview(for: recording)", self.conversation)
        # The durable check reads persisted job/request identity, and a known
        # job id is polled instead of creating a duplicate job.
        self.assertIn('defaults.string(forKey: key + ".job")', self.coordinator)
        self.assertIn('defaults.string(forKey: key + ".request")', self.coordinator)
        resume = self.coordinator.split("func resumePendingWordingReview(", 1)[1]
        self.assertIn('durableKey + ".job"', resume.split("guard let requestKey", 1)[0])
        self.assertIn("pollJob(", resume.split("guard let requestKey", 1)[0])

    def test_account_neutral_app_has_no_private_credentials(self):
        combined = self.models + self.client + self.coordinator + self.conversation + self.settings
        for forbidden in (
            "PLAUD_REFRESH_TOKEN",
            "BridgeUserId",
            "MacWiFiHelperToken",
            "consumerPlaudFileId",
        ):
            self.assertNotIn(forbidden, combined)


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("swiftc"),
    "The macOS Swift compiler is unavailable",
)
class BetaIntelligenceCoordinatorExecutableTests(unittest.TestCase):
    def test_lost_response_retry_and_restart_restore_are_idempotent(self):
        harness = textwrap.dedent(
            r"""
            import Foundation

            struct BetaSession { let sessionToken: String; let userID: String }
            enum BetaRecordingStatus { case ready }
            struct BetaRecording { let status: BetaRecordingStatus; let transcriptionID: String? }
            struct BetaMarkedMomentTag { let timestamp: Int }
            struct BetaRecordingMarkedMoments { var tags: [BetaMarkedMomentTag] }
            final class BetaRecordingStore {
                static func userDirectoryName(for userID: String) -> String { "user-" + userID }
            }

            final class ScriptedClient: BetaIntelligenceProviding {
                struct CreateCall {
                    let templateID: String?
                    let markedMoments: [Int]
                    let idempotencyKey: String
                }
                var createCalls: [CreateCall] = []
                var findCalls: [String] = []
                var getJobCalls: [String] = []
                var createResults: [Result<BetaSummaryJob, Error>] = []
                var findResults: [Result<BetaSummaryJob?, Error>] = []
                var getJobResults: [Result<BetaSummaryJob, Error>] = []

                func createSummaryJob(
                    transcriptionID: String,
                    kind: BetaSummaryJobKind,
                    templateID: String?,
                    markedMoments: [Int],
                    background: Bool,
                    sessionToken: String,
                    idempotencyKey: String,
                    completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
                ) {
                    createCalls.append(CreateCall(
                        templateID: templateID,
                        markedMoments: markedMoments,
                        idempotencyKey: idempotencyKey
                    ))
                    completion(createResults.removeFirst())
                }

                func findSummaryJob(
                    idempotencyKey: String,
                    sessionToken: String,
                    completion: @escaping (Result<BetaSummaryJob?, Error>) -> Void
                ) {
                    findCalls.append(idempotencyKey)
                    completion(findResults.removeFirst())
                }

                func getSummaryJob(
                    id: String,
                    sessionToken: String,
                    completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
                ) {
                    getJobCalls.append(id)
                    completion(getJobResults.removeFirst())
                }

                func getSettings(sessionToken: String, completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void) { fatalError("unused") }
                func updateSettings(_ settings: BetaIntelligenceSettings, sessionToken: String, completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void) { fatalError("unused") }
                func listTemplates(sessionToken: String, completion: @escaping (Result<[BetaSummaryTemplate], Error>) -> Void) { fatalError("unused") }
                func createTemplate(name: String, prompt: String, sessionToken: String, completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void) { fatalError("unused") }
                func updateTemplate(id: String, name: String, prompt: String, sessionToken: String, completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void) { fatalError("unused") }
                func archiveTemplate(id: String, sessionToken: String, completion: @escaping (Result<Void, Error>) -> Void) { fatalError("unused") }
                func listVocabulary(sessionToken: String, completion: @escaping (Result<[BetaVocabularyTerm], Error>) -> Void) { fatalError("unused") }
                func addVocabulary(term: String, sessionToken: String, completion: @escaping (Result<BetaVocabularyTerm, Error>) -> Void) { fatalError("unused") }
                func removeVocabulary(id: String, sessionToken: String, completion: @escaping (Result<Void, Error>) -> Void) { fatalError("unused") }
                func getSummary(transcriptionID: String, sessionToken: String, completion: @escaping (Result<BetaSummarySnapshot?, Error>) -> Void) { fatalError("unused") }
                func approveSummaryJob(id: String, expectedVersion: Int?, expectedHash: String?, approvedVocabulary: [String], sessionToken: String, completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void) { fatalError("unused") }
                func discardSummaryJob(id: String, sessionToken: String, completion: @escaping (Result<Void, Error>) -> Void) { fatalError("unused") }
            }

            func pump(_ seconds: TimeInterval = 0.2) {
                RunLoop.main.run(until: Date().addingTimeInterval(seconds))
            }

            let suite = "PinPointCoordinatorRegression"
            let defaults = UserDefaults(suiteName: suite)!
            defaults.removePersistentDomain(forName: suite)

            let session = BetaSession(sessionToken: "token", userID: "user_1")
            let recording = BetaRecording(status: .ready, transcriptionID: "tr_1")
            var moments = [BetaMarkedMomentTag(timestamp: 5)]

            // 1. Improve wording is requested and the create response is lost.
            let client = ScriptedClient()
            let coordinator = BetaIntelligenceCoordinator(client: client, session: session, defaults: defaults)
            coordinator.markedMomentsProvider = { _ in BetaRecordingMarkedMoments(tags: moments) }
            client.createResults = [.failure(URLError(.networkConnectionLost))]
            var sawFirstFailure = false
            coordinator.improveSummary(for: recording, templateID: "tpl_original") { result in
                if case .failure = result { sawFirstFailure = true }
            }
            pump()
            precondition(sawFirstFailure, "The lost response must surface as an error")
            precondition(client.createCalls.count == 1)
            let original = client.createCalls[0]
            precondition(original.templateID == "tpl_original")
            precondition(original.markedMoments == [5])

            // 2. Inputs change before the retry. The retry must first try to
            // recover the job by its original key, then replay the exact
            // original payload - never the rebuilt inputs.
            moments = [BetaMarkedMomentTag(timestamp: 9), BetaMarkedMomentTag(timestamp: 12)]
            let proposed = BetaSummaryJob(
                id: "job_1",
                transcriptionID: "tr_1",
                kind: .improve,
                status: .proposed,
                proposedSummary: "Proposed wording",
                proposedVocabulary: [],
                baseSummaryVersion: 3,
                baseSummaryHash: "hash_3",
                errorMessage: nil
            )
            client.findResults = [.success(nil)]
            client.createResults = [.success(proposed)]
            var retriedJobID: String?
            coordinator.improveSummary(for: recording, templateID: "tpl_changed") { result in
                if case .success(let job) = result { retriedJobID = job.id }
            }
            pump()
            precondition(client.findCalls == [original.idempotencyKey], "Retry must resolve the original key on the backend first")
            precondition(client.createCalls.count == 2)
            let replay = client.createCalls[1]
            precondition(replay.idempotencyKey == original.idempotencyKey, "Retry must reuse the original idempotency key")
            precondition(replay.templateID == "tpl_original", "Retry must replay the original template")
            precondition(replay.markedMoments == [5], "Retry must replay the original marked moments")
            precondition(retriedJobID == "job_1")

            // 3. App restart: a fresh coordinator over the same durable state
            // restores the awaiting-approval job by polling its persisted job
            // id, with no duplicate create and no extra model call.
            let restartClient = ScriptedClient()
            restartClient.getJobResults = [.success(proposed)]
            let restarted = BetaIntelligenceCoordinator(client: restartClient, session: session, defaults: defaults)
            precondition(restarted.hasPendingWordingReview(for: recording), "Restart must see the persisted pending review")
            var restoredJob: BetaSummaryJob?
            restarted.resumePendingWordingReview(for: recording) { result in
                if case .success(let job) = result { restoredJob = job }
            }
            pump()
            precondition(restartClient.getJobCalls == ["job_1"], "Restore must poll the persisted job id")
            precondition(restartClient.createCalls.isEmpty, "Restore must not create a duplicate job")
            precondition(restartClient.findCalls.isEmpty, "Restore must not need key recovery when the job id is known")
            precondition(restoredJob?.id == "job_1")
            precondition(restoredJob?.status == .proposed)

            defaults.removePersistentDomain(forName: suite)
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            main = Path(temporary) / "main.swift"
            binary = Path(temporary) / "intelligence-coordinator-regression"
            main.write_text(harness, encoding="utf-8")
            subprocess.run(
                [
                    "swiftc",
                    str(ROOT / "App" / "BetaIntelligenceModels.swift"),
                    str(ROOT / "App" / "BetaIntelligenceCoordinator.swift"),
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
