#!/usr/bin/env python3
"""Contracts for the isolated PinPoint Fast Wi-Fi transport."""

from pathlib import Path
import plistlib
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class BetaFastTransferContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = (ROOT / "App/BetaDeviceService.swift").read_text()
        cls.model = (ROOT / "App/BetaFastTransfer.swift").read_text()
        cls.domain = (ROOT / "Shared/PlaudDeviceModel.swift").read_text()
        cls.project = yaml.safe_load((ROOT / "project.yml").read_text())

    def test_beta_links_the_official_wifi_sdk_and_declares_required_access(self):
        app_template = self.project["targetTemplates"]["PinPointApp"]
        dependencies = yaml.safe_dump(app_template["dependencies"])
        info = plistlib.loads((ROOT / "App/Info.plist").read_bytes())
        entitlements = (ROOT / "App/PinPointHosted.entitlements").read_text()
        self.assertIn("PlaudWiFiSDK.framework", dependencies)
        self.assertIn("NSLocalNetworkUsageDescription", info)
        self.assertIn("NSLocationWhenInUseUsageDescription", info)
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])
        self.assertIn("com.apple.developer.networking.HotspotConfiguration", entitlements)
        self.assertIn("com.apple.developer.networking.wifi-info", entitlements)

    def test_ui_contract_exposes_one_batch_offer_and_manual_activation(self):
        for declaration in (
            "struct BetaFastTransferOffer",
            "enum BetaFastTransferState",
            "var onFastTransferOffer:",
            "var onFastTransferOfferDismissed:",
            "var onFastTransferStateChange:",
            "func requestManualFastTransfer()",
            "func resolveFastTransferOffer(id: UUID, useWiFi: Bool)",
        ):
            self.assertIn(declaration, self.model + self.service)
        self.assertIn("recordingCount", self.model)
        self.assertIn("totalDuration", self.model)

    def test_automatic_offer_defaults_the_whole_batch_to_ble_after_ten_seconds(self):
        offer = self.service.split("private func offerFastTransferIfNeeded()", 1)[1]
        offer = offer.split("private func clearFastTransferOffer", 1)[0]
        self.assertIn("Date().addingTimeInterval(10)", offer)
        self.assertIn("deadline: .now() + 10", offer)
        self.assertIn("resolveFastTransferOffer(id: offer.id, useWiFi: false)", offer)
        resolve = self.service.split("func resolveFastTransferOffer", 1)[1]
        resolve = resolve.split("func reconnectSavedDeviceIfAvailable", 1)[0]
        self.assertIn("files.forEach", resolve)
        self.assertIn("blePreferredFiles.insert", resolve)
        self.assertIn("exportNextFileIfNeeded()", resolve)
        self.assertIn("Date() < offer.expiresAt", resolve)

    def test_live_device_capability_and_active_recording_exclusion_are_enforced(self):
        self.assertGreaterEqual(self.domain.count("supportsWiFiFastTransfer: true"), 2)
        self.assertIn("var supportsWiFiFastTransfer: Bool = false", self.service)
        self.assertGreaterEqual(
            self.service.count("connectedDevice.supportsWiFiFastTransfer"), 3
        )
        self.assertIn("func refreshWiFiCapabilityAfterHandshake", self.service)
        capability = self.service.split(
            "private func refreshWiFiCapabilityAfterHandshake", 1
        )[1].split("private func closePendingDeviceWiFiIfNeeded", 1)[0]
        self.assertIn("sdkDevice.serialNumber == serialNumber", capability)
        self.assertIn("liveSDKDevice(serialNumber: serialNumber) != nil", capability)
        self.assertIn("sdkDevice.supportWiFi", capability)
        pen_state = self.service.split("func blePenState", 1)[1]
        pen_state = pen_state.split("func bleScanResult", 1)[0]
        self.assertIn("case .checkingOwnership(let device)", pen_state)
        self.assertIn("case .ready(let device)", pen_state)
        self.assertIn("activeScannedDevice?.serialNumber == device.serialNumber", pen_state)
        self.assertIn("connectedDevice?.serialNumber == device.serialNumber", pen_state)
        self.assertIn("refreshWiFiCapabilityAfterHandshake", pen_state)
        self.assertIn("device.supportsWiFiFastTransfer = supportsWiFi", pen_state)
        bind = self.service.split("func bleBind", 1)[1].split(
            "func blePowerChange", 1
        )[0]
        self.assertNotIn("refreshWiFiCapabilityAfterHandshake", bind)
        self.assertNotIn("raw.supportWiFi", bind)
        self.assertIn("PlaudDeviceAgent.shared.checkIsRecording()", self.service)
        self.assertIn("file.sessionId != activeSessionID", self.service)

    def test_wifi_is_only_a_transport_over_the_existing_user_store(self):
        self.assertIn("recordingStore.needsCopy", self.service)
        self.assertIn("recordingStore.markLocal", self.service)
        self.assertIn("currentLocalDataGeneration()", self.service)
        self.assertIn("context.userGeneration == userGeneration", self.service)
        wifi_complete = self.service.split("fileprivate func handleWiFiExportComplete", 1)[1]
        wifi_complete = wifi_complete.split("fileprivate func handleWiFiExportError", 1)[0]
        self.assertLess(
            wifi_complete.index("recordingStore.markLocal"),
            wifi_complete.index("startCloudPipeline"),
        )

    def test_wifi_failure_restores_ble_and_never_deletes_recorder_files(self):
        finish = self.service.split("private func finishWiFiFastTransfer", 1)[1]
        finish = finish.split("private func stopFastTransferForLifecycle", 1)[0]
        for required in (
            "endWiFiTransfer()",
            "PlaudWiFiAgent.shared.disconnect()",
            "shouldMaintainConnection = true",
            "scheduleReconnect(after: 1)",
        ):
            self.assertIn(required, finish)
        self.assertIsNone(
            re.search(
                r"Plaud(?:Device|WiFi)Agent\.shared\.(?:deleteFile|wifiFileDelete)",
                self.service,
            )
        )

    def test_device_wifi_close_is_deferred_until_authenticated_ble_reconnect(self):
        finish = self.service.split("private func finishWiFiFastTransfer", 1)[1]
        finish = finish.split("private func stopFastTransferForLifecycle", 1)[0]
        self.assertIn("deferDeviceWiFiClose(serialNumber: serialNumber)", finish)
        deferred = self.service.split(
            "private func deferDeviceWiFiClose", 1
        )[1].split("private var pairedSerialKey", 1)[0]
        self.assertIn("pendingDeviceWiFiCloseSerial = serialNumber", deferred)
        self.assertIn("pendingDeviceWiFiCloseGeneration = userGeneration", deferred)
        close = self.service.split(
            "private func closePendingDeviceWiFiIfNeeded", 1
        )[1].split("private var pairedSerialKey", 1)[0]
        self.assertIn("pendingDeviceWiFiCloseGeneration == userGeneration", close)
        self.assertIn("pendingDeviceWiFiCloseSerial == serialNumber", close)
        self.assertIn("liveSDKDevice(serialNumber: serialNumber) != nil", close)
        self.assertIn("setDeviceWiFi(open: false)", close)
        bind = self.service.split("func bleBind", 1)[1].split(
            "func blePowerChange", 1
        )[0]
        self.assertIn("closePendingDeviceWiFiIfNeeded(serialNumber: serial)", bind)
        disconnect = self.service.split("func bleConnectState", 1)[1].split(
            "func bleBind", 1
        )[0]
        self.assertGreaterEqual(disconnect.count("deferDeviceWiFiClose"), 2)
        lifecycle = self.service.split(
            "private func stopFastTransferForLifecycle", 1
        )[1].split("private func resetFastTransferStateForNewUser", 1)[0]
        self.assertIn("deferDeviceWiFiClose(serialNumber: serialNumber)", lifecycle)
        self.assertIn("setDeviceWiFi(open: false)", lifecycle)
        self.assertIn("if bluetoothAlreadyRestored", finish)
        self.assertNotIn("PlaudDeviceAgent.shared.disconnect()", finish)

    def test_location_permission_is_resolved_before_ble_is_interrupted(self):
        self.assertIn("import CoreLocation", self.service)
        begin = self.service.split("private func beginWiFiFastTransfer", 1)[1]
        begin = begin.split("private func requestLocationAccessIfNeeded", 1)[0]
        permission = begin.index("requestLocationAccessIfNeeded")
        interrupt = begin.index("interruptActiveBLEExportForWiFi")
        open_channel = begin.index("setDeviceWiFi(open: true)")
        self.assertLess(permission, interrupt)
        self.assertLess(permission, open_channel)
        self.assertIn("case .denied, .restricted", self.service)
        self.assertIn("Automatic Bluetooth copy remains active", self.service)

    def test_wifi_callbacks_are_phase_gated_and_only_one_export_can_run(self):
        start = self.service.split("private func startNextWiFiExport", 1)[1]
        start = start.split("private func armWiFiExportWatchdog", 1)[0]
        self.assertIn("wifiPhase == .exporting", start)
        self.assertIn("wifiCurrentContext == nil", start)

        handshake = self.service.split("fileprivate func handleWiFiHandshake", 1)[1]
        handshake = handshake.split("fileprivate func handleWiFiFileList", 1)[0]
        self.assertIn("wifiTransferGeneration == generation", handshake)
        self.assertIn("wifiPhase == .joiningNetwork", handshake)
        file_list = self.service.split("fileprivate func handleWiFiFileList", 1)[1]
        file_list = file_list.split("fileprivate func handleWiFiFileListFailure", 1)[0]
        self.assertIn("wifiTransferGeneration == generation", file_list)
        self.assertIn("wifiPhase == .waitingForFileList", file_list)
        self.assertIn("wifiPhase = .exporting", file_list)
        close = self.service.split("fileprivate func handleWiFiClose", 1)[1]
        close = close.split("fileprivate func handleWiFiClientFailure", 1)[0]
        self.assertIn("wifiTransferGeneration == generation", close)
        self.assertIn("wifiPhase == .joiningNetwork", close)
        self.assertIn("wifiPhase == .waitingForFileList", close)
        self.assertIn("wifiPhase == .exporting", close)
        self.assertIn("if status == 1000", close)
        self.assertIn("finishWiFiFastTransfer(message: nil)", close)

    def test_per_transfer_delegate_rejects_late_callbacks_and_teardown_is_ordered(self):
        proxy = self.service.split("final class BetaWiFiDelegateProxy", 1)[1]
        proxy = proxy.split("enum BetaDeviceState", 1)[0]
        for callback in (
            "handleWiFiConnectResult",
            "handleWiFiHandshake",
            "handleWiFiFileList",
            "handleWiFiFileListFailure",
            "handleWiFiClose",
            "handleWiFiClientFailure",
        ):
            self.assertIn(callback, proxy)
        self.assertGreaterEqual(proxy.count("generation: generation"), 5)

        finish = self.service.split("private func finishWiFiFastTransfer", 1)[1]
        finish = finish.split("private func stopFastTransferForLifecycle", 1)[0]
        self.assertLess(finish.index("wifiPhase = .closing"), finish.index("endWiFiTransfer()"))
        self.assertLess(finish.index("endWiFiTransfer()"), finish.index("PlaudWiFiAgent.shared.disconnect()"))
        self.assertLess(finish.index("PlaudWiFiAgent.shared.disconnect()"), finish.index("wifiTransferGeneration = nil"))

    def test_manual_switch_waits_for_ble_tail_and_has_a_bounded_lookup(self):
        manual = self.service.split("func requestManualFastTransfer", 1)[1]
        manual = manual.split("func reconnectSavedDeviceIfAvailable", 1)[0]
        self.assertIn("currentExportTailReceived", manual)
        self.assertIn("armManualFastTransferLookupTimeout", manual)
        self.assertIn("deadline: .now() + 30", manual)
        self.assertIn("func bleSyncFileTail", self.service)
        self.assertIn("continueManualFastTransferAfterBLEIfNeeded", self.service)
        file_list = self.service.split("func bleFileList", 1)[1]
        file_list = file_list.split("private func exportNextFileIfNeeded", 1)[0]
        self.assertIn("currentExportTailReceived", file_list)
        continuation = self.service.split(
            "private func continueManualFastTransferAfterBLEIfNeeded", 1
        )[1].split("private func interruptActiveBLEExportForWiFi", 1)[0]
        self.assertIn("!locationPermissionFiles.isEmpty", continuation)

    def test_wifi_error_ends_the_batch_before_ble_retry(self):
        fallback = self.service.split("private func fallbackCurrentWiFiFile", 1)[1]
        fallback = fallback.split("private func failWiFiFastTransfer", 1)[0]
        self.assertIn("failWiFiFastTransfer(message)", fallback)
        self.assertNotIn("startNextWiFiExport()", fallback)
        # The active SDK copy is cancelled once by centralized teardown.
        self.assertNotIn("stopSyncFile", fallback)

    def test_wifi_progress_watchdog_is_monotonic_and_has_an_absolute_limit(self):
        progress = self.service.split("fileprivate func handleWiFiExportProgress", 1)[1]
        progress = progress.split("fileprivate func handleWiFiExportComplete", 1)[0]
        self.assertIn("progress > $0", progress)
        self.assertIn("armWiFiExportWatchdog", progress)
        absolute = self.service.split("private func armWiFiExportAbsoluteTimeout", 1)[1]
        absolute = absolute.split("fileprivate func handleWiFiExportProgress", 1)[0]
        self.assertIn("deadline: .now() + 30 * 60", absolute)
        finish = self.service.split("private func finishWiFiFastTransfer", 1)[1]
        finish = finish.split("private func stopFastTransferForLifecycle", 1)[0]
        self.assertIn("wifiExportAbsoluteTimeoutWorkItem?.cancel()", finish)

    def test_account_lifecycle_cancels_every_ephemeral_wifi_callback(self):
        for method_name in (
            "func suspendForAccessValidation",
            "private func tearDownCurrentUser",
            "func forgetCurrentAttempt",
        ):
            section = self.service.split(method_name, 1)[1]
            section = section[:1800]
            self.assertIn("stopFastTransferForLifecycle()", section)
        cleanup = self.service.split("private func stopFastTransferForLifecycle", 1)[1]
        cleanup = cleanup.split("private func resetFastTransferStateForNewUser", 1)[0]
        self.assertIn("wifiTransferGeneration = nil", cleanup)
        self.assertIn("wifiRequestedFiles.removeAll()", cleanup)
        self.assertIn("PlaudWiFiAgent.shared.delegate = nil", cleanup)


if __name__ == "__main__":
    unittest.main()
