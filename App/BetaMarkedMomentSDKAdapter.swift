import Foundation
import PlaudBleSDK

/// The only Beta type that knows about the Objective-C delegate relay and
/// Plaud's concrete marking-tag class. All callbacks are moved onto the main
/// queue before they reach the coordinator.
final class BetaMarkedMomentSDKAdapter: BetaMarkedMomentCommandTransport {
    weak var coordinator: BetaMarkedMomentCoordinator?

    private let rawTransport: BetaRawMarkingTransport
    private var activeScopeIdentifier: String?

    init(rawTransport: BetaRawMarkingTransport = BetaRawMarkingTransport()) {
        self.rawTransport = rawTransport
    }

    func beginCapture(scopeIdentifier: String) {
        activeScopeIdentifier = scopeIdentifier
        installCapture(scopeIdentifier: scopeIdentifier)
    }

    private func installCapture(scopeIdentifier: String) {
        rawTransport.beginCapture(
            withScopeIdentifier: scopeIdentifier,
            tagsHandler: { [weak self] callbackScope, uid, totals, index, tags in
                let values = tags.map {
                    BetaMarkedMomentTag(
                        timestamp: Int($0.timestamp),
                        type: Int($0.type),
                        status: Int($0.status),
                        reserved: $0.reserved.map(Int.init)
                    )
                }
                self?.performOnMain {
                    self?.coordinator?.receiveRecordMarkingTags(
                        scopeIdentifier: callbackScope,
                        uid: uid,
                        totals: totals,
                        index: index,
                        tags: values
                    )
                }
            },
            legacyHandler: { [weak self] callbackScope, sessionID, status, marks in
                let values = marks.map(\.uint32Value)
                self?.performOnMain {
                    self?.coordinator?.receiveLegacyMarking(
                        scopeIdentifier: callbackScope,
                        sessionID: sessionID,
                        status: status,
                        marks: values
                    )
                }
            }
        )
    }

    func endCapture(scopeIdentifier: String) {
        guard activeScopeIdentifier == scopeIdentifier else { return }
        activeScopeIdentifier = nil
        rawTransport.endCapture(withScopeIdentifier: scopeIdentifier)
    }

    func requestRecordMarkingTags(
        scopeIdentifier: String,
        uid: Int,
        startTimestamp: Int,
        endTimestamp: Int
    ) {
        guard activeScopeIdentifier == scopeIdentifier else { return }
        // PlaudDeviceAgent legitimately reclaims BleAgent.delegate while it
        // reconnects or changes transport. Reinstalling the scoped relay here
        // makes the read-only query recover without requiring every lifecycle
        // path in BetaDeviceService to know about this adapter.
        installCapture(scopeIdentifier: scopeIdentifier)
        rawTransport.requestRecordMarkingTags(
            withScopeIdentifier: scopeIdentifier,
            uid: uid,
            startTimestamp: startTimestamp,
            endTimestamp: endTimestamp
        )
    }

    func requestLegacyMarking(scopeIdentifier: String, sessionID: Int) {
        guard activeScopeIdentifier == scopeIdentifier else { return }
        installCapture(scopeIdentifier: scopeIdentifier)
        rawTransport.requestLegacyMarking(
            withScopeIdentifier: scopeIdentifier,
            sessionId: sessionID
        )
    }

    private func performOnMain(_ operation: @escaping () -> Void) {
        if Thread.isMainThread {
            operation()
        } else {
            DispatchQueue.main.async(execute: operation)
        }
    }
}

/// UI-facing identity and status without exposing the raw SDK tag objects.
struct BetaMarkedMomentSnapshot: Equatable {
    let deviceSerialNumber: String
    let sessionID: Int
    let status: BetaMarkedMomentStatus
    let attempts: Int
    let fetchedAt: Date?
    let tags: [BetaMarkedMomentTag]
}

/// Owns one signed-in user's complete marked-moment lane. Construct a fresh
/// runtime for every authenticated Beta user. It deliberately has no reference
/// to upload, transcription, deletion, binding, or recording controls.
final class BetaMarkedMomentRuntime {
    var onEvent: ((BetaMarkedMomentCoordinatorEvent) -> Void)?

    private let userID: String
    private let store: BetaMarkedMomentStore
    private let sdkAdapter: BetaMarkedMomentSDKAdapter
    private let coordinator: BetaMarkedMomentCoordinator
    private var isActive = false

    init(
        userID: String,
        fileManager: FileManager = .default,
        applicationSupportRoot: URL? = nil,
        scheduler: BetaMarkedMomentCoordinator.Scheduler? = nil
    ) throws {
        self.userID = userID
        store = try BetaMarkedMomentStore(
            userID: userID,
            fileManager: fileManager,
            applicationSupportRoot: applicationSupportRoot
        )
        sdkAdapter = BetaMarkedMomentSDKAdapter()
        if let scheduler {
            coordinator = BetaMarkedMomentCoordinator(
                store: store,
                transport: sdkAdapter,
                scheduler: scheduler
            )
        } else {
            coordinator = BetaMarkedMomentCoordinator(store: store, transport: sdkAdapter)
        }
        sdkAdapter.coordinator = coordinator
        coordinator.onEvent = { [weak self] event in
            guard let self, self.isActive else { return }
            self.onEvent?(event)
        }
    }

    deinit {
        performOnMainSync { coordinator.deactivate() }
    }

    /// Returns the opaque callback scope. Returning nil means the supplied
    /// account does not match this runtime's per-user storage.
    @discardableResult
    func activate(userID: String, userGeneration: Int) -> String? {
        performOnMainSync {
            guard userID == self.userID else {
                coordinator.deactivate()
                isActive = false
                return nil
            }
            let scope = coordinator.activate(userID: userID, userGeneration: userGeneration)
            isActive = scope != nil
            return scope
        }
    }

    func deactivate() {
        performOnMainSync {
            coordinator.deactivate()
            isActive = false
        }
    }

    /// Feed this from the authoritative Beta recording store after a file-list
    /// update and whenever the recorder enters or leaves a recording window.
    /// Only rows for the currently connected serial are considered.
    func refresh(
        recordings: [BetaRecording],
        connectedDeviceSerialNumber: String?,
        deviceConnected: Bool,
        activeRecordingSessionID: Int?
    ) {
        let candidates = recordings.compactMap { recording -> BetaMarkedMomentCandidate? in
            guard connectedDeviceSerialNumber == recording.deviceSerialNumber else { return nil }
            return BetaMarkedMomentCandidate(
                deviceSerialNumber: recording.deviceSerialNumber,
                sessionID: recording.sessionID,
                duration: recording.duration
            )
        }
        performOnMain { [weak self] in
            guard let self, self.isActive else { return }
            self.coordinator.refresh(
                recordings: candidates,
                deviceConnected: deviceConnected,
                activeRecordingSessionID: activeRecordingSessionID
            )
        }
    }

    func markedMoments(for recording: BetaRecording) -> BetaRecordingMarkedMoments? {
        store.record(for: candidate(for: recording))
    }

    func markStatus(for recording: BetaRecording) -> BetaMarkedMomentStatus {
        markedMoments(for: recording)?.status ?? .pending
    }

    func snapshot(for recording: BetaRecording) -> BetaMarkedMomentSnapshot {
        let record = markedMoments(for: recording)
        return BetaMarkedMomentSnapshot(
            deviceSerialNumber: recording.deviceSerialNumber,
            sessionID: recording.sessionID,
            status: record?.status ?? .pending,
            attempts: record?.attempts ?? 0,
            fetchedAt: record?.fetchedAt,
            tags: record?.tags ?? []
        )
    }

    func snapshots(for recordings: [BetaRecording]) -> [BetaMarkedMomentSnapshot] {
        recordings.map(snapshot(for:))
    }

    private func candidate(for recording: BetaRecording) -> BetaMarkedMomentCandidate {
        BetaMarkedMomentCandidate(
            deviceSerialNumber: recording.deviceSerialNumber,
            sessionID: recording.sessionID,
            duration: recording.duration
        )
    }

    private func performOnMain(_ operation: @escaping () -> Void) {
        if Thread.isMainThread {
            operation()
        } else {
            DispatchQueue.main.async(execute: operation)
        }
    }

    private func performOnMainSync<T>(_ operation: () -> T) -> T {
        if Thread.isMainThread { return operation() }
        return DispatchQueue.main.sync(execute: operation)
    }
}
