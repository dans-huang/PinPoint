#!/usr/bin/env python3
"""Public PinPoint security, onboarding, and deployment-isolation contracts."""

from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PublicPinPointSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "App").glob("*.swift"))
        )
        cls.project = yaml.safe_load((ROOT / "project.yml").read_text())

    def test_self_hosted_and_hosted_modes_have_separate_config_files(self):
        self.assertNotIn("configFiles", {key: value for key, value in self.project.items() if key != "targets"})
        self_hosted = self.project["targets"]["PinPoint"]["configFiles"]
        hosted = self.project["targets"]["PinPointHosted"]["configFiles"]
        self.assertEqual(set(self_hosted.values()), {"Config/PinPoint.xcconfig"})
        self.assertEqual(set(hosted.values()), {"Config/PinPointHosted.xcconfig"})
        self.assertNotEqual(self_hosted, hosted)

    def test_beta_has_no_partner_or_consumer_secrets(self):
        for forbidden in (
            "BRIDGE_RAW_APP_KEY",
            "BRIDGE_RAW_BIND_TOKEN",
            "BridgeMacWiFiHelperSecret",
            "legacy-private-bridge",
            "org.example.private.bridge",
            "consumer Plaud",
        ):
            self.assertNotIn(forbidden, self.sources)
        self.assertIsNone(re.search(r"(?:sk_|sk-)[A-Za-z0-9_-]{20,}", self.sources))

    def test_login_uses_server_nonce_apple_and_keychain(self):
        self.assertIn("v1/session/nonce", self.sources)
        self.assertIn("ASAuthorizationAppleIDButton", self.sources)
        self.assertIn("SHA256.hash", self.sources)
        self.assertIn("kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly", self.sources)
        self.assertIn("v1/session/apple", self.sources)

    def test_apple_authorization_code_reaches_opt_in_server_custody(self):
        welcome = (ROOT / "App/BetaWelcomeViewController.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        backend = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        config = (ROOT / "PublicBackend/pinpoint_backend/config.py").read_text()
        security = (ROOT / "PublicBackend/pinpoint_backend/security.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()

        self.assertIn("credential.authorizationCode", welcome)
        self.assertIn("authorizationCode: payload.authorizationCode", coordinator)
        self.assertIn('body["authorization_code"] = authorizationCode', client)
        self.assertIn("authorization_code: str | None", backend)
        self.assertIn("PINPOINT_APPLE_TOKEN_CUSTODY_ENABLED", config)
        self.assertIn("_verify_exchanged_apple_identity", security)
        self.assertIn("AESGCM", security)
        self.assertIn("CREATE TABLE IF NOT EXISTS apple_token_custody", state)
        self.assertNotIn('@app.delete("/v1/account', backend)

    def test_first_sign_in_accepts_a_managed_invitation_without_persisting_it(self):
        welcome = (ROOT / "App/BetaWelcomeViewController.swift").read_text()
        invitation_link = (ROOT / "App/BetaInvitationLink.swift").read_text()
        scene = (ROOT / "App/SceneDelegate.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        backend = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()
        admin = (ROOT / "PublicBackend/pinpoint_backend/admin.py").read_text()
        session_store = (ROOT / "App/BetaSessionStore.swift").read_text()
        self.assertIn("let inviteCode: String?", welcome)
        self.assertIn("Invitation ready", welcome)
        self.assertIn("Enter invitation code", welcome)
        self.assertIn("BetaInvitationLink.isValid", welcome)
        self.assertIn('static let scheme = "pinpoint"', invitation_link)
        self.assertIn("components.queryItems", invitation_link)
        self.assertIn("coordinator?.handleInvitationURL(url)", scene)
        self.assertIn("guard currentSession == nil, !hasPendingSignOut", coordinator)
        self.assertIn("pendingInvitationCode = nil", coordinator)
        self.assertIn("welcome.showInvitationLinkError(pendingInvitationError)", coordinator)
        keys = coordinator.split("private enum Keys", 1)[1].split("init(window:", 1)[0]
        self.assertNotIn("Invitation", keys)
        self.assertIn('body["invite_code"] = inviteCode', client)
        self.assertIn("invite_code: str | None", backend)
        self.assertIn("beta_authorization_status(user_id, request.invite_code)", backend)
        self.assertIn('"invitation_unavailable"', backend)
        self.assertIn("CREATE TABLE IF NOT EXISTS beta_invites", state)
        self.assertIn("invite_digest TEXT NOT NULL UNIQUE", state)
        self.assertIn('invitations.add_subparsers(dest="invite_command"', admin)
        self.assertIn('record["invite_code"] = raw_code', admin)
        self.assertIn('record["activation_url"]', admin)
        self.assertNotIn("invite", session_store.lower())

    def test_unauthenticated_sign_in_errors_are_not_reported_as_expired_sessions(self):
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        self.assertIn("if http.statusCode == 401, bearerToken != nil", client)
        self.assertIn('case "invitation_required"', client)
        self.assertIn('case "invitation_unavailable"', client)
        self.assertIn('case "membership_disabled"', client)
        backend = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        self.assertIn('"apple_identity_invalid"', backend)

    def test_supported_device_models_are_routed_through_shared_domain(self):
        self.assertIn("PlaudDeviceModel(serialNumber:", self.sources)
        self.assertIn("model.partnerBindingType", self.sources)
        self.assertIn("Plaud NotePin S", (ROOT / "Shared/PlaudDeviceModel.swift").read_text())
        self.assertIn("Plaud Note Pro", (ROOT / "Shared/PlaudDeviceModel.swift").read_text())

    def test_bound_elsewhere_recovery_is_truthful(self):
        self.assertIn("This recorder is connected elsewhere", self.sources)
        self.assertIn("Plaud does not reveal which account owns it", self.sources)
        self.assertIn("I’ve removed it — Try again", self.sources)
        self.assertNotIn("factory reset", self.sources.lower())

    def test_transfer_uses_official_partner_routes(self):
        backend = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "PublicBackend" / "pinpoint_backend").glob("*.py"))
        )
        self.assertIn("open/partner/files/upload/generate-presigned-urls", backend)
        self.assertIn("open/partner/files/upload/complete-upload", backend)
        self.assertIn('path: "v1/uploads"', self.sources)
        self.assertIn("completeUploadAndSubmit", self.sources)
        self.assertIn("v1/transcriptions", self.sources)
        self.assertNotIn("plaud_upload.py", self.sources)

    def test_public_uploads_are_server_issued_owned_and_bounded(self):
        app = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()
        self.assertIn('@app.post("/v1/uploads")', app)
        self.assertIn("probe_audio_size", app)
        self.assertIn("user_daily_recording_limit", app)
        self.assertIn("global_daily_recording_limit", app)
        self.assertIn("owns_transcription", app)
        self.assertIn("reserve_upload_job", state)
        self.assertNotIn('body: ["file_url": fileURL]', self.sources)

    def test_beta_declares_its_required_reason_api_use(self):
        manifest = (ROOT / "App/PrivacyInfo.xcprivacy").read_text()
        self.assertIn("NSPrivacyAccessedAPICategoryUserDefaults", manifest)
        self.assertIn("CA92.1", manifest)
        self.assertIn("NSPrivacyCollectedDataTypeUserID", manifest)
        self.assertIn("NSPrivacyCollectedDataTypeDeviceID", manifest)
        self.assertIn("NSPrivacyCollectedDataTypeAudioData", manifest)
        self.assertIn("NSPrivacyCollectedDataTypeOtherUserContent", manifest)
        self.assertNotIn("requestedScopes", self.sources)
        app_paths = [
            entry["path"] if isinstance(entry, dict) else entry
            for entry in self.project["targetTemplates"]["PinPointApp"]["sources"]
        ]
        self.assertIn("App", app_paths)

    def test_cloud_checkpoint_prevents_a_restart_from_creating_a_second_job(self):
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        self.assertIn("cloudCheckpoint", store)
        self.assertIn("onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool", pipeline)
        self.assertIn("guard onCheckpoint(pending)", pipeline)
        self.assertIn("onProgress: { parts in", pipeline)
        self.assertIn("return onCheckpoint(persisted)", pipeline)
        self.assertIn("onCheckpoint(persisted)", pipeline)
        self.assertIn("completeCheckpoint(\n                    checkpoint", pipeline)
        self.assertIn("jobID: checkpoint.uploadJobID", pipeline)
        self.assertIn("checkpoint?.transcriptionID", pipeline)

    def test_expired_create_request_discards_its_idempotency_checkpoint(self):
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        self.assertIn("canSafelyRestartUpload(after: error)", pipeline)
        self.assertIn('status == 410 || (status == 409 && code == "upload_attempt_mismatch")', pipeline)
        self.assertIn("guard onCheckpoint(nil)", pipeline)
        self.assertIn("restartRetiredUpload", pipeline)
        self.assertIn("this same pipeline", pipeline)

    def test_source_ledger_lease_and_privacy_controls_support_safe_cross_mac_recovery(self):
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        api = (ROOT / "App/PinpointAPIClient.swift").read_text()
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        app = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()
        config = (ROOT / "PublicBackend/pinpoint_backend/config.py").read_text()

        self.assertIn('"pinpoint-recording-v3\\u{0}\\(serialNumber)\\u{0}\\(sessionID)"', store)
        self.assertNotIn("durationMilliseconds", store)
        self.assertGreaterEqual(pipeline.count("heartbeatUpload("), 2)
        self.assertIn("restartRetiredUpload", pipeline)
        self.assertIn("maxRetiredUploadRestarts = 2", pipeline)
        self.assertIn('path: "v1/uploads/\\(jobID)/heartbeat"', api)
        self.assertIn('@app.post("/v1/uploads/{job_id}/heartbeat")', app)
        self.assertIn("expire_and_get_upload_job", app)
        self.assertIn("recording_sources", state)
        self.assertIn("heartbeat_upload_attempt", state)
        self.assertIn("upload_attempt_lease_seconds", config)
        self.assertIn('response.headers["Cache-Control"] = "no-store, private"', app)
        self.assertIn('response.headers["Pragma"] = "no-cache"', app)
        self.assertIn("URLSession(configuration: .ephemeral)", api)
        self.assertIn("reloadIgnoringLocalCacheData", api)

    def test_each_signed_in_user_has_isolated_recordings_and_pairing_state(self):
        self.assertIn("BetaRecordingStore(userID: session.userID)", self.sources)
        self.assertIn("userDirectoryName(for userID:", self.sources)
        self.assertIn("Keys.pairedSerialPrefix + BetaRecordingStore.userDirectoryName", self.sources)
        self.assertIn('appendingPathComponent("PinPoint", isDirectory: true)', self.sources)
        legacy_namespace = "Pinpoint" + "Beta"
        self.assertNotIn(
            f'appendingPathComponent("{legacy_namespace}", isDirectory: true)\n        metadataURL',
            self.sources,
        )

    def test_fractional_server_dates_use_the_canonical_keychain_round_trip(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        store = (ROOT / "App/BetaSessionStore.swift").read_text()
        self.assertIn("abs(persisted.sessionExpiresAt.timeIntervalSince(session.sessionExpiresAt)) < 1.1", coordinator)
        self.assertIn("abs(persisted.plaudTokenExpiresAt.timeIntervalSince(session.plaudTokenExpiresAt)) < 1.1", coordinator)
        self.assertIn("let persisted = try self.persistSession(session)", coordinator)
        self.assertIn("let persisted = try self.persistSession(refreshed)", coordinator)
        self.assertIn("self.currentSession = persisted", coordinator)
        self.assertIn("self.scheduleSessionRefresh(for: persisted)", coordinator)
        self.assertIn("self.deviceService.updateSession(persisted)", coordinator)
        self.assertIn("dateEncodingStrategy = .iso8601", store)
        self.assertIn("dateDecodingStrategy = .iso8601", store)

    def test_beta_requests_permissions_for_explicit_fast_wifi_transfer(self):
        app_template = self.project["targetTemplates"]["PinPointApp"]
        dependencies = yaml.safe_dump(app_template.get("dependencies", []))
        info = plistlib.loads((ROOT / "App/Info.plist").read_bytes())
        entitlements = (ROOT / "App/PinPointHosted.entitlements").read_text()
        self.assertIn("PlaudWiFiSDK", dependencies)
        self.assertIn("NSLocalNetworkUsageDescription", info)
        self.assertIn("NSLocationWhenInUseUsageDescription", info)
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])
        self.assertIn("HotspotConfiguration", entitlements)
        self.assertIn("com.apple.developer.networking.wifi-info", entitlements)

    def test_transient_sign_in_errors_do_not_disable_apple_sign_in(self):
        welcome = (ROOT / "App/BetaWelcomeViewController.swift").read_text()
        self.assertIn("blockingConfigurationError", welcome)
        self.assertIn("initialError", welcome)
        self.assertIn("button.isEnabled = blockingConfigurationError == nil", welcome)

    def test_app_rejects_an_unexpected_plaud_token_destination(self):
        configuration = (ROOT / "App/BetaConfiguration.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        self.assertIn('domain.hasSuffix(".plaud.ai")', configuration)
        self.assertIn("session.plaudDomain == configuration.plaudDomain", coordinator)

    def test_unfinished_cloud_work_is_retried_without_recoping_audio(self):
        device_service = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("retryCloudItems()", device_service)
        self.assertIn("[.local, .uploading, .transcribing, .failed]", device_service)
        self.assertIn("recordingStore.audioURL(for: recording)", device_service)

    def test_copy_failure_retry_reacquires_the_recording_from_the_recorder(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        retry_body = device.split("func retryRecording(_ recording: BetaRecording)", 1)[1]
        retry_body = retry_body.split("func removeRecorder", 1)[0]
        self.assertIn("recordingStore.begin(", retry_body)
        self.assertIn("PlaudDeviceAgent.shared.getFileList(startSessionId: 0)", retry_body)
        self.assertIn("startScan()", retry_body)

    def test_account_switch_requires_a_cold_start_and_keychain_clear_is_verified(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        restart = (ROOT / "App/BetaRestartViewController.swift").read_text()
        self.assertIn("guard try sessionStore.load() == nil", coordinator)
        self.assertIn("BetaRestartViewController", coordinator)
        self.assertIn("sdkUserIDForProcess", device)
        self.assertIn("Press ⌘Q", restart)

    def test_recorder_can_be_released_without_erasing_recordings(self):
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        home = (ROOT / "App/BetaHomeViewController.swift").read_text()
        self.assertFalse((ROOT / "App/PartnerDeviceBindingClient.swift").exists())
        self.assertIn('path: "v1/devices/\\(action)"', client)
        self.assertIn("pinpointAPI.unbindRecorder(", device)
        self.assertIn("PlaudDeviceAgent.shared.depair(clear: true)", device)
        self.assertIn("files on the recorder are not erased", home)
        remove = device.split("func removeRecorder(", 1)[1]
        remove = remove.split("private func tearDownCurrentUser", 1)[0]
        self.assertLess(
            remove.index("PlaudDeviceAgent.shared.checkIsRecording()"),
            remove.index("phase: .requested"),
        )
        file_list = device.split("func bleFileList(bleFiles: [BleFile])", 1)[1]
        file_list = file_list.split("private func exportNextFileIfNeeded", 1)[0]
        self.assertIn("self.recorderReleaseCheckpoint == nil", file_list)
        self.assertIn("!self.cloudReleaseInFlight", file_list)

    def test_beta_routes_bind_and_unbind_only_through_the_pinpoint_backend(self):
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        project = (ROOT / "project.yml").read_text()
        self.assertIn("func bindRecorder(", client)
        self.assertIn("func unbindRecorder(", client)
        self.assertIn('path: "v1/devices/\\(action)"', client)
        self.assertIn("pinpointAPI.bindRecorder(", device)
        self.assertIn("pinpointAPI.unbindRecorder(", device)
        self.assertNotIn("PartnerDeviceBindingClient", device)
        self.assertNotIn("PartnerDeviceBindingClient.swift", project)

    def test_backend_device_lifecycle_is_durable_serialized_and_membership_gated(self):
        app = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()
        self.assertIn('@app.post("/v1/devices/bind")', app)
        self.assertIn('@app.post("/v1/devices/unbind")', app)
        self.assertLess(app.index("active.state.begin_device_binding("), app.index("active.plaud.bind_device("))
        self.assertLess(app.index("active.state.begin_device_release("), app.index("active.plaud.unbind_device("))
        self.assertIn("state IN ('binding', 'bound', 'release_pending', 'released')", state)
        self.assertIn("device_lifecycle_lock(", app)
        self.assertIn("device_lifecycle_lock(", state)
        self.assertIn("SELECT membership_state FROM beta_users", state)
        self.assertIn('membership[0] != "active"', state)

    def test_self_service_account_deletion_is_intentionally_absent(self):
        app = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        security = (ROOT / "PublicBackend/pinpoint_backend/security.py").read_text()
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        visible_beta = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "App").glob("*.swift"))
        )
        self.assertNotIn("delete_account", app)
        self.assertNotIn("delete_account", security)
        self.assertNotIn("deleteAccount", client)
        self.assertNotIn("Delete account", visible_beta)
        self.assertNotIn("/v1/account", app)

    def test_recorder_association_is_durable_across_bind_outcomes(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        ownership = device.split("private func checkCloudOwnership(for device:", 1)[1]
        ownership = ownership.split("private func emitState()", 1)[0]
        persist_intent = ownership.index("recordingStore.beginRecorderAssociation(")
        backend_bind = ownership.index("pinpointAPI.bindRecorder(")
        self.assertLess(persist_intent, backend_bind)
        self.assertIn("case .identityConflict(let existing):", ownership)
        self.assertLess(ownership.index("case .identityConflict"), backend_bind)

        success = ownership.split("case .success:", 1)[1]
        success = success.split("case .failure(.boundElsewhere):", 1)[0]
        self.assertIn("recordingStore.markRecorderAssociationBound", success)

        ownership_conflict = ownership.split("case .failure(.boundElsewhere):", 1)[1]
        ownership_conflict = ownership_conflict.split("case .failure(.expiredSession):", 1)[0]
        self.assertIn("recordingStore.clearRecorderAssociation(", ownership_conflict)
        self.assertIn("matchingSerialNumber: device.serialNumber", ownership_conflict)
        self.assertIn('status == 409', client)
        self.assertIn('code == "recorder_claimed_elsewhere"', client)
        self.assertIn('object["detail"] as? [String: Any]', client)
        self.assertNotIn('localizedCaseInsensitiveContains("bound to another account")', client)
        self.assertIn("if let existing = recorderAssociationValue", store)
        self.assertIn("return .identityConflict(existing)", store)
        self.assertIn("Repeated bind checks never downgrade", store)

    def test_saved_reconnect_ignores_other_recorders_and_keeps_retrying(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        reconnect_body = device.split("func reconnectSavedDeviceIfAvailable() -> Bool", 1)[1]
        reconnect_body = reconnect_body.split("func startScan()", 1)[0]
        self.assertIn("shouldMaintainConnection = true", reconnect_body)
        self.assertIn("autoReconnectSerial = serial", reconnect_body)
        scan_body = device.split("func bleScanResult(bleDevices: [BleDevice])", 1)[1]
        scan_body = scan_body.split("func bleScanOverTime()", 1)[0]
        self.assertIn("visible.first(where: { $0.serialNumber == serial })", scan_body)
        self.assertIn("else if self.autoReconnectSerial == nil, !visible.isEmpty", scan_body)
        self.assertNotIn("else if !visible.isEmpty", scan_body)

    def test_release_checkpoint_advances_and_is_cleared_last(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        remove_body = device.split("func removeRecorder(", 1)[1]
        remove_body = remove_body.split("private func tearDownCurrentUser", 1)[0]
        self.assertIn("phase: .requested", remove_body)
        self.assertLess(
            remove_body.index("recordingStore.saveRecorderReleaseCheckpoint(checkpoint)"),
            remove_body.rindex("resumeRecorderRelease(checkpoint, completion: recorderReleaseCompletionHandler())"),
        )

        resume_body = device.split("private func resumeRecorderRelease(", 1)[1]
        resume_body = resume_body.split("private func continueLocalRecorderDepair(", 1)[0]
        self.assertIn("pinpointAPI.unbindRecorder(", resume_body)
        self.assertIn("confirmed.phase = .cloudConfirmed", resume_body)
        self.assertLess(
            resume_body.index("confirmed.phase = .cloudConfirmed"),
            resume_body.index("recordingStore.saveRecorderReleaseCheckpoint(confirmed)"),
        )

        finish_body = device.split("private func finishRecorderRemoval(status: Int?)", 1)[1]
        finish_body = finish_body.split("private func finalizeRecorderRelease(", 1)[0]
        self.assertIn("guard status == 0", finish_body)
        self.assertIn("checkpoint.phase = .localDepaired", finish_body)
        self.assertIn("recordingStore.saveRecorderReleaseCheckpoint(checkpoint)", finish_body)

        finalize = device.split("private func finalizeRecorderRelease(", 1)[1]
        finalize = finalize.split("enum BetaRecorderRemovalError", 1)[0]
        association_clear = finalize.index("recordingStore.clearRecorderAssociation(")
        paired_clear = finalize.index("defaults.removeObject(forKey: key)")
        legacy_clear = finalize.index("defaults.removeObject(forKey: key)", paired_clear + 1)
        defaults_flush = finalize.index("defaults.synchronize()")
        checkpoint_clear = finalize.index("recordingStore.clearRecorderReleaseCheckpoint()")
        self.assertLess(association_clear, paired_clear)
        self.assertLess(paired_clear, legacy_clear)
        self.assertLess(legacy_clear, defaults_flush)
        self.assertLess(defaults_flush, checkpoint_clear)
        self.assertIn("checkpoint is removed last", finalize)

    def test_any_release_phase_resumes_before_cloud_bind(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        update = device.split("func updateSession(_ session: BetaSession)", 1)[1]
        update = update.split("func beginOnboarding()", 1)[0]
        self.assertLess(
            update.index("if let checkpoint = recorderReleaseCheckpoint"),
            update.index("checkCloudOwnership(for: activeScannedDevice)"),
        )
        reconnect = device.split("func reconnectSavedDeviceIfAvailable() -> Bool", 1)[1]
        reconnect = reconnect.split("func startScan()", 1)[0]
        self.assertLess(
            reconnect.index("if let checkpoint = recorderReleaseCheckpoint"),
            reconnect.index("if let connectedDevice"),
        )
        ble_bind = device.split("func bleBind(sn: String?", 1)[1]
        ble_bind = ble_bind.split("func blePowerChange", 1)[0]
        self.assertLess(
            ble_bind.index("if let checkpoint = self.recorderReleaseCheckpoint"),
            ble_bind.index("self.checkCloudOwnership(for: device)"),
        )

    def test_offline_release_uses_saved_association(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        paired = device.split("private var pairedSerial: String?", 1)[1]
        paired = paired.split("private var recorderReleaseCheckpoint", 1)[0]
        self.assertIn("recordingStore?.recorderAssociation()?.serialNumber", paired)
        remove_body = device.split("func removeRecorder(", 1)[1]
        remove_body = remove_body.split("private func tearDownCurrentUser", 1)[0]
        self.assertIn("recordingStore?.recorderAssociation()", remove_body)
        self.assertIn("let serialNumber = association.serialNumber", remove_body)
        self.assertIn("deferredRecorderReleaseCompletion = completion", device)
        release_bind = device.split("if let checkpoint = self.recorderReleaseCheckpoint", 1)[1]
        release_bind = release_bind.split("self.checkCloudOwnership(for: device)", 1)[0]
        self.assertIn("let deferredCompletion = self.deferredRecorderReleaseCompletion", release_bind)

    def test_old_recording_retry_cannot_replace_the_current_recorder(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        retry = device.split("func retryRecording(_ recording: BetaRecording)", 1)[1]
        retry = retry.split("func removeRecorder", 1)[0]
        association_guard = retry.index("let association = recordingStore.recorderAssociation()")
        serial_guard = retry.index("association.serialNumber == recording.deviceSerialNumber")
        reconnect = retry.index("startScan()")
        self.assertLess(association_guard, serial_guard)
        self.assertLess(serial_guard, reconnect)
        self.assertIn("markNeedsSupport", retry[association_guard:reconnect])

    def test_local_delete_cancels_work_and_all_pipeline_callbacks_are_generation_guarded(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        deletion = device.split("func deleteCurrentUserLocalData() throws", 1)[1]
        deletion = deletion.split("func retryRecording", 1)[0]
        for required in (
            "connectedDevice == nil",
            "pairedSerial == nil",
            "recorderReleaseCheckpoint == nil",
            "pendingRecorderRemoval == nil",
            "!cloudReleaseInFlight",
            "invalidateLocalDataCallbacks()",
            "activePipelines.forEach { $0.cancel() }",
            "deleteAllLocalData()",
        ):
            self.assertIn(required, deletion)

        pipeline = device.split("private func startCloudPipeline(", 1)[1]
        pipeline = pipeline.split("private func updateConnected", 1)[0]
        self.assertIn("let expectedLocalDataGeneration = currentLocalDataGeneration()", pipeline)
        self.assertGreaterEqual(
            pipeline.count("isCurrentLocalDataGeneration(expectedLocalDataGeneration)"),
            3,
        )
        self.assertGreaterEqual(pipeline.count("self.recordingStore === recordingStore"), 3)
        invalidation = device.split("private func invalidateLocalDataCallbacks()", 1)[1]
        invalidation = invalidation.split("private func updateConnected", 1)[0]
        self.assertIn("localDataGeneration &+= 1", invalidation)
        self.assertIn("localDataGenerationLock.lock()", invalidation)

    def test_cancelled_abandon_callback_cannot_clear_a_checkpoint(self):
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        abandon = pipeline.split("private func abandonCheckpoint(", 1)[1]
        abandon = abandon.split("private func completeUpload(", 1)[0]
        cancellation_guard = abandon.index("guard let self, self.canContinue else { return }")
        checkpoint_write = abandon.index("_ = onCheckpoint(nil)")
        completion_write = abandon.index("completion(.failure(error))")
        self.assertLess(cancellation_guard, checkpoint_write)
        self.assertLess(cancellation_guard, completion_write)

    def test_logout_stops_device_service_before_keychain_clear_and_fails_blocking(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        finish = coordinator.split("private func finishLocalSignOut()", 1)[1]
        finish = finish.split("private func confirmLocalDataDeletion()", 1)[0]
        disconnect = finish.index("deviceService.disconnect()")
        clear = finish.index("try sessionStore.clear()")
        self.assertLess(disconnect, clear)
        self.assertLess(finish.index("currentSession = nil"), clear)
        failure = finish.split("} catch {", 1)[1]
        self.assertIn("BetaRestartViewController(", failure)
        self.assertIn("Automatic sync is stopped", failure)
        self.assertIn("server sign-in was revoked", failure)
        self.assertNotIn("showWelcome", failure)

    def test_backend_wires_session_refresh_logout_and_daily_byte_limits(self):
        app = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        security = (ROOT / "PublicBackend/pinpoint_backend/security.py").read_text()
        state = (ROOT / "PublicBackend/pinpoint_backend/state.py").read_text()
        self.assertIn('@app.post("/v1/session/refresh"', app)
        self.assertIn('@app.post("/v1/session/logout")', app)
        self.assertIn("previous_session_token=token", app)
        self.assertIn("active.security.revoke_session(token)", app)
        self.assertIn("session_absolute_ttl_seconds", security)
        self.assertIn("refresh_session(", security)
        self.assertIn("revoke_session_family(", state)
        self.assertIn("user_daily_bytes_limit=active.settings.user_daily_audio_bytes_limit", app)
        self.assertIn("global_daily_bytes_limit=active.settings.global_daily_audio_bytes_limit", app)

    def test_scene_wake_rechecks_and_refreshes_the_session(self):
        scene = (ROOT / "App/SceneDelegate.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        self.assertIn("func sceneDidBecomeActive(_ scene: UIScene)", scene)
        self.assertIn("coordinator?.sceneDidBecomeActive()", scene)
        wake_body = coordinator.split("func sceneDidBecomeActive()", 1)[1]
        wake_body = wake_body.split("private func showWelcome", 1)[0]
        self.assertIn("min(session.sessionExpiresAt, session.plaudTokenExpiresAt)", wake_body)
        self.assertIn("refreshSession(session, showFailure: false)", wake_body)
        self.assertIn("scheduleSessionRefresh(for: session)", wake_body)

    def test_access_validation_is_fail_closed_across_scene_wake_and_refresh(self):
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()

        validate_client = client.split("func validateSession(", 1)[1]
        validate_client = validate_client.split("func revokeSession(", 1)[0]
        self.assertIn('path: "v1/session/status"', validate_client)
        self.assertIn('method: "GET"', validate_client)
        self.assertIn('object["status"] as? String == "active"', validate_client)

        wake = coordinator.split("func sceneDidBecomeActive()", 1)[1]
        wake = wake.split("private func showWelcome", 1)[0]
        suspend = wake.index("deviceService.suspendForAccessValidation(")
        validate = wake.index("validateAccess(")
        self.assertLess(wake.index("accessResumePending = true"), suspend)
        self.assertLess(suspend, validate)
        # validateAccess may return while refresh is in flight, so the wake path
        # must already have paused the recorder before reaching that guard.
        self.assertNotIn("!refreshInFlight", wake[:suspend])

        refresh = coordinator.split("private func refreshSession(", 1)[1]
        refresh = refresh.split("private func signOut()", 1)[0]
        success = refresh.split("case .success(let refreshed):", 1)[1]
        success = success.split("case .failure(let error):", 1)[0]
        resume = success.index("deviceService.resumeAfterAccessValidation()")
        pending_guard = success.index("if self.accessResumePending")
        self.assertLess(pending_guard, resume)
        self.assertIn("self.accessResumePending = false", success[pending_guard:resume])

        failure = refresh.split("case .failure(let error):", 1)[1]
        self.assertIn("if self.accessResumePending", failure)
        self.assertIn("self.presentAccessValidationFailure(error: error)", failure)
        self.assertNotIn("resumeAfterAccessValidation", failure)

    def test_sign_out_pauses_before_revoke_and_ambiguous_failure_requires_resolution(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        sign_out = coordinator.split("private func signOut()", 1)[1]
        sign_out = sign_out.split("private func presentUnconfirmedSignOut", 1)[0]
        self.assertLess(
            sign_out.index("deviceService.suspendForAccessValidation("),
            sign_out.index("apiClient.revokeSession("),
        )
        self.assertIn("case .failure(PinpointAPIError.sessionExpired):", sign_out)
        expired = sign_out.split("case .failure(PinpointAPIError.sessionExpired):", 1)[1]
        expired = expired.split("case .failure(let error):", 1)[0]
        self.assertIn("self.finishLocalSignOut()", expired)
        ambiguous = sign_out.split("case .failure(let error):", 1)[1]
        self.assertIn("self.signOutAwaitingResolution = true", ambiguous)
        self.assertIn("self.presentUnconfirmedSignOut(error: error)", ambiguous)
        self.assertNotIn("resumeAfterAccessValidation", ambiguous)
        self.assertIn("authOperationGeneration &+= 1", sign_out)
        self.assertIn("self.authOperationGeneration == expectedAuthOperationGeneration", sign_out)

        resolution = coordinator.split("private func presentUnconfirmedSignOut", 1)[1]
        resolution = resolution.split("private func finishLocalSignOut", 1)[0]
        self.assertIn('title: "Automatic sync is paused"', resolution)
        self.assertIn('title: "Retry Sign Out"', resolution)
        self.assertIn('title: "Resume PinPoint"', resolution)
        resume_action = resolution.split('title: "Resume PinPoint"', 1)[1]
        self.assertIn("self.accessResumePending = true", resume_action)
        self.assertIn("self.validateAccess(", resume_action)
        self.assertNotIn("resumeAfterAccessValidation", resume_action)

    def test_sign_out_intent_survives_crash_and_is_cleared_last(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        sign_out = coordinator.split("private func signOut()", 1)[1]
        sign_out = sign_out.split("private func presentUnconfirmedSignOut", 1)[0]
        self.assertLess(
            sign_out.index("persistPendingSignOut()"),
            sign_out.index("apiClient.revokeSession("),
        )
        start = coordinator.split("func start(invitationURL:", 1)[1]
        start = start.split("func sceneDidBecomeActive()", 1)[0]
        self.assertIn("if hasPendingSignOut", start)
        self.assertIn("Finish your previous sign-out", start)
        self.assertLess(start.index("if hasPendingSignOut"), start.index("refreshSession(session"))
        finish = coordinator.split("private func finishLocalSignOut()", 1)[1]
        finish = finish.split("private func confirmLocalDataDeletion", 1)[0]
        self.assertLess(finish.index("try sessionStore.clear()"), finish.index("clearPendingSignOut()"))
        self.assertIn("guard try sessionStore.load() == nil", finish)
        validate = coordinator.split("private func validateAccess(", 1)[1]
        validate = validate.split("private func presentAccessValidationFailure", 1)[0]
        self.assertIn("|| self.resumeClearsPendingSignOut", validate)
        self.assertIn("|| self.hasPendingSignOut", validate)
        self.assertIn("self.finishLocalSignOut()", validate)
        pending_success = validate.split("case .success:", 1)[1]
        pending_success = pending_success.split("case .failure(PinpointAPIError.sessionExpired):", 1)[0]
        self.assertIn("self.hasPendingSignOut && !self.resumeClearsPendingSignOut", pending_success)
        self.assertIn("self.presentUnconfirmedSignOut", pending_success)

    def test_keychain_restore_and_pending_signout_are_fail_closed_before_login(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        session_store = (ROOT / "App/BetaSessionStore.swift").read_text()
        start = coordinator.split("func start(invitationURL:", 1)[1]
        start = start.split("func sceneDidBecomeActive()", 1)[0]
        self.assertIn("catch SessionStoreError.invalidSession where !hasPendingSignOut", start)
        self.assertIn("showSessionStorageFailure", start)
        self.assertIn("hasPendingSignOut && !isTrusted(session)", start)
        self.assertNotIn("try? sessionStore.clear()", start)
        sign_in = coordinator.split("welcome.onSignIn =", 1)[1]
        sign_in = sign_in.split("navigationController.setViewControllers", 1)[0]
        self.assertGreaterEqual(sign_in.count("guard !self.hasPendingSignOut"), 2)
        decode = session_store.split("func load() throws", 1)[1]
        decode = decode.split("func save(", 1)[0]
        self.assertIn("try clear()", decode)
        self.assertNotIn("try? clear()", decode)

    def test_bootstrap_and_refresh_validation_overlap_are_fail_closed(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        validate = coordinator.split("private func validateAccess(", 1)[1]
        validate = validate.split("private func presentAccessValidationFailure", 1)[0]
        refresh_overlap = validate.split("if refreshInFlight", 1)[1]
        refresh_overlap = refresh_overlap.split("guard !accessValidationInFlight", 1)[0]
        self.assertIn("accessResumePending = true", refresh_overlap)
        self.assertIn("deviceService.suspendForAccessValidation", refresh_overlap)
        access_failure = coordinator.split("private func presentAccessValidationFailure", 1)[1]
        access_failure = access_failure.split("private func expireCurrentSession", 1)[0]
        self.assertIn("if self.bootstrapPending", access_failure)
        self.assertIn("self.refreshSession(session, showFailure: true)", access_failure)
        refresh = coordinator.split("private func refreshSession(", 1)[1]
        refresh = refresh.split("private func signOut()", 1)[0]
        self.assertIn("self.bootstrapPending = false", refresh)
        self.assertIn("self.showDeviceSetup(session: persisted)", refresh)
        failure = refresh.split("case .failure(let error):", 1)[1]
        self.assertIn("self.accessResumePending || self.bootstrapPending", failure)
        self.assertIn("self.accessResumePending = true", failure)
        success = refresh.split("case .success(let refreshed):", 1)[1]
        success = success.split("case .failure(let error):", 1)[0]
        self.assertIn("showFailure || self.bootstrapPending", success)
        self.assertIn("self.authOperationGeneration == expectedAuthOperationGeneration", refresh)
        untrusted = success.split("guard refreshed.userID == expectedUserID", 1)[1]
        untrusted = untrusted.split("do {", 1)[0]
        self.assertIn("deviceService.suspendForAccessValidation", untrusted)
        self.assertIn("presentAccessValidationFailure", untrusted)
        service_expiry = coordinator.split("private func refreshAfterServiceExpiry()", 1)[1]
        service_expiry = service_expiry.split("private func scheduleAccessValidation", 1)[0]
        self.assertLess(
            service_expiry.index("deviceService.suspendForAccessValidation"),
            service_expiry.index("refreshSession(session"),
        )

    def test_access_suspension_stops_all_work_but_preserves_durable_user_state(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        suspend = device.split("func suspendForAccessValidation(message: String)", 1)[1]
        suspend = suspend.split("func resumeAfterAccessValidation()", 1)[0]
        for stopped in (
            "PlaudDeviceAgent.shared.stopScan()",
            "PlaudDeviceAgent.shared.stopDownloadFile()",
            "PlaudDeviceAgent.shared.stopSyncFile()",
            "PlaudDeviceAgent.shared.disconnect()",
            "PlaudDeviceAgent.shared.setUserAccessToken(nil)",
            "activePipelines.forEach { $0.cancel() }",
        ):
            self.assertIn(stopped, suspend)
        self.assertIn("accessValidationSuspended = true", suspend)
        self.assertIn("state = .accessPaused(message)", suspend)
        self.assertNotIn("session = nil", suspend)
        self.assertNotIn("recordingStore = nil", suspend)
        self.assertNotIn("deleteAllLocalData", suspend)
        self.assertNotIn("clearRecorderAssociation", suspend)
        self.assertNotIn("clearRecorderReleaseCheckpoint", suspend)
        self.assertNotIn("pendingRemoval?.completion", suspend)
        self.assertNotIn("deferredRelease?", suspend)

        resume = device.split("func resumeAfterAccessValidation()", 1)[1]
        resume = resume.split("func deleteCurrentUserLocalData()", 1)[0]
        token = resume.index("setUserAccessToken(session.plaudUserAccessToken)")
        retry = resume.index("retryCloudItems()")
        reconnect = resume.index("reconnectSavedDeviceIfAvailable()")
        self.assertLess(token, retry)
        self.assertLess(retry, reconnect)
        reconnect_body = device.split("func reconnectSavedDeviceIfAvailable() -> Bool", 1)[1]
        reconnect_body = reconnect_body.split("func startScan()", 1)[0]
        self.assertIn("if let checkpoint = recorderReleaseCheckpoint", reconnect_body)

        update_connected = device.split("private func updateConnected(", 1)[1]
        update_connected = update_connected.split("fileprivate func handleExportProgress(", 1)[0]
        self.assertIn("!self.accessValidationSuspended", update_connected)
        self.assertLess(
            update_connected.index("!self.accessValidationSuspended"),
            update_connected.index("self.state = .ready(device)"),
        )
        configure = device.split("func configure(session:", 1)[1]
        configure = configure.split("func updateSession(", 1)[0]
        suspension_gate = configure.index("guard !accessValidationSuspended else { return true }")
        token_restore = configure.index("setUserAccessToken(session.plaudUserAccessToken)")
        self.assertLess(suspension_gate, token_restore)
        self.assertNotIn(
            "accessValidationSuspended = false",
            configure[configure.index("self.session = session"):],
        )

    def test_recording_persistence_is_a_hard_gate_before_side_effects(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()

        file_list = device.split("func bleFileList(bleFiles: [BleFile])", 1)[1]
        file_list = file_list.split("private func exportNextFileIfNeeded()", 1)[0]
        begin = file_list.index("if recordingStore.begin(")
        durable_append = file_list.index("durableFiles.append(file)")
        pending_append = file_list.index("self.pendingFiles.append(")
        export = file_list.index("self.exportNextFileIfNeeded()")
        self.assertLess(begin, durable_append)
        self.assertLess(durable_append, pending_append)
        self.assertLess(pending_append, export)

        complete = device.split("private func handleExportCompleteOnMain(", 1)[1]
        complete = complete.split("fileprivate func handleExportError(", 1)[0]
        mark_local = complete.index("guard recordingStore.markLocal(")
        cloud = complete.index("startCloudPipeline(")
        self.assertLess(mark_local, cloud)
        self.assertIn("return", complete[mark_local:cloud])

        pipeline = device.split("private func startCloudPipeline(", 1)[1]
        pipeline = pipeline.split("private func updateConnected", 1)[0]
        stage = pipeline.split("onStage: {", 1)[1]
        stage = stage.split("onCheckpoint:", 1)[0]
        persist = stage.index("guard recordingStore.updateCloudStage(")
        publish = stage.index("self.emitRecordings(from: recordingStore)")
        self.assertLess(persist, publish)
        self.assertIn("pipeline.cancel()", stage[persist:publish])

        cloud_pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        submit = cloud_pipeline.split("private func completeCheckpoint(", 1)[1]
        submit = submit.split("private func abandonCheckpoint(", 1)[0]
        durable_stage = submit.index('onStage(.transcribing("Submitting"))')
        cancellation_gate = submit.index("guard canContinue else { return }")
        external_complete = submit.index("completeUpload(\n")
        self.assertLess(durable_stage, cancellation_gate)
        self.assertLess(cancellation_gate, external_complete)

    def test_cloud_batch_is_bounded_and_failed_rows_do_not_immediately_loop(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("private let maxConcurrentCloudPipelines = 2", device)
        pipeline = device.split("private func startCloudPipeline(", 1)[1]
        pipeline = pipeline.split("private func retryCloudItems", 1)[0]
        capacity = pipeline.index("cloudPipelines.count < maxConcurrentCloudPipelines")
        start = pipeline.index("pipeline.process(")
        self.assertLess(capacity, start)
        self.assertIn("retryCloudItems(includeFailed: false)", pipeline)
        retry = device.split("private func retryCloudItems(includeFailed:", 1)[1]
        retry = retry.split("private func emitRecordings", 1)[0]
        self.assertIn("? [.local, .uploading, .transcribing, .failed]", retry)
        self.assertIn(": [.local, .uploading, .transcribing]", retry)
        completion = pipeline.split("completion: { [weak self] result in", 1)[1]
        self.assertIn("terminalStateWasPersisted", completion)
        self.assertLess(
            completion.index("guard terminalStateWasPersisted"),
            completion.index("self.retryCloudItems(includeFailed: false)"),
        )

    def test_cloud_callbacks_are_serialized_with_account_and_store_changes(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        pipeline = device.split("private func startCloudPipeline(", 1)[1]
        pipeline = pipeline.split("private func retryCloudItems", 1)[0]
        stage = pipeline.split("onStage: {", 1)[1].split("onCheckpoint:", 1)[0]
        checkpoint = pipeline.split("onCheckpoint: {", 1)[1].split("completion:", 1)[0]
        completion = pipeline.split("completion: {", 1)[1]
        self.assertIn("onMainSync", stage)
        self.assertIn("onMainSync", checkpoint)
        self.assertIn("DispatchQueue.main.async", completion)
        self.assertIn("private func onMainSync", device)
        self.assertIn("emitRecordings(from: recordingStore)", pipeline)

    def test_setup_storage_failure_remains_visible_and_deletion_cannot_false_succeed(self):
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        setup = coordinator.split("private func showDeviceSetup(session:", 1)[1]
        setup = setup.split("private func showHome", 1)[0]
        observer = setup.index("deviceService.onStateChange =")
        configure = setup.index("guard deviceService.configure(")
        reconnect = setup.index("deviceService.reconnectSavedDeviceIfAvailable()")
        self.assertLess(observer, configure)
        self.assertLess(configure, reconnect)
        self.assertIn("setup.render(deviceService.state)", setup)
        deletion = device.split("func deleteCurrentUserLocalData() throws", 1)[1]
        deletion = deletion.split("func retryRecording", 1)[0]
        self.assertIn("guard let recordingStore else", deletion)
        self.assertIn("throw BetaLocalDataDeletionError.storageUnavailable", deletion)
        self.assertNotIn("try recordingStore?.deleteAllLocalData()", deletion)

    def test_connection_and_missed_stop_recovery_are_bounded(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        connect = device.split("func connect(_ device:", 1)[1]
        connect = connect.split("func retryOwnershipCheck", 1)[0]
        self.assertIn("armConnectionTimeout(", connect)
        timeout = device.split("private func armConnectionTimeout(", 1)[1]
        timeout = timeout.split("private func startFileRefreshMonitor", 1)[0]
        self.assertIn("connectionAttemptID == attemptID", timeout)
        self.assertIn("deadline: .now() + 15", timeout)
        monitor = device.split("private func startFileRefreshMonitor()", 1)[1]
        monitor = monitor.split("private func clearConnectionIdentity", 1)[0]
        self.assertIn("Timer(timeInterval: 120", monitor)
        self.assertIn("!PlaudDeviceAgent.shared.checkIsRecording()", monitor)
        self.assertIn("self.currentExportContext == nil", monitor)
        progress = device.split("private func handleExportProgressOnMain", 1)[1]
        progress = progress.split("fileprivate func handleExportComplete", 1)[0]
        self.assertIn("progress > $0", progress)
        self.assertLess(progress.index("progress > $0"), progress.index("armExportWatchdog"))
        bind = device.split("func bleBind(sn:", 1)[1]
        bind = bind.split("func blePowerChange", 1)[0]
        failure = bind.split("guard status == 0", 1)[1]
        failure = failure.split("self.connectionTimeoutWorkItem?.cancel()", 1)[0]
        self.assertIn("PlaudDeviceAgent.shared.disconnect()", failure)
        self.assertIn("self.clearConnectionIdentity()", failure)
        self.assertIn("self.scheduleReconnect(after: 2)", failure)

    def test_active_recording_is_never_exported_as_a_finished_file(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        file_list = device.split("func bleFileList(bleFiles: [BleFile])", 1)[1]
        file_list = file_list.split("private func exportNextFileIfNeeded()", 1)[0]
        recording_check = file_list.index("PlaudDeviceAgent.shared.checkIsRecording()")
        current_session = file_list.index("PlaudDeviceAgent.shared.getCurrentSessionID()")
        exclusion = file_list.index("$0.sessionId != activeSessionID")
        begin = file_list.index("recordingStore.begin(")
        self.assertLess(recording_check, current_session)
        self.assertLess(current_session, exclusion)
        self.assertLess(exclusion, begin)

    def test_export_callbacks_are_scoped_and_interrupted_copies_requeue_safely(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        for field in (
            "let token: UUID",
            "let userGeneration: Int",
            "let localDataGeneration: Int",
            "let sessionID: Int",
            "let serialNumber: String",
        ):
            self.assertIn(field, device)
        self.assertIn("let callback = BetaAudioExportCallback(service: self, context: context)", device)
        self.assertIn("callback: callback", device)
        active = device.split("private func isActiveExport(", 1)[1]
        self.assertIn("currentExportContext == context", active)
        self.assertIn("context.userGeneration == userGeneration", active)
        self.assertIn("isCurrentLocalDataGeneration(context.localDataGeneration)", active)
        self.assertIn("private func armExportWatchdog(for context:", device)
        self.assertIn("interruptActiveExportForRetry(", device)
        interruption = device.split("private func interruptActiveExportForRetry", 1)[1]
        self.assertIn("currentExportContext = nil", interruption)
        self.assertIn("currentExportCallback = nil", interruption)
        self.assertIn("pendingFiles.removeAll()", interruption)
        self.assertIn("PlaudDeviceAgent.shared.stopSyncFile()", interruption)
        self.assertIn("PlaudDeviceAgent.shared.stopDownloadFile()", interruption)

    def test_sdk_is_initialized_once_and_same_user_only_refreshes_the_token(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        configure = device.split("func configure(session:", 1)[1]
        configure = configure.split("func updateSession(", 1)[0]
        self.assertIn("if sdkInitializedForProcess", configure)
        self.assertIn("setUserAccessToken(session.plaudUserAccessToken)", configure)
        self.assertIn("PlaudDeviceAgent.shared.initSDK(", configure)
        self.assertIn("sdkInitializedForProcess = true", configure)
        self.assertLess(
            configure.index("if sdkInitializedForProcess"),
            configure.index("PlaudDeviceAgent.shared.initSDK("),
        )

    def test_existing_local_state_is_strictly_decoded_before_sdk_side_effects(self):
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("init(userID: String, fileManager: FileManager = .default) throws", store)
        self.assertIn("corruptRecordingMetadata", store)
        self.assertIn("corruptRecorderAssociation", store)
        self.assertIn("corruptRecorderReleaseCheckpoint", store)
        self.assertNotIn("try? Data(contentsOf: metadataURL)", store)
        self.assertIn("private static func isValidCloudCheckpoint", store)
        self.assertIn("values.isRegularFile == true", store)
        self.assertIn("values.isSymbolicLink != true", store)
        self.assertIn("(values.fileSize ?? 0) > 0", store)
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        self.assertIn("multipliedReportingOverflow", pipeline)
        configure = device.split("func configure(session:", 1)[1]
        configure = configure.split("func updateSession(", 1)[0]
        store_open = configure.index("store = try BetaRecordingStore")
        sdk_init = configure.index("PlaudDeviceAgent.shared.initSDK(")
        self.assertLess(store_open, sdk_init)

    def test_permanent_recorder_lifecycle_conflict_stops_reconnect(self):
        client = (ROOT / "App/PinpointAPIClient.swift").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("case server(Int, String?)", client)
        self.assertIn("completion(.failure(.server(status, code)))", client)
        conflict = device.split(
            "case .failure(.server(let status, _)) where status == 409:", 1
        )[1].split("case .failure(.server):", 1)[0]
        self.assertIn("shouldMaintainConnection = false", conflict)
        self.assertIn("reconnectWorkItem?.cancel()", conflict)
        self.assertNotIn("scheduleReconnect", conflict)

    def test_failed_cloud_retries_are_queued_when_capacity_is_full(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("private var queuedFailedCloudRetries: Set<BetaCloudWork>", device)
        retry = device.split("func retryRecording(_ recording:", 1)[1]
        retry = retry.split("func removeRecorder(", 1)[0]
        self.assertIn("queueFailedRetryIfBusy: true", retry)
        start = device.split("private func startCloudPipeline(", 1)[1]
        start = start.split("private func retryCloudItems", 1)[0]
        capacity = start.split("guard cloudPipelines.count < maxConcurrentCloudPipelines", 1)[1]
        self.assertIn("queuedFailedCloudRetries.insert(work)", capacity)
        self.assertIn("self.drainQueuedFailedCloudRetries()", start)
        drain = device.split("private func drainQueuedFailedCloudRetries()", 1)[1]
        self.assertIn("queueFailedRetryIfBusy: true", drain)

    def test_release_completion_survives_access_revalidation(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("private var explicitRecorderReleaseCompletion", device)
        remove = device.split("func removeRecorder(", 1)[1]
        remove = remove.split("private func tearDownCurrentUser", 1)[0]
        self.assertIn("explicitRecorderReleaseCompletion = completion", remove)
        suspend = device.split("func suspendForAccessValidation(message: String)", 1)[1]
        suspend = suspend.split("func resumeAfterAccessValidation()", 1)[0]
        self.assertNotIn("explicitRecorderReleaseCompletion = nil", suspend)
        reconnect = device.split("func reconnectSavedDeviceIfAvailable() -> Bool", 1)[1]
        reconnect = reconnect.split("func startScan()", 1)[0]
        self.assertIn("recorderReleaseCompletionHandler()", reconnect)
        handler = device.split("private func recorderReleaseCompletionHandler()", 1)[1]
        handler = handler.split("private func beginLocalRecorderDepair", 1)[0]
        self.assertIn("self.explicitRecorderReleaseCompletion = nil", handler)
        self.assertIn("completion(result)", handler)

    def test_recorder_release_rechecks_capture_before_local_depair(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        depair = device.split("private func beginLocalRecorderDepair(", 1)[1]
        depair = depair.split("private func finishRecorderRemoval", 1)[0]
        recording_gate = depair.index("PlaudDeviceAgent.shared.checkIsRecording()")
        side_effect = depair.index("PlaudDeviceAgent.shared.depair(clear: true)")
        self.assertLess(recording_gate, side_effect)
        self.assertIn("completion(.failure(.recordingInProgress))", depair)
        self.assertIn("shouldMaintainConnection = true", depair)
        record_stop = device.split("func bleRecordStop(", 1)[1]
        record_stop = record_stop.split("func bleFileList(", 1)[0]
        self.assertIn("checkpoint.phase == .cloudConfirmed", record_stop)
        self.assertIn("resumeRecorderRelease(", record_stop)

    def test_terminal_recordings_and_cloud_checkpoints_are_not_reuploaded(self):
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        needs_copy = store.split("func needsCopy(", 1)[1]
        needs_copy = needs_copy.split("func recording(", 1)[0]
        self.assertIn("recording.status == .ready", needs_copy)
        self.assertIn("recording.status == .needsSupport", needs_copy)
        mark_local = store.split("func markLocal(", 1)[1]
        mark_local = mark_local.split("func saveCloudCheckpoint", 1)[0]
        self.assertNotIn("cloudCheckpoint = nil", mark_local)
        self.assertIn("var canResumeWithoutLocalAudio: Bool", store)
        retry = (ROOT / "App/BetaDeviceService.swift").read_text()
        self.assertIn("recording.cloudCheckpoint?.canResumeWithoutLocalAudio == true", retry)
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        process = pipeline.split("func process(", 1)[1]
        self.assertIn("audioURL: URL?", process)
        self.assertLess(
            process.index("checkpoint?.transcriptionID"),
            process.index("guard let audioURL else"),
        )

    def test_retired_remote_only_checkpoint_reacquires_or_freezes_instead_of_looping(self):
        pipeline = (ROOT / "App/BetaCloudPipeline.swift").read_text()
        completion = pipeline.split("private func completeCheckpoint(", 1)[1]
        completion = completion.split("private func handleUploadFailure", 1)[0]
        retired = completion.index("if self.canSafelyRestartUpload(after: error)")
        clear = completion.index("guard onCheckpoint(nil)", retired)
        signal = completion.index("BetaCloudError.localAudioRequiredForSafeRestart", clear)
        self.assertLess(retired, clear)
        self.assertLess(clear, signal)

        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        cloud_completion = device.split("completion: { [weak self] result in", 1)[1]
        cloud_completion = cloud_completion.split("private func onMainSync", 1)[0]
        self.assertIn("shouldReacquireRetiredUploadFromRecorder", cloud_completion)
        self.assertIn("self.retryRecording(current)", cloud_completion)

        retry = device.split("func retryRecording(_ recording: BetaRecording)", 1)[1]
        retry = retry.split("func removeRecorder", 1)[0]
        self.assertIn("recordingStore.recorderAssociation()", retry)
        self.assertIn("recordingStore.markNeedsSupport(", retry)
        self.assertIn("recordingStore.begin(", retry)

    def test_interrupted_copy_never_trusts_a_nonzero_partial_audio_file(self):
        store = (ROOT / "App/BetaRecordingStore.swift").read_text()
        needs_copy = store.split("func needsCopy(", 1)[1]
        needs_copy = needs_copy.split("func recording(", 1)[0]
        checkpoint = needs_copy.index("canResumeWithoutLocalAudio")
        copying = needs_copy.index("recording.status == .copying")
        audio = needs_copy.index("audioURLUnlocked(for: recording) == nil")
        self.assertLess(checkpoint, copying)
        self.assertLess(copying, audio)

    def test_ambiguous_cloud_side_effects_use_stable_recovery_codes(self):
        backend = (ROOT / "PublicBackend/pinpoint_backend/app.py").read_text()
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        for code in (
            "upload_completion_unconfirmed",
            "transcription_submission_unconfirmed",
        ):
            self.assertIn(code, backend)
            self.assertIn(code, device)
        pipeline = device.split("private func startCloudPipeline(", 1)[1]
        pipeline = pipeline.split("private func retryCloudItems", 1)[0]
        completion = pipeline.split("completion: { [weak self] result in", 1)[1]
        self.assertNotIn("localizedCaseInsensitiveContains", completion)
        self.assertIn("case .server(409, let code, let message)", completion)

    def test_terminal_plaud_transcription_failure_is_frozen_for_support(self):
        device = (ROOT / "App/BetaDeviceService.swift").read_text()
        completion = device.split("completion: { [weak self] result in", 1)[1]
        completion = completion.split("private func onMainSync", 1)[0]
        terminal = completion.split("case .transcriptionFailed(let message)", 1)[1]
        terminal = terminal.split("} else {", 1)[0]
        self.assertIn("recordingStore.markNeedsSupport", terminal)
        self.assertNotIn("recordingStore.markFailed", terminal)

    def test_ready_transcripts_can_be_opened_inside_beta(self):
        home = (ROOT / "App/BetaHomeViewController.swift").read_text()
        conversation = (ROOT / "App/BetaConversationViewController.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        detail = (ROOT / "App/BetaTranscriptViewController.swift").read_text()
        self.assertIn("onOpenRecording?(recording)", home)
        self.assertIn("onOpenTranscript?(recording)", conversation)
        self.assertIn("BetaTranscriptViewController(", coordinator)
        self.assertIn("textView.isSelectable = true", detail)

    def test_debug_preview_states_never_ship_as_release_behavior(self):
        preview = (ROOT / "App/BetaPreviewFactory.swift").read_text()
        coordinator = (ROOT / "App/BetaAppCoordinator.swift").read_text()
        self.assertTrue(preview.startswith("#if DEBUG"))
        self.assertIn("#if DEBUG", coordinator)
        self.assertIn("--pinpoint-preview=", preview)


@unittest.skipUnless(shutil.which("xcodegen") and shutil.which("xcodebuild"), "Xcode tools unavailable")
class ResolvedBuildSettingsIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["xcodegen", "generate"], cwd=ROOT, check=True, capture_output=True, text=True)
        cls.hosted = subprocess.run(
            [
                "xcodebuild", "-project", "PinPoint.xcodeproj", "-scheme", "PinPointHosted",
                "-configuration", "Debug", "-showBuildSettings",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout

    def test_hosted_resolved_settings_do_not_inherit_legacy_private_values(self):
        for forbidden in (
            "BRIDGE_USER_ID =",
            "BRIDGE_MAC_WIFI_HELPER_SECRET =",
            "BRIDGE_RAW_APP_KEY =",
            "BRIDGE_RAW_BIND_TOKEN =",
            "BRIDGE_TOKEN_FILE =",
        ):
            self.assertNotIn(forbidden, self.hosted)

    def test_hosted_resolves_its_own_identity_and_entitlements(self):
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER = org.pinpoint.community.hosted", self.hosted)
        self.assertIn("CODE_SIGN_ENTITLEMENTS = App/PinPointHosted.entitlements", self.hosted)


if __name__ == "__main__":
    unittest.main()
