import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "App" / "BetaMarkedMoments.swift"
RAW_HEADER = ROOT / "App" / "BetaRawMarkingTransport.h"
RAW_IMPLEMENTATION = ROOT / "App" / "BetaRawMarkingTransport.m"
SDK_ADAPTER = ROOT / "App" / "BetaMarkedMomentSDKAdapter.swift"
BRIDGING_HEADER = ROOT / "App" / "PinPoint-Bridging-Header.h"
DEVICE_SERVICE = ROOT / "App" / "BetaDeviceService.swift"


class BetaMarkedMomentSourceContractTests(unittest.TestCase):
    def test_lane_is_read_only_and_scoped(self):
        swift = SOURCE.read_text(encoding="utf-8")
        raw = RAW_IMPLEMENTATION.read_text(encoding="utf-8")

        self.assertIn("let userScope: String", swift)
        self.assertIn("let userGeneration: Int", swift)
        self.assertIn("current.identifier == scopeIdentifier", swift)
        self.assertIn("maximumRecordingWindowAttempts = 8", swift)
        self.assertIn("activeRecordingSessionID != nil", swift)
        self.assertIn("sessionID != activeRecordingSessionID", swift)
        self.assertIn("options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]", swift)
        self.assertIn("getRecordMarkingTagsWithUid", raw)
        self.assertIn("getMarking:sessionId", raw)

        combined = swift + "\n" + raw
        for destructive_call in (
            "deleteFile(",
            "deleteFile:",
            "clearAllFile",
            "depair(",
            "startRecord(",
            "stopRecord(",
        ):
            self.assertNotIn(destructive_call, combined)

    def test_raw_transport_forwards_the_existing_sdk_delegate(self):
        raw = RAW_IMPLEMENTATION.read_text(encoding="utf-8")
        self.assertIn("self.primaryDelegate = self.agent.delegate", raw)
        self.assertIn("[invocation invokeWithTarget:primary]", raw)
        self.assertIn("self.agent.delegate = self.primaryDelegate", raw)
        self.assertIn("isEqualToString:scopeIdentifier", raw)

    def test_sdk_adapter_is_a_main_queue_scoped_runtime(self):
        adapter = SDK_ADAPTER.read_text(encoding="utf-8")

        self.assertIn("final class BetaMarkedMomentSDKAdapter", adapter)
        self.assertIn("final class BetaMarkedMomentRuntime", adapter)
        self.assertIn("private let store: BetaMarkedMomentStore", adapter)
        self.assertIn("private let coordinator: BetaMarkedMomentCoordinator", adapter)
        self.assertIn("DispatchQueue.main.async(execute: operation)", adapter)
        self.assertIn("guard let self, self.isActive else { return }", adapter)
        self.assertIn("guard connectedDeviceSerialNumber == recording.deviceSerialNumber", adapter)
        self.assertIn("timestamp: Int($0.timestamp)", adapter)
        self.assertIn("let values = marks.map(\\.uint32Value)", adapter)
        self.assertGreaterEqual(
            adapter.count("guard activeScopeIdentifier == scopeIdentifier else { return }"),
            3,
        )
        self.assertGreaterEqual(adapter.count("installCapture(scopeIdentifier: scopeIdentifier)"), 3)

    def test_device_service_opens_only_scoped_recording_windows(self):
        service = DEVICE_SERVICE.read_text(encoding="utf-8")

        self.assertIn("private var markedMomentRuntime: BetaMarkedMomentRuntime?", service)
        self.assertIn("runtime.activate(userID: session.userID, userGeneration: userGeneration)", service)
        self.assertIn("markedMomentRuntime?.deactivate()", service)
        self.assertIn(
            "func bleRecordStart(sessionId: Int, start: Int, status: Int, scene: Int, startTime: Int, reason: Int)",
            service,
        )
        self.assertIn("self.activeRecordingSessionID = sessionId", service)
        self.assertIn("self.activeRecordingSessionID = activeSessionID", service)
        self.assertIn("connectedDeviceSerialNumber: connectedDevice?.serialNumber", service)
        self.assertIn("activeRecordingSessionID: activeRecordingSessionID", service)
        self.assertIn("refreshMarkedMoments(deviceConnected: false)", service)

    @unittest.skipUnless(shutil.which("xcrun"), "Xcode command-line tools are unavailable")
    def test_raw_transport_compiles_against_the_pinned_sdk_headers(self):
        sdk = subprocess.run(
            ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "xcrun",
                "clang",
                "-fobjc-arc",
                "-fmodules",
                "-fsyntax-only",
                "-isysroot",
                sdk,
                "-F",
                str(ROOT / ".vendor" / "plaud-sdk-public" / "sdk" / "ios"),
                str(RAW_IMPLEMENTATION),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    @unittest.skipUnless(shutil.which("xcrun"), "Xcode command-line tools are unavailable")
    def test_sdk_adapter_typechecks_against_the_pinned_sdk(self):
        sdk = subprocess.run(
            ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        recording_stub = textwrap.dedent(
            '''
            import Foundation

            struct BetaRecording {
                let sessionID: Int
                let deviceSerialNumber: String
                let duration: TimeInterval
            }
            '''
        )

        with tempfile.TemporaryDirectory() as directory:
            stub_path = Path(directory) / "BetaRecordingStub.swift"
            stub_path.write_text(recording_stub, encoding="utf-8")
            subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    "-typecheck",
                    "-sdk",
                    sdk,
                    "-target",
                    "arm64-apple-ios14.0",
                    "-F",
                    str(ROOT / ".vendor" / "plaud-sdk-public" / "sdk" / "ios"),
                    "-import-objc-header",
                    str(BRIDGING_HEADER),
                    str(SOURCE),
                    str(stub_path),
                    str(SDK_ADAPTER),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )


@unittest.skipUnless(shutil.which("xcrun"), "Xcode command-line tools are unavailable")
class BetaMarkedMomentBehaviorTests(unittest.TestCase):
    def test_store_retry_and_coordinator_behavior(self):
        harness = textwrap.dedent(
            r'''
            import Foundation

            private func require(_ condition: @autoclosure () -> Bool, _ message: String) {
                if !condition() {
                    FileHandle.standardError.write(Data((message + "\n").utf8))
                    exit(1)
                }
            }

            private final class WorkBox {
                var values: [() -> Void] = []
                func runNext() {
                    require(!values.isEmpty, "expected scheduled marking work")
                    values.removeFirst()()
                }
            }

            private final class FakeTransport: BetaMarkedMomentCommandTransport {
                struct TagsRequest {
                    let scope: String
                    let uid: Int
                    let start: Int
                    let end: Int
                }
                var began: [String] = []
                var ended: [String] = []
                var tagsRequests: [TagsRequest] = []
                var legacyRequests: [(String, Int)] = []

                func beginCapture(scopeIdentifier: String) { began.append(scopeIdentifier) }
                func endCapture(scopeIdentifier: String) { ended.append(scopeIdentifier) }
                func requestRecordMarkingTags(
                    scopeIdentifier: String,
                    uid: Int,
                    startTimestamp: Int,
                    endTimestamp: Int
                ) {
                    tagsRequests.append(TagsRequest(
                        scope: scopeIdentifier,
                        uid: uid,
                        start: startTimestamp,
                        end: endTimestamp
                    ))
                }
                func requestLegacyMarking(scopeIdentifier: String, sessionID: Int) {
                    legacyRequests.append((scopeIdentifier, sessionID))
                }
            }

            @main
            private enum Harness {
                static func main() throws {
                    let temporary = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
                        .appendingPathComponent(UUID().uuidString, isDirectory: true)
                    defer { try? FileManager.default.removeItem(at: temporary) }

                    let userA = try BetaMarkedMomentStore(
                        userID: "user-a",
                        applicationSupportRoot: temporary
                    )
                    let userB = try BetaMarkedMomentStore(
                        userID: "user-b",
                        applicationSupportRoot: temporary
                    )
                    let first = BetaMarkedMomentCandidate(
                        deviceSerialNumber: "882-test",
                        sessionID: 1_800_000_000,
                        duration: 62
                    )
                    let sameIdentity = BetaMarkedMomentCandidate(
                        deviceSerialNumber: "882-test",
                        sessionID: 1_800_000_000,
                        duration: 63
                    )
                    require(first == sameIdentity, "duration must not change recording identity")

                    let pending = try userA.ensurePending(first)
                    require(pending.status == .pending, "new recording must start pending")
                    require(userB.record(for: first) == nil, "marks leaked across users")

                    var exhausted = pending
                    for index in 1...BetaRecordingMarkedMoments.maximumRecordingWindowAttempts {
                        exhausted = try userA.recordRecordingWindowAttempt(
                            first,
                            now: Date(timeIntervalSince1970: TimeInterval(index))
                        )
                    }
                    require(exhausted.status == .unavailable, "retry budget did not exhaust")
                    require(!userA.needsFetch(first), "exhausted recording still scheduled")

                    let tag = BetaMarkedMomentTag(
                        timestamp: first.sessionID + 20,
                        type: 2,
                        status: 1,
                        reserved: [7]
                    )
                    let late = try userA.saveComplete(first, tags: [tag], source: "late-tags")
                    require(late.status == .ready, "late authoritative result did not recover")
                    let noDowngrade = try userA.saveComplete(first, tags: [], source: "late-empty")
                    require(noDowngrade.tags == [tag], "late empty response erased a known mark")

                    let second = BetaMarkedMomentCandidate(
                        deviceSerialNumber: "882-test",
                        sessionID: first.sessionID + 500,
                        duration: 30
                    )
                    let transport = FakeTransport()
                    let work = WorkBox()
                    let coordinator = BetaMarkedMomentCoordinator(
                        store: userA,
                        transport: transport,
                        scheduler: { _, callback in work.values.append(callback) }
                    )
                    guard let scope = coordinator.activate(userID: "user-a", userGeneration: 9) else {
                        require(false, "coordinator did not activate")
                        return
                    }

                    coordinator.refresh(
                        recordings: [second],
                        deviceConnected: true,
                        activeRecordingSessionID: nil
                    )
                    require(transport.tagsRequests.isEmpty, "idle device sent a marking query")

                    coordinator.refresh(
                        recordings: [second],
                        deviceConnected: true,
                        activeRecordingSessionID: second.sessionID + 100
                    )
                    require(transport.began == [scope], "capture did not begin in recording window")
                    require(transport.tagsRequests.count == 1, "range query was not sent")
                    require(transport.tagsRequests[0].start == second.queryStartTimestamp, "wrong range start")
                    work.runNext()
                    require(transport.tagsRequests.count == 2, "sweep query was not sent")
                    work.runNext()
                    require(transport.legacyRequests.count == 1, "legacy fallback was not sent")

                    let uid = transport.tagsRequests[0].uid
                    coordinator.receiveRecordMarkingTags(
                        scopeIdentifier: "stale-user-scope",
                        uid: uid,
                        totals: 1,
                        index: 0,
                        tags: [tag]
                    )
                    require(userA.record(for: second)?.complete == false, "stale callback was accepted")
                    coordinator.receiveRecordMarkingTags(
                        scopeIdentifier: scope,
                        uid: uid,
                        totals: 1,
                        index: 0,
                        tags: [tag]
                    )
                    require(userA.record(for: second)?.status == .ready, "valid callback was not stored")

                    coordinator.deactivate()
                    coordinator.receiveLegacyMarking(
                        scopeIdentifier: scope,
                        sessionID: second.sessionID,
                        status: 0,
                        marks: [UInt32(second.sessionID + 25)]
                    )
                    require(transport.ended == [scope], "delegate capture was not restored")

                    let third = BetaMarkedMomentCandidate(
                        deviceSerialNumber: "882-test",
                        sessionID: first.sessionID + 900,
                        duration: 20
                    )
                    let transport2 = FakeTransport()
                    let work2 = WorkBox()
                    let coordinator2 = BetaMarkedMomentCoordinator(
                        store: userA,
                        transport: transport2,
                        scheduler: { _, callback in work2.values.append(callback) }
                    )
                    _ = coordinator2.activate(userID: "user-a", userGeneration: 10)
                    coordinator2.refresh(
                        recordings: [third],
                        deviceConnected: true,
                        activeRecordingSessionID: third.sessionID + 100
                    )
                    work2.runNext()
                    work2.runNext()
                    require(userA.record(for: third)?.attempts == 1, "first window count was wrong")
                    coordinator2.refresh(
                        recordings: [third],
                        deviceConnected: true,
                        activeRecordingSessionID: nil
                    )
                    coordinator2.refresh(
                        recordings: [third],
                        deviceConnected: true,
                        activeRecordingSessionID: third.sessionID + 200
                    )
                    require(userA.record(for: third)?.attempts == 2, "next recording did not retry")
                }
            }
            '''
        )

        with tempfile.TemporaryDirectory() as directory:
            harness_path = Path(directory) / "Harness.swift"
            executable = Path(directory) / "marked-moments-test"
            harness_path.write_text(harness, encoding="utf-8")
            subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    str(SOURCE),
                    str(harness_path),
                    "-o",
                    str(executable),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [str(executable)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
