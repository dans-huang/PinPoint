import Foundation
import CoreLocation
import PlaudBleSDK
import PlaudDeviceBasicSDK
import PlaudWiFiSDK

struct ScannedPlaudDevice: Equatable {
    let serialNumber: String
    let model: PlaudDeviceModel
    let rssi: Int

    var maskedSerialNumber: String {
        "••••" + serialNumber.suffix(4)
    }
}

struct ConnectedPlaudDevice: Equatable {
    let serialNumber: String
    let model: PlaudDeviceModel
    var batteryLevel: Int?
    var isCharging: Bool
    var storageUsed: Int?
    var storageTotal: Int?
    /// Runtime capability reported by Plaud for this exact device/firmware.
    var supportsWiFiFastTransfer: Bool = false

    var maskedSerialNumber: String { "••••" + serialNumber.suffix(4) }
}

fileprivate struct BetaAudioExportContext: Equatable {
    let token: UUID
    let userGeneration: Int
    let localDataGeneration: Int
    let sessionID: Int
    let serialNumber: String
}

fileprivate struct BetaCloudWork: Hashable {
    let sessionID: Int
    let serialNumber: String
}

fileprivate enum BetaWiFiPhase: Equatable {
    case idle
    case openingDeviceChannel
    case joiningNetwork
    case waitingForFileList
    case exporting
    case closing
}

fileprivate final class BetaAudioExportCallback: NSObject, AudioExportCallback {
    weak var service: BetaDeviceService?
    let context: BetaAudioExportContext

    init(service: BetaDeviceService, context: BetaAudioExportContext) {
        self.service = service
        self.context = context
    }

    func onProgress(_ progress: Int, message: String) {
        service?.handleExportProgress(context, progress: progress, message: message)
    }

    func onComplete(outputPath: String) {
        service?.handleExportComplete(context, outputPath: outputPath)
    }

    func onError(_ error: String) {
        service?.handleExportError(context, error: error)
    }
}

fileprivate final class BetaWiFiAudioExportCallback: NSObject, AudioExportCallback {
    weak var service: BetaDeviceService?
    let context: BetaAudioExportContext

    init(service: BetaDeviceService, context: BetaAudioExportContext) {
        self.service = service
        self.context = context
    }

    func onProgress(_ progress: Int, message: String) {
        service?.handleWiFiExportProgress(context, progress: progress)
    }

    func onComplete(outputPath: String) {
        service?.handleWiFiExportComplete(context, outputPath: outputPath)
    }

    func onError(_ error: String) {
        service?.handleWiFiExportError(context, error: error)
    }
}

/// The SDK delegate does not include a transfer identifier in its callbacks.
/// A per-transfer proxy supplies that missing identity so a delayed callback
/// from an earlier Wi-Fi session can never mutate a later one.
fileprivate final class BetaWiFiDelegateProxy: NSObject, PlaudWiFiAgentProtocol {
    weak var service: BetaDeviceService?
    let generation: UUID

    init(service: BetaDeviceService, generation: UUID) {
        self.service = service
        self.generation = generation
    }

    func wifiConnectResult(_ success: Bool, _ errorCode: Int, _ message: String) {
        service?.handleWiFiConnectResult(
            success,
            errorCode: errorCode,
            message: message,
            generation: generation
        )
    }

    func wifiHandshake(_ status: Int) {
        service?.handleWiFiHandshake(status, generation: generation)
    }

    func wifiFileList(_ files: [BleFile]) {
        service?.handleWiFiFileList(files, generation: generation)
    }

    func wifiFileListFail(_ status: Int) {
        service?.handleWiFiFileListFailure(status, generation: generation)
    }

    func wifiClose(_ status: Int) {
        service?.handleWiFiClose(status, generation: generation)
    }

    func wifiClientFail() {
        service?.handleWiFiClientFailure(generation: generation)
    }
}

enum BetaDeviceState: Equatable {
    case idle
    case scanning
    case discovered([ScannedPlaudDevice])
    case noDevicesFound
    case connecting(ScannedPlaudDevice)
    case checkingOwnership(ScannedPlaudDevice)
    case ready(ConnectedPlaudDevice)
    case bluetoothUnavailable
    case boundElsewhere(ScannedPlaudDevice)
    case releaseWaiting(PlaudDeviceModel)
    case accessPaused(String)
    case sessionExpired
    case failed(String)
}

final class BetaDeviceService: NSObject {
    var onStateChange: ((BetaDeviceState) -> Void)?
    var onRecordingsChange: (([BetaRecording]) -> Void)?
    var onSessionExpired: (() -> Void)?
    /// The coordinator presents one batch-level choice and must call
    /// `resolveFastTransferOffer`. If nobody responds, the service selects BLE
    /// after ten seconds without losing or failing any recording.
    var onFastTransferOffer: ((BetaFastTransferOffer) -> Void)?
    var onFastTransferOfferDismissed: ((UUID) -> Void)?
    var onFastTransferStateChange: ((BetaFastTransferState) -> Void)?
    var onMarkedMomentEvent: ((BetaMarkedMomentCoordinatorEvent) -> Void)?
    private(set) var state: BetaDeviceState = .idle { didSet { emitState() } }
    private(set) var fastTransferState: BetaFastTransferState = .idle {
        didSet { emitFastTransferState() }
    }

    private var session: BetaSession?
    private var cachedDevices: [String: BleDevice] = [:]
    private var activeScannedDevice: ScannedPlaudDevice?
    private var connectedDevice: ConnectedPlaudDevice?
    private var autoReconnectSerial: String?
    private let defaults: UserDefaults
    private var recordingStore: BetaRecordingStore?
    private var markedMomentRuntime: BetaMarkedMomentRuntime?
    private var activeRecordingSessionID: Int?
    private var currentUserID: String?
    private var sdkUserIDForProcess: String?
    private var sdkInitializedForProcess = false
    private var pinpointAPI: PinpointAPIClient?
    private var pendingFiles: [BleFile] = []
    private var currentExportFile: BleFile?
    private var currentExportContext: BetaAudioExportContext?
    private var currentExportCallback: BetaAudioExportCallback?
    private var exportWatchdogWorkItem: DispatchWorkItem?
    private var currentExportTailReceived = false
    private var cloudPipelines: [String: BetaCloudPipeline] = [:]
    private var queuedFailedCloudRetries: Set<BetaCloudWork> = []
    private let cloudPipelineLock = NSLock()
    private let maxConcurrentCloudPipelines = 2
    private var shouldMaintainConnection = false
    private var reconnectWorkItem: DispatchWorkItem?
    private var connectionTimeoutWorkItem: DispatchWorkItem?
    private var depairTimeoutWorkItem: DispatchWorkItem?
    private var fileRefreshTimer: Timer?
    private var userGeneration = 0
    private var localDataGeneration = 0
    private let localDataGenerationLock = NSLock()
    private var scanGeneration: Int?
    private var activeConnectionGeneration: Int?
    private var connectionAttemptID: UUID?
    private var cloudReleaseInFlight = false
    private let accessLeaseLock = NSLock()
    private var accessValidationExplicitlySuspended = true
    private var accessLeaseValidUntil = Date.distantPast
    private var accessLeaseExpiryNotificationPending = false
    private let accessLeaseDuration: TimeInterval = 5 * 60
    private var lastExportProgress: Int?

    // Fast Wi-Fi remains an ephemeral transport choice. Recording identity,
    // completion, and cloud checkpoints continue to live only in the
    // per-user BetaRecordingStore.
    private var fastTransferOffer: BetaFastTransferOffer?
    private var fastTransferOfferFiles: [BleFile] = []
    private var fastTransferDecisionWorkItem: DispatchWorkItem?
    private var manualFastTransferLookupWorkItem: DispatchWorkItem?
    private var blePreferredFiles = Set<BetaCloudWork>()
    private var manualFastTransferRequested = false
    private var locationManager: CLLocationManager?
    private var locationPermissionFiles: [BleFile] = []
    private var locationPermissionGeneration: Int?
    private var locationPermissionWorkItem: DispatchWorkItem?
    private var wifiTransferGeneration: UUID?
    private var wifiPhase: BetaWiFiPhase = .idle
    private var wifiRequestedFiles: [BetaCloudWork: BleFile] = [:]
    private var wifiPendingFiles: [BleFile] = []
    private var wifiCurrentFile: BleFile?
    private var wifiCurrentContext: BetaAudioExportContext?
    private var wifiExportCallback: BetaWiFiAudioExportCallback?
    private var wifiDelegateProxy: BetaWiFiDelegateProxy?
    private var wifiConnectTimeoutWorkItem: DispatchWorkItem?
    private var wifiExportWatchdogWorkItem: DispatchWorkItem?
    private var wifiExportAbsoluteTimeoutWorkItem: DispatchWorkItem?
    private var wifiCompletedCount = 0
    private var wifiLastFallbackMessage: String?
    private var wifiLastExportProgress: Int?
    // The recorder drops BLE while its temporary Wi-Fi channel is active. If
    // BLE is still down when Wi-Fi copying ends, close the recorder channel on
    // the next authenticated reconnect for this exact user and serial number.
    private var pendingDeviceWiFiCloseSerial: String?
    private var pendingDeviceWiFiCloseGeneration: Int?
    // `supportWiFi` is populated asynchronously by the SDK's pen-state
    // callback. Cache it by serial so an earlier cloud ownership response
    // cannot permanently lock a capable recorder into the BLE-only path.
    private var reportedWiFiCapabilitySerial: String?
    private var reportedWiFiCapability = false

    private struct PendingRecorderRemoval {
        let serialNumber: String
        let userID: String
        let generation: Int
        let completion: (Result<Void, BetaRecorderRemovalError>) -> Void
    }

    private var pendingRecorderRemoval: PendingRecorderRemoval?
    private var deferredRecorderReleaseCompletion: ((Result<Void, BetaRecorderRemovalError>) -> Void)?
    private var explicitRecorderReleaseCompletion: ((Result<Void, BetaRecorderRemovalError>) -> Void)?

    private enum Keys {
        static let pairedSerialPrefix = "PinPoint.pairedSerial."
        // Compatibility only. Current releases use an atomic per-user file.
        static let legacyReleasePendingPrefix = "PinPoint.releasePending."
    }

    var recordings: [BetaRecording] { recordingStore?.all ?? [] }
    var requiresColdStartForNewUser: Bool { sdkInitializedForProcess }
    var hasSavedRecorderAssociation: Bool {
        pairedSerial != nil || recorderReleaseCheckpoint != nil
    }
    var isAccessValidationSuspended: Bool { accessValidationSuspended }
    var onAccessLeaseExpired: (() -> Void)?

    /// Every BLE, export, and cloud entry point already checks this property.
    /// Folding the five-minute authorization lease into that shared gate makes
    /// background BLE wakeups fail closed even when iOS suspends the UI timer.
    private var accessValidationSuspended: Bool {
        get {
            accessLeaseLock.lock()
            if accessValidationExplicitlySuspended {
                accessLeaseLock.unlock()
                return true
            }
            if Date() < accessLeaseValidUntil {
                accessLeaseLock.unlock()
                return false
            }
            let shouldNotify = !accessLeaseExpiryNotificationPending
            accessLeaseExpiryNotificationPending = true
            accessLeaseLock.unlock()
            if shouldNotify {
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.suspendForAccessValidation(
                        message: "Confirming that this PinPoint sign-in is still active before automatic sync resumes."
                    )
                    self.onAccessLeaseExpired?()
                }
            }
            return true
        }
        set {
            accessLeaseLock.lock()
            accessValidationExplicitlySuspended = newValue
            accessLeaseValidUntil = newValue
                ? .distantPast
                : Date().addingTimeInterval(accessLeaseDuration)
            accessLeaseExpiryNotificationPending = false
            accessLeaseLock.unlock()
        }
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        super.init()
        PlaudDeviceAgent.shared.delegate = self
    }

    @discardableResult
    func configure(session: BetaSession, pinpointAPI: PinpointAPIClient) -> Bool {
        if let initializedUser = sdkUserIDForProcess, initializedUser != session.userID {
            state = .failed("Quit and reopen PinPoint before signing in as another person.")
            return false
        }
        if currentUserID != session.userID {
            if currentUserID != nil {
                tearDownCurrentUser()
            }
            let store: BetaRecordingStore
            do {
                store = try BetaRecordingStore(userID: session.userID)
            } catch {
                state = .failed(error.localizedDescription)
                return false
            }
            userGeneration &+= 1
            currentUserID = session.userID
            sdkUserIDForProcess = session.userID
            recordingStore = store
            do {
                let runtime = try BetaMarkedMomentRuntime(userID: session.userID)
                runtime.onEvent = { [weak self] event in self?.onMarkedMomentEvent?(event) }
                markedMomentRuntime = runtime
                _ = runtime.activate(userID: session.userID, userGeneration: userGeneration)
            } catch {
                markedMomentRuntime = nil
                DispatchQueue.main.async { [weak self] in
                    self?.onMarkedMomentEvent?(.storageWarning(error.localizedDescription))
                }
            }
            migrateLegacyRecorderAssociationIfNeeded()
            migrateLegacyReleaseCheckpointIfNeeded()
            pendingFiles.removeAll()
            currentExportFile = nil
            currentExportContext = nil
            currentExportCallback = nil
            currentExportTailReceived = false
            resetFastTransferStateForNewUser()
            accessValidationSuspended = false
        }
        self.session = session
        self.pinpointAPI = pinpointAPI
        // Re-entering recorder setup for the same signed-in user must never
        // undo a foreground membership check or an unresolved sign-out. Only
        // `resumeAfterAccessValidation` may lift an existing suspension.
        guard !accessValidationSuspended else { return true }
        PlaudDeviceAgent.shared.delegate = self
        if sdkInitializedForProcess {
            PlaudDeviceAgent.shared.setUserAccessToken(session.plaudUserAccessToken)
        } else {
            PlaudDeviceAgent.shared.initSDK(
                userAccessToken: session.plaudUserAccessToken,
                customDomain: session.plaudDomain
            )
            sdkInitializedForProcess = true
        }
        startFileRefreshMonitor()
        retryCloudItems()
        return true
    }

    func updateSession(_ session: BetaSession) {
        guard sdkUserIDForProcess == session.userID, currentUserID == session.userID else { return }
        let shouldResumeOwnership: Bool
        if case .sessionExpired = state {
            shouldResumeOwnership = true
        } else {
            shouldResumeOwnership = false
        }
        self.session = session
        guard !accessValidationSuspended else { return }
        PlaudDeviceAgent.shared.setUserAccessToken(session.plaudUserAccessToken)
        retryCloudItems()
        if shouldResumeOwnership {
            if let checkpoint = recorderReleaseCheckpoint {
                resumeRecorderRelease(checkpoint, completion: recorderReleaseCompletionHandler())
            } else if let activeScannedDevice, PlaudDeviceAgent.shared.isConnected() {
                checkCloudOwnership(for: activeScannedDevice)
            } else if !reconnectSavedDeviceIfAvailable() {
                startScan()
            }
        }
    }

    func beginOnboarding() {
        guard !accessValidationSuspended else { return }
        autoReconnectSerial = nil
        state = .idle
    }

    var supportsFastTransfer: Bool {
        connectedDevice?.supportsWiFiFastTransfer == true
    }

    /// Explicitly asks the service to move every unfinished recorder copy in
    /// the current batch over Wi-Fi. If the file list is not yet known, the
    /// next BLE file-list response fulfills the request. This never applies to
    /// a different signed-in user or recorder generation.
    func requestManualFastTransfer() {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.requestManualFastTransfer() }
            return
        }
        guard !accessValidationSuspended,
              recorderReleaseCheckpoint == nil,
              pendingRecorderRemoval == nil,
              !cloudReleaseInFlight,
              let connectedDevice,
              connectedDevice.supportsWiFiFastTransfer else {
            fastTransferState = .unavailable(
                "Fast Wi-Fi transfer is unavailable for the connected recorder or firmware."
            )
            return
        }
        guard !PlaudDeviceAgent.shared.checkIsRecording() else {
            fastTransferState = .unavailable(
                "Stop the current recording before starting Fast Wi-Fi transfer."
            )
            return
        }
        if wifiTransferGeneration != nil { return }

        if let offer = fastTransferOffer {
            resolveFastTransferOffer(id: offer.id, useWiFi: true)
            return
        }

        manualFastTransferRequested = true
        if currentExportContext != nil, currentExportTailReceived {
            // The recorder has already sent the entire source file. Let the
            // SDK finish local decoding, then move only the remaining batch.
            fastTransferState = .checkingForRecordings
            return
        }
        if beginManualFastTransferFromQueue() { return }

        fastTransferState = .checkingForRecordings
        armManualFastTransferLookupTimeout(serialNumber: connectedDevice.serialNumber)
        guard PlaudDeviceAgent.shared.isConnected() else {
            shouldMaintainConnection = true
            autoReconnectSerial = connectedDevice.serialNumber
            scheduleReconnect(after: 1)
            return
        }
        PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
    }

    private func armManualFastTransferLookupTimeout(serialNumber: String) {
        manualFastTransferLookupWorkItem?.cancel()
        let generation = userGeneration
        let workItem = DispatchWorkItem { [weak self] in
            guard let self,
                  self.userGeneration == generation,
                  self.manualFastTransferRequested,
                  (self.connectedDevice?.serialNumber == serialNumber
                    || self.autoReconnectSerial == serialNumber) else { return }
            self.manualFastTransferRequested = false
            self.fastTransferState = .unavailable(
                "PinPoint could not refresh the recorder list in time. Automatic Bluetooth copy remains active."
            )
            self.exportNextFileIfNeeded()
        }
        manualFastTransferLookupWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: workItem)
    }

    /// Resolves only the currently visible offer. Stale taps from an old user,
    /// device, or already-expired alert are ignored.
    func resolveFastTransferOffer(id: UUID, useWiFi: Bool) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.resolveFastTransferOffer(id: id, useWiFi: useWiFi)
            }
            return
        }
        guard let offer = fastTransferOffer, offer.id == id else { return }
        let files = fastTransferOfferFiles
        clearFastTransferOffer(notifyDismissal: true)
        // A delayed tap must not override the ten-second safe default after
        // the visible countdown has already expired.
        if useWiFi, Date() < offer.expiresAt {
            beginWiFiFastTransfer(files: files)
        } else {
            files.forEach { blePreferredFiles.insert(work(for: $0)) }
            fastTransferState = .idle
            exportNextFileIfNeeded()
        }
    }

    func reconnectSavedDeviceIfAvailable() -> Bool {
        guard !accessValidationSuspended, wifiTransferGeneration == nil else { return false }
        if let checkpoint = recorderReleaseCheckpoint {
            shouldMaintainConnection = true
            autoReconnectSerial = checkpoint.serialNumber
            resumeRecorderRelease(checkpoint, completion: recorderReleaseCompletionHandler())
            return true
        }
        if let connectedDevice {
            state = .ready(connectedDevice)
            return true
        }
        guard let serial = pairedSerial, !serial.isEmpty else {
            return false
        }
        shouldMaintainConnection = true
        autoReconnectSerial = serial
        startScan()
        return true
    }

    func startScan() {
        guard !accessValidationSuspended, wifiTransferGeneration == nil else { return }
        guard session != nil else {
            state = .sessionExpired
            return
        }
        // Starting a new scan invalidates every ownership response from the
        // previous connection attempt. A newly selected device receives a new
        // attempt id in `connect`.
        connectionAttemptID = nil
        connectionTimeoutWorkItem?.cancel()
        connectionTimeoutWorkItem = nil
        activeConnectionGeneration = nil
        activeScannedDevice = nil
        cachedDevices.removeAll()
        scanGeneration = userGeneration
        state = .scanning
        PlaudDeviceAgent.shared.startScan()
    }

    func connect(_ device: ScannedPlaudDevice) {
        guard !accessValidationSuspended else { return }
        guard let raw = cachedDevices[device.serialNumber], let session else {
            state = .failed("That recorder is no longer nearby. Scan again to reconnect.")
            return
        }
        PlaudDeviceAgent.shared.stopScan()
        reconnectWorkItem?.cancel()
        // `supportWiFi` is authoritative only after this connection's pen-state
        // handshake. Never carry an earlier connection's runtime value forward.
        reportedWiFiCapabilitySerial = nil
        reportedWiFiCapability = false
        activeScannedDevice = device
        activeConnectionGeneration = userGeneration
        connectionAttemptID = UUID()
        state = .connecting(device)
        if let connectionAttemptID {
            armConnectionTimeout(
                attemptID: connectionAttemptID,
                serialNumber: device.serialNumber,
                generation: userGeneration
            )
        }
        PlaudDeviceAgent.shared.connectBleDevice(bleDevice: raw, deviceToken: session.userID)
    }

    func retryOwnershipCheck() {
        guard !accessValidationSuspended else { return }
        guard let device = activeScannedDevice else {
            startScan()
            return
        }
        // The 403 recovery path deliberately disconnects BLE. Re-enter the
        // complete scan -> secure BLE bind -> cloud ownership flow rather than
        // declaring the recorder ready from a cloud response alone.
        shouldMaintainConnection = false
        autoReconnectSerial = device.serialNumber
        startScan()
    }

    func forgetCurrentAttempt() {
        guard !accessValidationSuspended else { return }
        shouldMaintainConnection = false
        reconnectWorkItem?.cancel()
        connectionTimeoutWorkItem?.cancel()
        connectionTimeoutWorkItem = nil
        stopFastTransferForLifecycle()
        interruptActiveExportForRetry(
            message: "Recorder copy was interrupted. Reconnect to retry; the recording remains on the recorder."
        )
        PlaudDeviceAgent.shared.disconnect()
        activeScannedDevice = nil
        connectedDevice = nil
        autoReconnectSerial = nil
        connectionAttemptID = nil
        state = .idle
    }

    func disconnect() {
        tearDownCurrentUser()
        state = .idle
    }

    /// Stops every Plaud/BLE/cloud side effect while retaining the signed-in
    /// user's durable recorder association, release intent, and upload
    /// checkpoints. This is used before a stale foreground session is
    /// revalidated and immediately when the user requests sign-out.
    func suspendForAccessValidation(message: String) {
        guard session != nil else { return }
        accessValidationSuspended = true
        userGeneration &+= 1
        markedMomentRuntime?.deactivate()
        activeRecordingSessionID = nil
        invalidateLocalDataCallbacks()
        stopFastTransferForLifecycle()
        shouldMaintainConnection = false
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        depairTimeoutWorkItem?.cancel()
        depairTimeoutWorkItem = nil
        connectionTimeoutWorkItem?.cancel()
        connectionTimeoutWorkItem = nil
        fileRefreshTimer?.invalidate()
        fileRefreshTimer = nil
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        pendingRecorderRemoval = nil
        deferredRecorderReleaseCompletion = nil
        cloudReleaseInFlight = false
        pendingFiles.removeAll()
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        PlaudDeviceAgent.shared.stopScan()
        PlaudDeviceAgent.shared.stopDownloadFile()
        PlaudDeviceAgent.shared.stopSyncFile()
        PlaudDeviceAgent.shared.disconnect()
        PlaudDeviceAgent.shared.setUserAccessToken(nil)
        cloudPipelineLock.lock()
        let activePipelines = Array(cloudPipelines.values)
        cloudPipelines.removeAll()
        queuedFailedCloudRetries.removeAll()
        cloudPipelineLock.unlock()
        activePipelines.forEach { $0.cancel() }
        cachedDevices.removeAll()
        scanGeneration = nil
        activeConnectionGeneration = nil
        connectionAttemptID = nil
        activeScannedDevice = nil
        connectedDevice = nil
        autoReconnectSerial = nil
        state = .accessPaused(message)
    }

    /// Restores the existing, already initialized SDK user only after the
    /// backend has confirmed that the invitation and session remain active.
    func resumeAfterAccessValidation() {
        guard accessValidationSuspended, let session else { return }
        accessValidationSuspended = false
        userGeneration &+= 1
        if pendingDeviceWiFiCloseSerial != nil {
            // This is still the same retained user session; rebind the deferred
            // close to the newly validated callback generation.
            pendingDeviceWiFiCloseGeneration = userGeneration
        }
        if let currentUserID {
            _ = markedMomentRuntime?.activate(userID: currentUserID, userGeneration: userGeneration)
        }
        PlaudDeviceAgent.shared.delegate = self
        PlaudDeviceAgent.shared.setUserAccessToken(session.plaudUserAccessToken)
        startFileRefreshMonitor()
        retryCloudItems()
        if !reconnectSavedDeviceIfAvailable() {
            beginOnboarding()
        }
    }

    func deleteCurrentUserLocalData() throws {
        guard connectedDevice == nil,
              pairedSerial == nil,
              recorderReleaseCheckpoint == nil,
              pendingRecorderRemoval == nil,
              !cloudReleaseInFlight else {
            throw BetaLocalDataDeletionError.releaseRecorderFirst
        }
        guard currentExportContext == nil,
              pendingFiles.isEmpty,
              fastTransferOffer == nil,
              wifiTransferGeneration == nil,
              !manualFastTransferRequested else {
            throw BetaLocalDataDeletionError.copyInProgress
        }
        invalidateLocalDataCallbacks()
        cloudPipelineLock.lock()
        let activePipelines = Array(cloudPipelines.values)
        cloudPipelines.removeAll()
        queuedFailedCloudRetries.removeAll()
        cloudPipelineLock.unlock()
        activePipelines.forEach { $0.cancel() }
        guard let recordingStore else {
            throw BetaLocalDataDeletionError.storageUnavailable
        }
        try recordingStore.deleteAllLocalData()
        emitRecordings()
    }

    func retryRecording(_ recording: BetaRecording) {
        guard !accessValidationSuspended else { return }
        guard recording.status == .failed,
              let recordingStore,
              let current = recordingStore.recording(
                sessionID: recording.sessionID,
                serialNumber: recording.deviceSerialNumber
              ) else { return }
        if let audioURL = recordingStore.audioURL(for: current) {
            startCloudPipeline(
                sessionID: recording.sessionID,
                serialNumber: recording.deviceSerialNumber,
                outputPath: audioURL.path,
                queueFailedRetryIfBusy: true
            )
            return
        }
        if current.cloudCheckpoint?.canResumeWithoutLocalAudio == true {
            startCloudPipeline(
                sessionID: recording.sessionID,
                serialNumber: recording.deviceSerialNumber,
                outputPath: nil,
                queueFailedRetryIfBusy: true
            )
            return
        }

        guard recorderReleaseCheckpoint == nil,
              let association = recordingStore.recorderAssociation(),
              association.serialNumber == recording.deviceSerialNumber else {
            if !recordingStore.markNeedsSupport(
                sessionID: recording.sessionID,
                serialNumber: recording.deviceSerialNumber,
                message: "This recording is no longer on the recorder currently assigned to PinPoint. A local audio copy is required to retry it."
            ) {
                state = .failed("PinPoint could not save the retry status. Free disk space and reopen PinPoint.")
            }
            emitRecordings()
            return
        }

        // A recorder-copy failure has no local file to send to the cloud. Make
        // the visible Retry action actually reacquire that recording instead
        // of silently doing nothing.
        guard recordingStore.begin(
            sessionID: recording.sessionID,
            serialNumber: recording.deviceSerialNumber,
            duration: current.duration
        ) else {
            state = .failed("PinPoint could not save recording progress. Free disk space, then retry.")
            return
        }
        emitRecordings()
        if connectedDevice?.serialNumber == recording.deviceSerialNumber,
           PlaudDeviceAgent.shared.isConnected() {
            PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
        } else {
            shouldMaintainConnection = true
            autoReconnectSerial = recording.deviceSerialNumber
            PlaudDeviceAgent.shared.disconnect()
            connectedDevice = nil
            startScan()
        }
    }

    func removeRecorder(completion: @escaping (Result<Void, BetaRecorderRemovalError>) -> Void) {
        guard !accessValidationSuspended else {
            completion(.failure(.serviceUnavailable))
            return
        }
        guard pendingRecorderRemoval == nil else {
            completion(.failure(.releaseInProgress))
            return
        }
        guard deferredRecorderReleaseCompletion == nil else {
            completion(.failure(.releaseInProgress))
            return
        }
        guard explicitRecorderReleaseCompletion == nil else {
            completion(.failure(.releaseInProgress))
            return
        }
        guard currentExportContext == nil,
              pendingFiles.isEmpty,
              fastTransferOffer == nil,
              wifiTransferGeneration == nil,
              !manualFastTransferRequested else {
            completion(.failure(.copyInProgress))
            return
        }
        guard !PlaudDeviceAgent.shared.checkIsRecording() else {
            completion(.failure(.recordingInProgress))
            return
        }
        guard session != nil else {
            completion(.failure(.sessionExpired))
            return
        }
        if let checkpoint = recorderReleaseCheckpoint {
            explicitRecorderReleaseCompletion = completion
            resumeRecorderRelease(checkpoint, completion: recorderReleaseCompletionHandler())
            return
        }
        guard let association = recordingStore?.recorderAssociation(),
              let model = PlaudDeviceModel(serialNumber: association.serialNumber),
              model.partnerBindingType == association.deviceType else {
            completion(.failure(.notConnected))
            return
        }
        let serialNumber = association.serialNumber
        guard let recordingStore else {
            completion(.failure(.checkpointPersistence))
            return
        }
        let checkpoint = BetaRecorderReleaseCheckpoint(
            serialNumber: serialNumber,
            deviceType: model.partnerBindingType,
            phase: .requested,
            updatedAt: Date()
        )
        // This is the local half of the two-phase release. Never call Plaud
        // until the intent is durable, otherwise a crash could re-bind the
        // recorder during the next automatic reconnect.
        guard recordingStore.saveRecorderReleaseCheckpoint(checkpoint) else {
            completion(.failure(.checkpointPersistence))
            return
        }
        explicitRecorderReleaseCompletion = completion
        resumeRecorderRelease(checkpoint, completion: recorderReleaseCompletionHandler())
    }

    private func tearDownCurrentUser() {
        userGeneration &+= 1
        markedMomentRuntime?.deactivate()
        markedMomentRuntime = nil
        activeRecordingSessionID = nil
        stopFastTransferForLifecycle()
        shouldMaintainConnection = false
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        depairTimeoutWorkItem?.cancel()
        depairTimeoutWorkItem = nil
        connectionTimeoutWorkItem?.cancel()
        connectionTimeoutWorkItem = nil
        fileRefreshTimer?.invalidate()
        fileRefreshTimer = nil
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        let explicitRelease = explicitRecorderReleaseCompletion
        explicitRecorderReleaseCompletion = nil
        pendingRecorderRemoval = nil
        deferredRecorderReleaseCompletion = nil
        cloudReleaseInFlight = false
        accessValidationSuspended = true
        pendingFiles.removeAll()
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        PlaudDeviceAgent.shared.stopScan()
        PlaudDeviceAgent.shared.stopDownloadFile()
        PlaudDeviceAgent.shared.stopSyncFile()
        PlaudDeviceAgent.shared.disconnect()
        PlaudDeviceAgent.shared.setUserAccessToken(nil)
        cloudPipelineLock.lock()
        let activePipelines = Array(cloudPipelines.values)
        cloudPipelines.removeAll()
        queuedFailedCloudRetries.removeAll()
        cloudPipelineLock.unlock()
        activePipelines.forEach { $0.cancel() }
        cachedDevices.removeAll()
        scanGeneration = nil
        activeConnectionGeneration = nil
        connectionAttemptID = nil
        activeScannedDevice = nil
        connectedDevice = nil
        autoReconnectSerial = nil
        pendingDeviceWiFiCloseSerial = nil
        pendingDeviceWiFiCloseGeneration = nil
        reportedWiFiCapabilitySerial = nil
        reportedWiFiCapability = false
        session = nil
        pinpointAPI = nil
        recordingStore = nil
        currentUserID = nil
        explicitRelease?(.failure(.serviceUnavailable))
    }

    private func checkCloudOwnership(for device: ScannedPlaudDevice) {
        guard !accessValidationSuspended else { return }
        guard let session else {
            state = .sessionExpired
            return
        }
        let expectedUserID = session.userID
        let expectedGeneration = userGeneration
        guard let expectedAttemptID = connectionAttemptID,
              activeScannedDevice?.serialNumber == device.serialNumber else {
            state = .failed("PinPoint discarded a stale recorder connection. Scan again to reconnect.")
            return
        }
        state = .checkingOwnership(device)
        guard let pinpointAPI, let recordingStore else {
            state = .failed("PinPoint could not reach its recorder ownership service.")
            return
        }
        // Persist identity before the cloud side effect. A crash or ambiguous
        // response can then retry bind or release this exact recorder.
        switch recordingStore.beginRecorderAssociation(
            serialNumber: device.serialNumber,
            deviceType: device.model.partnerBindingType
        ) {
        case .identityConflict(let existing):
            shouldMaintainConnection = false
            autoReconnectSerial = existing.serialNumber
            PlaudDeviceAgent.shared.disconnect()
            clearConnectionIdentity()
            state = .failed("Release the saved recorder ending in \(existing.serialNumber.suffix(4)) before connecting a different recorder.")
            return
        case .persistenceFailed:
            state = .failed("PinPoint could not safely begin recorder setup. Free disk space, then try again.")
            return
        case .ready:
            break
        }
        pinpointAPI.bindRecorder(
            serialNumber: device.serialNumber,
            model: device.model,
            sessionToken: session.sessionToken
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.userGeneration == expectedGeneration,
                      self.currentUserID == expectedUserID,
                      self.session?.userID == expectedUserID,
                      self.activeConnectionGeneration == expectedGeneration,
                      self.connectionAttemptID == expectedAttemptID,
                      self.activeScannedDevice?.serialNumber == device.serialNumber else { return }
                switch result {
                case .success:
                    self.shouldMaintainConnection = true
                    self.reconnectWorkItem?.cancel()
                    guard recordingStore.markRecorderAssociationBound(
                        serialNumber: device.serialNumber,
                        deviceType: device.model.partnerBindingType
                    ) else {
                        self.shouldMaintainConnection = false
                        self.state = .failed("PinPoint could not safely remember this recorder. Free disk space, then try again.")
                        return
                    }
                    if let key = self.pairedSerialKey {
                        self.defaults.removeObject(forKey: key)
                        _ = self.defaults.synchronize()
                    }
                    self.autoReconnectSerial = device.serialNumber
                    guard PlaudDeviceAgent.shared.isConnected() else {
                        self.state = .idle
                        self.scheduleReconnect(after: 1)
                        return
                    }
                    let raw = self.liveSDKDevice(serialNumber: device.serialNumber)
                    let connected = ConnectedPlaudDevice(
                        serialNumber: device.serialNumber,
                        model: device.model,
                        batteryLevel: raw.map(\.power),
                        isCharging: raw?.isCharging ?? false,
                        storageUsed: nil,
                        storageTotal: nil,
                        supportsWiFiFastTransfer: self.cachedWiFiCapability(
                            serialNumber: device.serialNumber
                        )
                    )
                    self.connectedDevice = connected
                    self.state = .ready(connected)
                    self.activeRecordingSessionID = PlaudDeviceAgent.shared.checkIsRecording()
                        ? PlaudDeviceAgent.shared.getCurrentSessionID()
                        : nil
                    self.refreshMarkedMoments(deviceConnected: true)
                    self.finishBluetoothRestoreIfNeeded()
                    PlaudDeviceAgent.shared.getState()
                    PlaudDeviceAgent.shared.getStorage()
                    PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
                    self.retryCloudItems()
                case .failure(.boundElsewhere):
                    self.shouldMaintainConnection = false
                    self.reconnectWorkItem?.cancel()
                    PlaudDeviceAgent.shared.disconnect()
                    if recordingStore.clearRecorderAssociation(
                        matchingSerialNumber: device.serialNumber,
                        deviceType: device.model.partnerBindingType
                    ) {
                        self.state = .boundElsewhere(device)
                    } else {
                        self.state = .failed("PinPoint could not safely finish the ownership check. Free disk space, then try again.")
                    }
                case .failure(.expiredSession):
                    self.shouldMaintainConnection = false
                    self.state = .sessionExpired
                    self.onSessionExpired?()
                case .failure(.unsupportedDevice):
                    self.state = .failed("This Plaud model is not supported by this Beta.")
                case .failure(.unreachable):
                    self.shouldMaintainConnection = true
                    self.autoReconnectSerial = device.serialNumber
                    PlaudDeviceAgent.shared.disconnect()
                    self.state = .failed("PinPoint could not check recorder ownership. Check your connection and try again.")
                    self.scheduleReconnect(after: 10)
                case .failure(.server(let status, _)) where status == 409:
                    // A lifecycle conflict is durable server state rather than
                    // a transient transport error. Preserve the association
                    // and stop reconnecting until a person resolves it.
                    self.shouldMaintainConnection = false
                    self.reconnectWorkItem?.cancel()
                    PlaudDeviceAgent.shared.disconnect()
                    self.state = .failed("This recorder setup needs review before PinPoint can continue. Release the saved recorder or contact support.")
                case .failure(.server):
                    self.shouldMaintainConnection = true
                    self.autoReconnectSerial = device.serialNumber
                    PlaudDeviceAgent.shared.disconnect()
                    self.state = .failed("Plaud could not finish the recorder setup. Please try again.")
                    self.scheduleReconnect(after: 10)
                }
            }
        }
    }

    private func emitState() {
        let snapshot = state
        DispatchQueue.main.async { [weak self] in self?.onStateChange?(snapshot) }
    }

    private func scheduleReconnect(after delay: TimeInterval = 3) {
        guard shouldMaintainConnection, !accessValidationSuspended else { return }
        reconnectWorkItem?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.shouldMaintainConnection else { return }
            if let durableSerial = self.recorderReleaseCheckpoint?.serialNumber ?? self.pairedSerial {
                self.autoReconnectSerial = durableSerial
            }
            self.startScan()
        }
        reconnectWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func armConnectionTimeout(
        attemptID: UUID,
        serialNumber: String,
        generation: Int
    ) {
        connectionTimeoutWorkItem?.cancel()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.userGeneration == generation,
                  self.connectionAttemptID == attemptID,
                  case .connecting(let device) = self.state,
                  device.serialNumber == serialNumber else { return }
            self.connectionAttemptID = nil
            self.activeConnectionGeneration = nil
            PlaudDeviceAgent.shared.disconnect()
            self.state = .failed("The recorder did not finish its secure connection. Bring it closer and try again.")
            self.scheduleReconnect(after: 2)
        }
        connectionTimeoutWorkItem = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: timeout)
    }

    private func startFileRefreshMonitor() {
        fileRefreshTimer?.invalidate()
        let timer = Timer(timeInterval: 120, repeats: true) { [weak self] _ in
            guard let self,
                  !self.accessValidationSuspended,
                  self.session != nil,
                  self.recorderReleaseCheckpoint == nil,
                  self.pendingRecorderRemoval == nil,
                  !self.cloudReleaseInFlight,
                  self.connectedDevice != nil,
                  self.currentExportContext == nil,
                  self.pendingFiles.isEmpty,
                  self.fastTransferOffer == nil,
                  self.wifiTransferGeneration == nil,
                  !self.manualFastTransferRequested,
                  PlaudDeviceAgent.shared.isConnected(),
                  !PlaudDeviceAgent.shared.checkIsRecording() else { return }
            PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
        }
        fileRefreshTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func clearConnectionIdentity() {
        refreshMarkedMoments(deviceConnected: false)
        activeRecordingSessionID = nil
        connectionTimeoutWorkItem?.cancel()
        connectionTimeoutWorkItem = nil
        connectionAttemptID = nil
        activeConnectionGeneration = nil
        scanGeneration = nil
        activeScannedDevice = nil
        connectedDevice = nil
        cachedDevices.removeAll()
    }

    private func liveSDKDevice(serialNumber: String) -> BleDevice? {
        guard let raw = PlaudDeviceAgent.shared.recentConnectDevice,
              raw.serialNumber == serialNumber else { return nil }
        return raw
    }

    private func cachedWiFiCapability(serialNumber: String) -> Bool {
        return reportedWiFiCapabilitySerial == serialNumber
            ? reportedWiFiCapability
            : false
    }

    /// Plaud documents `blePenState` as handshake completion. Read the runtime
    /// capability only there, and only from the SDK device for this connection.
    private func refreshWiFiCapabilityAfterHandshake(
        serialNumber: String,
        sdkDevice: BleDevice
    ) -> Bool {
        guard sdkDevice.serialNumber == serialNumber,
              liveSDKDevice(serialNumber: serialNumber) != nil,
              activeScannedDevice?.serialNumber == serialNumber
                || connectedDevice?.serialNumber == serialNumber else {
            return false
        }
        reportedWiFiCapabilitySerial = serialNumber
        reportedWiFiCapability = sdkDevice.supportWiFi
        return sdkDevice.supportWiFi
    }

    private func closePendingDeviceWiFiIfNeeded(serialNumber: String) {
        guard pendingDeviceWiFiCloseGeneration == userGeneration,
              pendingDeviceWiFiCloseSerial == serialNumber,
              PlaudDeviceAgent.shared.isConnected(),
              liveSDKDevice(serialNumber: serialNumber) != nil else { return }
        PlaudDeviceAgent.shared.setDeviceWiFi(open: false)
        pendingDeviceWiFiCloseSerial = nil
        pendingDeviceWiFiCloseGeneration = nil
    }

    private func deferDeviceWiFiClose(serialNumber: String?) {
        guard let serialNumber, !serialNumber.isEmpty else { return }
        pendingDeviceWiFiCloseSerial = serialNumber
        pendingDeviceWiFiCloseGeneration = userGeneration
    }

    private var pairedSerialKey: String? {
        guard let currentUserID else { return nil }
        return Keys.pairedSerialPrefix + BetaRecordingStore.userDirectoryName(for: currentUserID)
    }

    private var legacyReleasePendingKey: String? {
        guard let currentUserID else { return nil }
        return Keys.legacyReleasePendingPrefix + BetaRecordingStore.userDirectoryName(for: currentUserID)
    }

    private var pairedSerial: String? {
        if let serialNumber = recordingStore?.recorderAssociation()?.serialNumber {
            return serialNumber
        }
        return pairedSerialKey.flatMap { defaults.string(forKey: $0) }
    }

    private var recorderReleaseCheckpoint: BetaRecorderReleaseCheckpoint? {
        recordingStore?.recorderReleaseCheckpoint()
    }

    private func migrateLegacyRecorderAssociationIfNeeded() {
        guard recordingStore?.recorderAssociation() == nil,
              let key = pairedSerialKey,
              let serialNumber = defaults.string(forKey: key),
              let model = PlaudDeviceModel(serialNumber: serialNumber),
              let recordingStore else { return }
        if case .ready = recordingStore.beginRecorderAssociation(
            serialNumber: serialNumber,
            deviceType: model.partnerBindingType
        ), recordingStore.markRecorderAssociationBound(
            serialNumber: serialNumber,
            deviceType: model.partnerBindingType
        ) {
            defaults.removeObject(forKey: key)
            _ = defaults.synchronize()
        }
    }

    private func migrateLegacyReleaseCheckpointIfNeeded() {
        guard recorderReleaseCheckpoint == nil,
              let key = legacyReleasePendingKey,
              let serialNumber = defaults.string(forKey: key),
              let model = PlaudDeviceModel(serialNumber: serialNumber),
              let recordingStore else { return }
        let checkpoint = BetaRecorderReleaseCheckpoint(
            serialNumber: serialNumber,
            deviceType: model.partnerBindingType,
            phase: .cloudConfirmed,
            updatedAt: Date()
        )
        if recordingStore.saveRecorderReleaseCheckpoint(checkpoint) {
            defaults.removeObject(forKey: key)
        }
    }

    private func resumeRecorderRelease(
        _ checkpoint: BetaRecorderReleaseCheckpoint,
        completion: @escaping (Result<Void, BetaRecorderRemovalError>) -> Void
    ) {
        if checkpoint.phase == .localDepaired {
            finalizeRecorderRelease(completion: completion)
            return
        }
        guard let model = PlaudDeviceModel(serialNumber: checkpoint.serialNumber),
              model.partnerBindingType == checkpoint.deviceType,
              let session,
              let recordingStore else {
            completion(.failure(.checkpointPersistence))
            return
        }
        let expectedGeneration = userGeneration
        let expectedUserID = session.userID

        if checkpoint.phase == .cloudConfirmed {
            continueLocalRecorderDepair(
                checkpoint,
                userID: expectedUserID,
                generation: expectedGeneration,
                completion: completion
            )
            return
        }
        guard !cloudReleaseInFlight else {
            completion(.failure(.releaseInProgress))
            return
        }
        guard let pinpointAPI else {
            completion(.failure(.serviceUnavailable))
            return
        }
        cloudReleaseInFlight = true
        pinpointAPI.unbindRecorder(
            serialNumber: checkpoint.serialNumber,
            model: model,
            sessionToken: session.sessionToken
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self,
                      self.userGeneration == expectedGeneration,
                      self.currentUserID == expectedUserID else { return }
                self.cloudReleaseInFlight = false
                switch result {
                case .success:
                    var confirmed = checkpoint
                    confirmed.phase = .cloudConfirmed
                    confirmed.updatedAt = Date()
                    guard recordingStore.saveRecorderReleaseCheckpoint(confirmed) else {
                        // `requested` is still durable. A later retry safely
                        // repeats the idempotent backend unbind.
                        completion(.failure(.checkpointPersistence))
                        return
                    }
                    self.continueLocalRecorderDepair(
                        confirmed,
                        userID: expectedUserID,
                        generation: expectedGeneration,
                        completion: completion
                    )
                case .failure(.expiredSession):
                    completion(.failure(.sessionExpired))
                    self.onSessionExpired?()
                case .failure:
                    completion(.failure(.serviceUnavailable))
                }
            }
        }
    }

    private func continueLocalRecorderDepair(
        _ checkpoint: BetaRecorderReleaseCheckpoint,
        userID: String,
        generation: Int,
        completion: @escaping (Result<Void, BetaRecorderRemovalError>) -> Void
    ) {
        shouldMaintainConnection = true
        autoReconnectSerial = checkpoint.serialNumber
        if connectedDevice?.serialNumber == checkpoint.serialNumber,
           PlaudDeviceAgent.shared.isConnected() {
            shouldMaintainConnection = false
            reconnectWorkItem?.cancel()
            beginLocalRecorderDepair(
                serialNumber: checkpoint.serialNumber,
                userID: userID,
                generation: generation,
                completion: completion
            )
        } else {
            deferredRecorderReleaseCompletion = completion
            startScan()
        }
    }

    private func handleAutomaticRecorderRelease(
        _ result: Result<Void, BetaRecorderRemovalError>
    ) {
        if case .failure(let error) = result {
            if error == .sessionExpired {
                state = .sessionExpired
            } else if error != .releaseInProgress {
                state = .failed(error.localizedDescription)
            }
        }
    }

    private func recorderReleaseCompletionHandler()
        -> (Result<Void, BetaRecorderRemovalError>) -> Void {
        { [weak self] result in
            guard let self else { return }
            if let completion = self.explicitRecorderReleaseCompletion {
                self.explicitRecorderReleaseCompletion = nil
                completion(result)
            } else {
                self.handleAutomaticRecorderRelease(result)
            }
        }
    }

    private func beginLocalRecorderDepair(
        serialNumber: String,
        userID: String,
        generation: Int,
        completion: @escaping (Result<Void, BetaRecorderRemovalError>) -> Void
    ) {
        guard pendingRecorderRemoval == nil else {
            completion(.failure(.releaseInProgress))
            return
        }
        // Cloud unbind and crash recovery are asynchronous. The recorder may
        // have started a new capture since the user first chose Release, so
        // re-check at the final local side effect and retain cloudConfirmed
        // for an automatic retry after the recording stops.
        guard !PlaudDeviceAgent.shared.checkIsRecording() else {
            shouldMaintainConnection = true
            autoReconnectSerial = serialNumber
            completion(.failure(.recordingInProgress))
            return
        }
        pendingRecorderRemoval = PendingRecorderRemoval(
            serialNumber: serialNumber,
            userID: userID,
            generation: generation,
            completion: completion
        )
        let timeout = DispatchWorkItem { [weak self] in
            self?.finishRecorderRemoval(status: nil)
        }
        depairTimeoutWorkItem = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: timeout)
        PlaudDeviceAgent.shared.depair(clear: true)
    }

    private func finishRecorderRemoval(status: Int?) {
        guard let pending = pendingRecorderRemoval,
              pending.generation == userGeneration,
              pending.userID == currentUserID else { return }
        pendingRecorderRemoval = nil
        depairTimeoutWorkItem?.cancel()
        depairTimeoutWorkItem = nil
        guard status == 0 else {
            shouldMaintainConnection = true
            pending.completion(.failure(.localDepairUnconfirmed))
            return
        }
        guard var checkpoint = recorderReleaseCheckpoint,
              checkpoint.serialNumber == pending.serialNumber,
              let recordingStore else {
            pending.completion(.failure(.checkpointPersistence))
            return
        }
        checkpoint.phase = .localDepaired
        checkpoint.updatedAt = Date()
        guard recordingStore.saveRecorderReleaseCheckpoint(checkpoint) else {
            pending.completion(.failure(.checkpointPersistence))
            return
        }
        finalizeRecorderRelease(completion: pending.completion)
    }

    private func finalizeRecorderRelease(
        completion: @escaping (Result<Void, BetaRecorderRemovalError>) -> Void
    ) {
        guard let checkpoint = recorderReleaseCheckpoint,
              checkpoint.phase == .localDepaired,
              let recordingStore,
              recordingStore.clearRecorderAssociation(
                matchingSerialNumber: checkpoint.serialNumber,
                deviceType: checkpoint.deviceType
              ) else {
            completion(.failure(.checkpointPersistence))
            return
        }
        if let key = pairedSerialKey {
            defaults.removeObject(forKey: key)
        }
        if let key = legacyReleasePendingKey {
            defaults.removeObject(forKey: key)
        }
        guard defaults.synchronize(),
              pairedSerialKey.flatMap({ defaults.string(forKey: $0) }) == nil else {
            completion(.failure(.checkpointPersistence))
            return
        }
        // The checkpoint is removed last. Every crash point before this line
        // resumes release and can never call the cloud bind route.
        guard recordingStore.clearRecorderReleaseCheckpoint() else {
            completion(.failure(.checkpointPersistence))
            return
        }
        cachedDevices.removeAll()
        activeScannedDevice = nil
        connectedDevice = nil
        autoReconnectSerial = nil
        scanGeneration = nil
        activeConnectionGeneration = nil
        state = .idle
        completion(.success(()))
    }
}

enum BetaRecorderRemovalError: LocalizedError, Equatable {
    case releaseInProgress
    case copyInProgress
    case recordingInProgress
    case notConnected
    case sessionExpired
    case serviceUnavailable
    case localDepairUnconfirmed
    case checkpointPersistence

    var errorDescription: String? {
        switch self {
        case .releaseInProgress:
            return "PinPoint is already releasing this recorder. Wait a moment."
        case .copyInProgress:
            return "Wait for the current recorder copy to finish, then try again."
        case .recordingInProgress:
            return "Stop the current recording before releasing this recorder. The recording will stay on the device."
        case .notConnected:
            return "PinPoint has no saved recorder to release for this account."
        case .sessionExpired:
            return "Sign in again before releasing this recorder."
        case .serviceUnavailable:
            return "PinPoint could not confirm the cloud release. It will not bind this recorder again; try Release recorder later."
        case .localDepairUnconfirmed:
            return "Plaud Cloud released the recorder, but local unpairing was not confirmed. Keep it nearby and try Release recorder again."
        case .checkpointPersistence:
            return "PinPoint could not safely save the recorder release. Free disk space, then try again."
        }
    }
}

enum BetaLocalDataDeletionError: LocalizedError {
    case copyInProgress
    case releaseRecorderFirst
    case storageUnavailable

    var errorDescription: String? {
        switch self {
        case .copyInProgress:
            return "Wait for the current recorder copy to finish before deleting local data."
        case .releaseRecorderFirst:
            return "Release the recorder from PinPoint first. This prevents deleted recordings from being copied back from the recorder."
        case .storageUnavailable:
            return "PinPoint could not open its protected local storage, so nothing was deleted. Quit the app and contact support."
        }
    }
}

extension BetaDeviceService: PlaudDeviceAgentProtocol {
    func bleDepair(_ status: Int) {
        DispatchQueue.main.async { [weak self] in
            self?.finishRecorderRemoval(status: status)
        }
    }

    func blePenState(
        state: Int,
        privacy: Int,
        keyState: Int,
        uDisk: Int,
        findMyToken: Int,
        hasSndpKey: Int,
        deviceAccessToken: Int
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration else { return }
            let serialNumber: String
            switch self.state {
            case .checkingOwnership(let device):
                guard self.activeScannedDevice?.serialNumber == device.serialNumber else { return }
                serialNumber = device.serialNumber
            case .ready(let device):
                guard self.connectedDevice?.serialNumber == device.serialNumber else { return }
                serialNumber = device.serialNumber
            default:
                return
            }
            guard
                  let raw = self.liveSDKDevice(serialNumber: serialNumber) else { return }
            let supportsWiFi = self.refreshWiFiCapabilityAfterHandshake(
                serialNumber: serialNumber,
                sdkDevice: raw
            )
            self.closePendingDeviceWiFiIfNeeded(serialNumber: serialNumber)
            self.updateConnected { device in
                device.batteryLevel = raw.power
                device.isCharging = raw.isCharging
                device.supportsWiFiFastTransfer = supportsWiFi
            }
        }
    }

    func bleScanResult(bleDevices: [BleDevice]) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.scanGeneration == self.userGeneration,
                  case .scanning = self.state else { return }
            let supported = bleDevices.compactMap { raw -> (BleDevice, ScannedPlaudDevice)? in
                guard let model = PlaudDeviceModel(serialNumber: raw.serialNumber) else { return nil }
                return (raw, ScannedPlaudDevice(
                    serialNumber: raw.serialNumber,
                    model: model,
                    rssi: Int(raw.rssi)
                ))
            }
            self.cachedDevices = Dictionary(
                supported.map { ($0.1.serialNumber, $0.0) },
                uniquingKeysWith: { first, _ in first }
            )
            let visible = supported.map { $0.1 }.sorted { $0.rssi > $1.rssi }
            if let serial = self.autoReconnectSerial,
               let match = visible.first(where: { $0.serialNumber == serial }),
               case .scanning = self.state {
                self.connect(match)
            } else if self.autoReconnectSerial == nil, !visible.isEmpty {
                self.state = .discovered(visible)
            }
        }
    }

    func bleScanOverTime() {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.scanGeneration == self.userGeneration else { return }
            if case .scanning = self.state {
                if let checkpoint = self.recorderReleaseCheckpoint,
                   checkpoint.phase == .cloudConfirmed,
                   let model = PlaudDeviceModel(serialNumber: checkpoint.serialNumber) {
                    self.state = .releaseWaiting(model)
                } else {
                    self.state = .noDevicesFound
                }
                self.scheduleReconnect(after: 5)
            }
        }
    }

    func bleState(powered: Bool) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            guard !self.accessValidationSuspended else { return }
            if powered {
                if self.shouldMaintainConnection, case .bluetoothUnavailable = self.state {
                    self.scheduleReconnect(after: 1)
                }
            } else {
                if self.wifiTransferGeneration != nil {
                    self.failWiFiFastTransfer(
                        "Bluetooth turned off while the recorder was preparing Fast Wi-Fi transfer."
                    )
                }
                self.interruptActiveExportForRetry(
                    message: "Bluetooth turned off during recorder copy. Turn it back on to retry; the recording remains on the recorder."
                )
                self.clearConnectionIdentity()
                self.state = .bluetoothUnavailable
            }
        }
    }

    func bleConnectState(state: Int) {
        guard state != 1 else { return }
        if state == 0 {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                guard !self.accessValidationSuspended else { return }
                // NotePin S releases its BLE transport while its temporary
                // Wi-Fi channel is active. That state machine owns the
                // expected disconnect and later restores BLE.
                guard self.wifiTransferGeneration == nil else {
                    self.deferDeviceWiFiClose(
                        serialNumber: self.connectedDevice?.serialNumber
                            ?? self.wifiRequestedFiles.keys.first?.serialNumber
                    )
                    return
                }
                self.interruptActiveExportForRetry(
                    message: "Recorder copy was interrupted by a disconnect. PinPoint will retry after reconnecting."
                )
                let wasConnecting: Bool
                if case .connecting = self.state { wasConnecting = true } else { wasConnecting = false }
                self.clearConnectionIdentity()
                if self.shouldMaintainConnection {
                    self.state = .idle
                    self.scheduleReconnect()
                } else if wasConnecting {
                    self.state = .failed("The recorder disconnected before its secure connection finished. Try again.")
                } else if case .failed = self.state {
                    // Preserve the actionable ownership/storage failure that
                    // intentionally caused this disconnect.
                } else {
                    self.state = .idle
                }
            }
        } else if state == 2 || state < 0 {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                guard !self.accessValidationSuspended else { return }
                guard self.wifiTransferGeneration == nil else {
                    self.deferDeviceWiFiClose(
                        serialNumber: self.connectedDevice?.serialNumber
                            ?? self.wifiRequestedFiles.keys.first?.serialNumber
                    )
                    return
                }
                self.interruptActiveExportForRetry(
                    message: "Recorder copy was interrupted by a connection failure. PinPoint will retry after reconnecting."
                )
                self.clearConnectionIdentity()
                self.state = .failed("The secure Bluetooth connection failed. Bring the recorder closer and try again.")
                self.scheduleReconnect()
            }
        }
    }

    func bleBind(sn: String?, status: Int, protVersion: Int, timezone: Int) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration,
                  self.connectionAttemptID != nil,
                  case .connecting(let expectedDevice) = self.state else { return }
            if let callbackSerial = sn,
               callbackSerial != expectedDevice.serialNumber {
                return
            }
            // A bind callback can arrive after the user has already selected a
            // different recorder. Ignore it; never apply A's handshake to B.
            guard status == 0,
                  let serial = sn,
                  serial == expectedDevice.serialNumber,
                  self.activeScannedDevice?.serialNumber == serial,
                  let model = PlaudDeviceModel(serialNumber: serial),
                  model == expectedDevice.model else {
                let shouldRetrySavedRecorder = self.shouldMaintainConnection
                    || self.pairedSerial == expectedDevice.serialNumber
                    || self.recorderReleaseCheckpoint?.serialNumber == expectedDevice.serialNumber
                PlaudDeviceAgent.shared.disconnect()
                self.clearConnectionIdentity()
                self.shouldMaintainConnection = shouldRetrySavedRecorder
                self.autoReconnectSerial = shouldRetrySavedRecorder
                    ? expectedDevice.serialNumber
                    : nil
                self.state = .failed(
                    shouldRetrySavedRecorder
                        ? "The saved recorder did not complete its secure connection. PinPoint will retry."
                        : "The recorder did not complete its secure connection. Scan again or choose another recorder."
                )
                if shouldRetrySavedRecorder {
                    self.scheduleReconnect(after: 2)
                }
                return
            }
            self.connectionTimeoutWorkItem?.cancel()
            self.connectionTimeoutWorkItem = nil
            let device = expectedDevice
            let raw = self.liveSDKDevice(serialNumber: serial)
            self.closePendingDeviceWiFiIfNeeded(serialNumber: serial)
            if let checkpoint = self.recorderReleaseCheckpoint,
               checkpoint.serialNumber == serial {
                self.connectedDevice = ConnectedPlaudDevice(
                    serialNumber: serial,
                    model: model,
                    batteryLevel: raw?.power,
                    isCharging: raw?.isCharging ?? false,
                    storageUsed: nil,
                    storageTotal: nil,
                    supportsWiFiFastTransfer: self.cachedWiFiCapability(
                        serialNumber: serial
                    )
                )
                let deferredCompletion = self.deferredRecorderReleaseCompletion
                self.deferredRecorderReleaseCompletion = nil
                self.resumeRecorderRelease(checkpoint) { [weak self] result in
                    if let deferredCompletion {
                        deferredCompletion(result)
                    } else {
                        self?.handleAutomaticRecorderRelease(result)
                    }
                }
                return
            }
            self.checkCloudOwnership(for: device)
        }
    }

    func blePowerChange(power: Int, oldPower: Int) {
        updateConnected { $0.batteryLevel = power }
    }

    func bleChargingState(isCharging: Bool, level: Int) {
        updateConnected {
            $0.isCharging = isCharging
            $0.batteryLevel = level
        }
    }

    func bleStorage(total: Int, free: Int, duration: Int) {
        updateConnected {
            $0.storageTotal = total
            $0.storageUsed = max(0, total - free)
        }
    }

    func bleSyncFileTail(sessionId: Int, crc: Int) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.currentExportContext?.sessionID == sessionId else { return }
            self.currentExportTailReceived = true
        }
    }

    func bleWiFiOpen(_ status: Int, _ wifiName: String, _ wholeName: String, _ wifiPass: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration != nil,
                  self.wifiPhase == .openingDeviceChannel else { return }
            guard status == 0 else {
                self.failWiFiFastTransfer(
                    "The recorder refused to open Fast Wi-Fi (status \(status))."
                )
                return
            }
            let ssid = wholeName.isEmpty ? wifiName : wholeName
            guard !ssid.isEmpty else {
                self.failWiFiFastTransfer("The recorder returned an empty Wi-Fi network name.")
                return
            }
            PlaudWiFiAgent.shared.bleDevice = BleAgent.shared.bleDevice
            PlaudWiFiAgent.shared.delegate = self.wifiDelegateProxy
            self.wifiPhase = .joiningNetwork
            let generation = self.wifiTransferGeneration
            // Plaud's reference flow gives the recorder time to finish
            // bringing up its temporary channel before association.
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let self,
                      self.wifiTransferGeneration == generation else { return }
                // This is the only public-SDK association path. It is fully
                // supported on iPhone/iPad. A Designed-for-iPad Mac that does
                // not permit NEHotspotConfiguration will fail closed and
                // resume the same batch over BLE; no private helper secret is
                // inherited from the Personal target.
                PlaudWiFiAgent.shared.connectWifi(ssid, wifiPass, 180)
            }
        }
    }

    func bleWiFiClose(_ status: Int) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.wifiTransferGeneration != nil else { return }
            self.failWiFiFastTransfer(
                "The recorder closed Fast Wi-Fi early (status \(status))."
            )
        }
    }

    func bleRecordStart(sessionId: Int, start: Int, status: Int, scene: Int, startTime: Int, reason: Int) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration,
                  self.connectedDevice != nil else { return }
            self.activeRecordingSessionID = sessionId
            self.refreshMarkedMoments(deviceConnected: true)
        }
    }

    func bleRecordStop(sessionId: Int, reason: Int, fileExist: Bool, fileSize: Int) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration,
                  self.connectedDevice != nil else { return }
            if self.activeRecordingSessionID == sessionId {
                self.activeRecordingSessionID = nil
                self.refreshMarkedMoments(deviceConnected: true)
            }
            if let checkpoint = self.recorderReleaseCheckpoint,
               checkpoint.phase == .cloudConfirmed,
               checkpoint.serialNumber == self.connectedDevice?.serialNumber {
                self.resumeRecorderRelease(
                    checkpoint,
                    completion: self.recorderReleaseCompletionHandler()
                )
                return
            }
            guard fileExist else { return }
            let generation = self.userGeneration
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                guard let self,
                      !self.accessValidationSuspended,
                      self.userGeneration == generation,
                      self.activeConnectionGeneration == generation,
                      self.connectedDevice != nil,
                      PlaudDeviceAgent.shared.isConnected() else { return }
                PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
            }
        }
    }

    func bleFileList(bleFiles: [BleFile]) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration,
                  self.recorderReleaseCheckpoint == nil,
                  self.pendingRecorderRemoval == nil,
                  !self.cloudReleaseInFlight,
                  let connectedSerial = self.connectedDevice?.serialNumber,
                  case .ready(let readyDevice) = self.state,
                  readyDevice.serialNumber == connectedSerial,
                  PlaudDeviceAgent.shared.isConnected(),
                  let recordingStore = self.recordingStore else { return }
            self.manualFastTransferLookupWorkItem?.cancel()
            self.manualFastTransferLookupWorkItem = nil
            let activeSessionID = PlaudDeviceAgent.shared.checkIsRecording()
                ? PlaudDeviceAgent.shared.getCurrentSessionID()
                : nil
            var seenFiles = Set<BetaCloudWork>()
            let newFiles = bleFiles
                .filter {
                    $0.sn == connectedSerial
                        && $0.sessionId != activeSessionID
                        && recordingStore.needsCopy(sessionID: $0.sessionId, serialNumber: $0.sn)
                }
                .filter {
                    seenFiles.insert(BetaCloudWork(
                        sessionID: $0.sessionId,
                        serialNumber: $0.sn
                    )).inserted
                }
                .sorted { $0.sessionId < $1.sessionId }
            var durableFiles: [BleFile] = []
            var persistenceFailed = false
            newFiles.forEach { file in
                if recordingStore.begin(
                    sessionID: file.sessionId,
                    serialNumber: file.sn,
                    duration: TimeInterval(file.duration()) / 1000
                ) {
                    durableFiles.append(file)
                } else {
                    persistenceFailed = true
                }
            }
            self.pendingFiles.append(contentsOf: durableFiles.filter { candidate in
                !self.pendingFiles.contains {
                    $0.sessionId == candidate.sessionId && $0.sn == candidate.sn
                }
                    && !(self.currentExportContext?.sessionID == candidate.sessionId
                        && self.currentExportContext?.serialNumber == candidate.sn)
                    && self.wifiRequestedFiles[self.work(for: candidate)] == nil
            })
            self.emitRecordings()
            self.activeRecordingSessionID = activeSessionID
            self.refreshMarkedMoments(deviceConnected: true)
            if persistenceFailed {
                self.state = .failed("PinPoint could not save new recording metadata. The recording remains on the recorder; free disk space and reconnect to retry.")
            }
            if self.manualFastTransferRequested {
                if self.currentExportContext != nil,
                   self.currentExportTailReceived {
                    // A late file-list refresh must not discard the person's
                    // explicit request while the SDK is finishing local decode.
                    self.fastTransferState = .checkingForRecordings
                    return
                }
                if self.beginManualFastTransferFromQueue() { return }
                self.manualFastTransferRequested = false
                self.fastTransferState = .idle
            } else {
                self.offerFastTransferIfNeeded()
            }
            self.exportNextFileIfNeeded()
        }
    }

    private func exportNextFileIfNeeded() {
        guard !accessValidationSuspended,
              let recordingStore,
              currentExportContext == nil,
              fastTransferOffer == nil,
              wifiTransferGeneration == nil,
              locationPermissionFiles.isEmpty,
              !manualFastTransferRequested,
              !pendingFiles.isEmpty,
              PlaudDeviceAgent.shared.isConnected() else { return }
        let file = pendingFiles.removeFirst()
        let context = BetaAudioExportContext(
            token: UUID(),
            userGeneration: userGeneration,
            localDataGeneration: currentLocalDataGeneration(),
            sessionID: file.sessionId,
            serialNumber: file.sn
        )
        let callback = BetaAudioExportCallback(service: self, context: context)
        currentExportFile = file
        currentExportContext = context
        currentExportCallback = callback
        currentExportTailReceived = false
        lastExportProgress = nil
        armExportWatchdog(for: context)
        PlaudDeviceAgent.shared.exportAudio(
            sessionId: file.sessionId,
            outputDir: recordingStore.audioDirectory(serialNumber: file.sn).path,
            format: .mp3,
            channels: 1,
            callback: callback
        )
    }

    private func work(for file: BleFile) -> BetaCloudWork {
        BetaCloudWork(sessionID: file.sessionId, serialNumber: file.sn)
    }

    private func offerFastTransferIfNeeded() {
        guard !accessValidationSuspended,
              wifiTransferGeneration == nil,
              currentExportContext == nil,
              locationPermissionFiles.isEmpty,
              !manualFastTransferRequested,
              let connectedDevice,
              let recordingStore else { return }

        let activeSessionID = PlaudDeviceAgent.shared.checkIsRecording()
            ? PlaudDeviceAgent.shared.getCurrentSessionID()
            : nil
        var seen = Set<BetaCloudWork>()
        let candidates = pendingFiles.filter { file in
            let identity = work(for: file)
            return file.sn == connectedDevice.serialNumber
                && file.sessionId != activeSessionID
                && !blePreferredFiles.contains(identity)
                && recordingStore.needsCopy(
                    sessionID: file.sessionId,
                    serialNumber: file.sn
                )
                && seen.insert(identity).inserted
        }
        guard !candidates.isEmpty else { return }

        guard connectedDevice.supportsWiFiFastTransfer else {
            candidates.forEach { blePreferredFiles.insert(work(for: $0)) }
            return
        }

        if let existing = fastTransferOffer {
            var combined = Dictionary(
                uniqueKeysWithValues: fastTransferOfferFiles.map { (work(for: $0), $0) }
            )
            candidates.forEach { combined[work(for: $0)] = $0 }
            fastTransferOfferFiles = combined.values.sorted { $0.sessionId < $1.sessionId }
            let updated = BetaFastTransferOffer(
                id: existing.id,
                recordingCount: fastTransferOfferFiles.count,
                totalDuration: fastTransferOfferFiles.reduce(0) {
                    $0 + TimeInterval($1.duration()) / 1000
                },
                expiresAt: existing.expiresAt
            )
            fastTransferOffer = updated
            fastTransferState = .waitingForDecision(updated)
            onFastTransferOffer?(updated)
            return
        }

        let offer = BetaFastTransferOffer(
            id: UUID(),
            recordingCount: candidates.count,
            totalDuration: candidates.reduce(0) {
                $0 + TimeInterval($1.duration()) / 1000
            },
            expiresAt: Date().addingTimeInterval(10)
        )
        fastTransferOffer = offer
        fastTransferOfferFiles = candidates
        fastTransferState = .waitingForDecision(offer)
        onFastTransferOffer?(offer)

        let generation = userGeneration
        let timeout = DispatchWorkItem { [weak self] in
            guard let self,
                  self.userGeneration == generation,
                  self.fastTransferOffer?.id == offer.id else { return }
            self.resolveFastTransferOffer(id: offer.id, useWiFi: false)
        }
        fastTransferDecisionWorkItem = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 10, execute: timeout)
    }

    private func clearFastTransferOffer(notifyDismissal: Bool) {
        let offerID = fastTransferOffer?.id
        fastTransferDecisionWorkItem?.cancel()
        fastTransferDecisionWorkItem = nil
        fastTransferOffer = nil
        fastTransferOfferFiles.removeAll()
        if notifyDismissal, let offerID {
            onFastTransferOfferDismissed?(offerID)
        }
    }

    @discardableResult
    private func beginManualFastTransferFromQueue() -> Bool {
        guard let connectedDevice,
              connectedDevice.supportsWiFiFastTransfer,
              let recordingStore,
              PlaudDeviceAgent.shared.isConnected(),
              !(currentExportContext != nil && currentExportTailReceived) else {
            return false
        }
        let activeSessionID = PlaudDeviceAgent.shared.checkIsRecording()
            ? PlaudDeviceAgent.shared.getCurrentSessionID()
            : nil
        var seen = Set<BetaCloudWork>()
        let files = ([currentExportFile].compactMap { $0 } + pendingFiles).filter { file in
            let identity = work(for: file)
            return file.sn == connectedDevice.serialNumber
                && file.sessionId != activeSessionID
                && recordingStore.needsCopy(
                    sessionID: file.sessionId,
                    serialNumber: file.sn
                )
                && seen.insert(identity).inserted
        }
        guard !files.isEmpty else { return false }
        beginWiFiFastTransfer(files: files)
        if wifiTransferGeneration != nil
            || !locationPermissionFiles.isEmpty
            || !manualFastTransferRequested {
            manualFastTransferRequested = false
            manualFastTransferLookupWorkItem?.cancel()
            manualFastTransferLookupWorkItem = nil
            return true
        }
        return false
    }

    @discardableResult
    private func continueManualFastTransferAfterBLEIfNeeded() -> Bool {
        guard manualFastTransferRequested else { return false }
        if !locationPermissionFiles.isEmpty {
            // The system permission sheet is still authoritative. Do not turn
            // a completed BLE item into an implicit cancellation of the rest
            // of the selected batch.
            return true
        }
        if beginManualFastTransferFromQueue() { return true }
        manualFastTransferRequested = false
        manualFastTransferLookupWorkItem?.cancel()
        manualFastTransferLookupWorkItem = nil
        fastTransferState = .idle
        return false
    }

    private func interruptActiveBLEExportForWiFi() {
        guard let interruptedFile = currentExportFile,
              let interruptedContext = currentExportContext else { return }
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        PlaudDeviceAgent.shared.stopSyncFile()
        PlaudDeviceAgent.shared.stopDownloadFile()
        guard interruptedFile.sessionId == interruptedContext.sessionID,
              interruptedFile.sn == interruptedContext.serialNumber,
              recordingStore?.needsCopy(
                sessionID: interruptedFile.sessionId,
                serialNumber: interruptedFile.sn
              ) == true,
              !pendingFiles.contains(where: {
                $0.sessionId == interruptedFile.sessionId && $0.sn == interruptedFile.sn
              }) else { return }
        pendingFiles.insert(interruptedFile, at: 0)
    }

    private func beginWiFiFastTransfer(files: [BleFile]) {
        guard !accessValidationSuspended,
              wifiTransferGeneration == nil,
              let connectedDevice,
              connectedDevice.supportsWiFiFastTransfer,
              let recordingStore,
              PlaudDeviceAgent.shared.isConnected(),
              !PlaudDeviceAgent.shared.checkIsRecording() else {
            files.forEach { blePreferredFiles.insert(work(for: $0)) }
            manualFastTransferRequested = false
            fastTransferState = .unavailable(
                "Fast Wi-Fi could not start, so PinPoint will continue safely over Bluetooth."
            )
            exportNextFileIfNeeded()
            return
        }
        guard requestLocationAccessIfNeeded(for: files) else { return }
        if currentExportContext != nil, currentExportTailReceived {
            manualFastTransferRequested = true
            fastTransferState = .checkingForRecordings
            return
        }
        interruptActiveBLEExportForWiFi()
        guard currentExportContext == nil else {
            files.forEach { blePreferredFiles.insert(work(for: $0)) }
            manualFastTransferRequested = false
            fastTransferState = .unavailable(
                "The current Bluetooth copy could not be paused safely, so it will continue over Bluetooth."
            )
            exportNextFileIfNeeded()
            return
        }

        var unique: [BetaCloudWork: BleFile] = [:]
        files.forEach { file in
            guard file.sn == connectedDevice.serialNumber,
                  recordingStore.needsCopy(
                    sessionID: file.sessionId,
                    serialNumber: file.sn
                  ) else { return }
            unique[work(for: file)] = file
        }
        let ordered = unique.values.sorted { $0.sessionId < $1.sessionId }
        guard !ordered.isEmpty else {
            manualFastTransferRequested = false
            fastTransferState = .idle
            exportNextFileIfNeeded()
            return
        }

        let selected = Set(unique.keys)
        pendingFiles.removeAll { selected.contains(work(for: $0)) }
        selected.forEach { blePreferredFiles.remove($0) }
        clearFastTransferOffer(notifyDismissal: true)
        manualFastTransferRequested = false
        wifiRequestedFiles = unique
        wifiPendingFiles.removeAll()
        wifiCurrentFile = nil
        wifiCurrentContext = nil
        wifiExportCallback = nil
        wifiCompletedCount = 0
        wifiLastFallbackMessage = nil
        let generation = UUID()
        wifiTransferGeneration = generation
        let delegateProxy = BetaWiFiDelegateProxy(service: self, generation: generation)
        wifiDelegateProxy = delegateProxy
        wifiPhase = .openingDeviceChannel
        fastTransferState = .connecting(recordingCount: ordered.count)

        PlaudWiFiAgent.shared.delegate = delegateProxy
        PlaudWiFiAgent.shared.bleDevice = BleAgent.shared.bleDevice
        PlaudDeviceAgent.shared.setDeviceWiFi(open: true)
        armWiFiConnectTimeout()
    }

    private func requestLocationAccessIfNeeded(for files: [BleFile]) -> Bool {
        let manager = locationManager ?? CLLocationManager()
        locationManager = manager
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            return true
        case .notDetermined:
            locationPermissionFiles = files
            locationPermissionGeneration = userGeneration
            manager.delegate = self
            fastTransferState = .requestingLocationPermission(
                recordingCount: files.count
            )
            manager.requestWhenInUseAuthorization()
            locationPermissionWorkItem?.cancel()
            let generation = userGeneration
            let workItem = DispatchWorkItem { [weak self] in
                guard let self,
                      self.locationPermissionGeneration == generation,
                      !self.locationPermissionFiles.isEmpty else { return }
                self.fallbackFromLocationPermission(
                    "Location permission was not confirmed in time. Automatic Bluetooth copy remains active."
                )
            }
            locationPermissionWorkItem = workItem
            DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: workItem)
            return false
        case .denied, .restricted:
            files.forEach { blePreferredFiles.insert(work(for: $0)) }
            manualFastTransferRequested = false
            fastTransferState = .unavailable(
                "Allow Location access in Settings to use Fast Wi-Fi. Automatic Bluetooth copy remains active."
            )
            exportNextFileIfNeeded()
            return false
        @unknown default:
            files.forEach { blePreferredFiles.insert(work(for: $0)) }
            manualFastTransferRequested = false
            fastTransferState = .unavailable(
                "Fast Wi-Fi permission is unavailable. Automatic Bluetooth copy remains active."
            )
            exportNextFileIfNeeded()
            return false
        }
    }

    private func handleLocationAuthorization(_ status: CLAuthorizationStatus) {
        guard let expectedGeneration = locationPermissionGeneration,
              expectedGeneration == userGeneration,
              !locationPermissionFiles.isEmpty else { return }
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            let files = locationPermissionFiles
            clearLocationPermissionRequest()
            beginWiFiFastTransfer(files: files)
        case .denied, .restricted:
            fallbackFromLocationPermission(
                "Location access was not allowed. Automatic Bluetooth copy remains active."
            )
        case .notDetermined:
            break
        @unknown default:
            fallbackFromLocationPermission(
                "Fast Wi-Fi permission is unavailable. Automatic Bluetooth copy remains active."
            )
        }
    }

    private func fallbackFromLocationPermission(_ message: String) {
        let files = locationPermissionFiles
        clearLocationPermissionRequest()
        files.forEach { blePreferredFiles.insert(work(for: $0)) }
        manualFastTransferRequested = false
        fastTransferState = .unavailable(message)
        exportNextFileIfNeeded()
    }

    private func clearLocationPermissionRequest() {
        locationPermissionWorkItem?.cancel()
        locationPermissionWorkItem = nil
        locationPermissionFiles.removeAll()
        locationPermissionGeneration = nil
        locationManager?.delegate = nil
    }

    private func armWiFiConnectTimeout() {
        wifiConnectTimeoutWorkItem?.cancel()
        guard let generation = wifiTransferGeneration else { return }
        let workItem = DispatchWorkItem { [weak self] in
            guard let self, self.wifiTransferGeneration == generation else { return }
            self.failWiFiFastTransfer("The recorder's Wi-Fi channel did not become ready in time.")
        }
        wifiConnectTimeoutWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 190, execute: workItem)
    }

    private func armWiFiFileListTimeout() {
        wifiConnectTimeoutWorkItem?.cancel()
        guard let generation = wifiTransferGeneration else { return }
        let workItem = DispatchWorkItem { [weak self] in
            guard let self, self.wifiTransferGeneration == generation else { return }
            self.failWiFiFastTransfer("The recorder did not return its Wi-Fi file list in time.")
        }
        wifiConnectTimeoutWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: workItem)
    }

    private func startNextWiFiExport() {
        guard let generation = wifiTransferGeneration,
              wifiPhase == .exporting,
              wifiCurrentContext == nil,
              let recordingStore else { return }
        wifiExportWatchdogWorkItem?.cancel()
        wifiExportWatchdogWorkItem = nil
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        wifiExportAbsoluteTimeoutWorkItem = nil

        while !wifiPendingFiles.isEmpty {
            let file = wifiPendingFiles.removeFirst()
            guard recordingStore.needsCopy(
                sessionID: file.sessionId,
                serialNumber: file.sn
            ) else { continue }
            let context = BetaAudioExportContext(
                token: UUID(),
                userGeneration: userGeneration,
                localDataGeneration: currentLocalDataGeneration(),
                sessionID: file.sessionId,
                serialNumber: file.sn
            )
            let callback = BetaWiFiAudioExportCallback(service: self, context: context)
            wifiCurrentFile = file
            wifiCurrentContext = context
            wifiExportCallback = callback
            wifiLastExportProgress = nil
            fastTransferState = .transferring(
                completed: wifiCompletedCount,
                total: wifiRequestedFiles.count,
                speed: nil
            )
            armWiFiExportWatchdog(context: context, generation: generation)
            armWiFiExportAbsoluteTimeout(context: context, generation: generation)
            PlaudWiFiAgent.shared.exportAudioViaWiFi(
                sessionId: file.sessionId,
                outputDir: recordingStore.audioDirectory(serialNumber: file.sn).path,
                format: .mp3,
                channels: 1,
                callback: callback
            )
            return
        }

        finishWiFiFastTransfer(message: nil)
    }

    private func armWiFiExportWatchdog(
        context: BetaAudioExportContext,
        generation: UUID
    ) {
        wifiExportWatchdogWorkItem?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.isActiveWiFiExport(context) else { return }
            self.fallbackCurrentWiFiFile(
                message: "A Fast Wi-Fi copy stopped making progress."
            )
        }
        wifiExportWatchdogWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 240, execute: workItem)
    }

    private func armWiFiExportAbsoluteTimeout(
        context: BetaAudioExportContext,
        generation: UUID
    ) {
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.isActiveWiFiExport(context) else { return }
            self.fallbackCurrentWiFiFile(
                message: "Fast Wi-Fi exceeded its safe transfer window."
            )
        }
        wifiExportAbsoluteTimeoutWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 30 * 60, execute: workItem)
    }

    fileprivate func handleWiFiExportProgress(
        _ context: BetaAudioExportContext,
        progress: Int
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.isActiveWiFiExport(context),
                  let generation = self.wifiTransferGeneration else { return }
            self.fastTransferState = .transferring(
                completed: self.wifiCompletedCount,
                total: self.wifiRequestedFiles.count,
                speed: PlaudWiFiAgent.shared.getFormattedDownloadSpeed()
            )
            if self.wifiLastExportProgress.map({ progress > $0 }) ?? true {
                self.wifiLastExportProgress = progress
                self.armWiFiExportWatchdog(context: context, generation: generation)
            }
        }
    }

    fileprivate func handleWiFiExportComplete(
        _ context: BetaAudioExportContext,
        outputPath: String
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.isActiveWiFiExport(context),
                  let recordingStore = self.recordingStore else { return }
            self.wifiExportWatchdogWorkItem?.cancel()
            self.wifiExportWatchdogWorkItem = nil
            self.wifiExportAbsoluteTimeoutWorkItem?.cancel()
            self.wifiExportAbsoluteTimeoutWorkItem = nil
            self.wifiLastExportProgress = nil
            self.wifiCurrentFile = nil
            self.wifiCurrentContext = nil
            self.wifiExportCallback = nil
            let size = (try? FileManager.default.attributesOfItem(
                atPath: outputPath
            )[.size] as? NSNumber)?.intValue ?? 0
            guard size > 0,
                  recordingStore.markLocal(
                    sessionID: context.sessionID,
                    serialNumber: context.serialNumber,
                    outputPath: outputPath
                  ) else {
                self.blePreferredFiles.insert(BetaCloudWork(
                    sessionID: context.sessionID,
                    serialNumber: context.serialNumber
                ))
                self.wifiLastFallbackMessage =
                    "Fast Wi-Fi returned an incomplete local copy for one recording."
                self.failWiFiFastTransfer(
                    "Fast Wi-Fi returned an incomplete local copy for one recording."
                )
                return
            }
            self.wifiCompletedCount += 1
            self.emitRecordings()
            self.startCloudPipeline(
                sessionID: context.sessionID,
                serialNumber: context.serialNumber,
                outputPath: outputPath
            )
            self.startNextWiFiExport()
        }
    }

    fileprivate func handleWiFiExportError(
        _ context: BetaAudioExportContext,
        error: String
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.isActiveWiFiExport(context) else { return }
            self.fallbackCurrentWiFiFile(
                message: "Fast Wi-Fi could not copy one recording."
            )
        }
    }

    private func fallbackCurrentWiFiFile(message: String) {
        wifiExportWatchdogWorkItem?.cancel()
        wifiExportWatchdogWorkItem = nil
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        wifiExportAbsoluteTimeoutWorkItem = nil
        if let file = wifiCurrentFile {
            blePreferredFiles.insert(work(for: file))
        }
        wifiLastFallbackMessage = message
        failWiFiFastTransfer(message)
    }

    private func failWiFiFastTransfer(_ message: String) {
        guard wifiTransferGeneration != nil else { return }
        wifiPhase = .closing
        finishWiFiFastTransfer(message: message)
    }

    private func finishWiFiFastTransfer(message: String?) {
        guard let finishingGeneration = wifiTransferGeneration else { return }
        wifiPhase = .closing
        let total = wifiRequestedFiles.count
        let completed = wifiCompletedCount
        let fallbackFiles = wifiRequestedFiles.filter { identity, _ in
            recordingStore?.needsCopy(
                sessionID: identity.sessionID,
                serialNumber: identity.serialNumber
            ) ?? true
        }
        fallbackFiles.keys.forEach { blePreferredFiles.insert($0) }
        let serialNumber = connectedDevice?.serialNumber
            ?? wifiRequestedFiles.keys.first?.serialNumber
        let activeWiFiSessionID = wifiCurrentFile?.sessionId
        let restorationMessage = message ?? wifiLastFallbackMessage

        wifiConnectTimeoutWorkItem?.cancel()
        wifiConnectTimeoutWorkItem = nil
        wifiExportWatchdogWorkItem?.cancel()
        wifiExportWatchdogWorkItem = nil
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        wifiExportAbsoluteTimeoutWorkItem = nil

        // The bridge never calls either Plaud device-file deletion API. The
        // recorder remains authoritative until markLocal has durably committed
        // a non-empty local export in this user's protected store.
        if let activeWiFiSessionID {
            PlaudWiFiAgent.shared.stopSyncFile(activeWiFiSessionID, 1)
        }
        let bluetoothAlreadyRestored = PlaudDeviceAgent.shared.isConnected()
            && serialNumber.flatMap { liveSDKDevice(serialNumber: $0) } != nil
        if bluetoothAlreadyRestored {
            pendingDeviceWiFiCloseSerial = nil
            pendingDeviceWiFiCloseGeneration = nil
        } else {
            deferDeviceWiFiClose(serialNumber: serialNumber)
        }
        // Keep the generation live, but the phase closed, while asking the SDK
        // to tear down. Synchronous callbacks are ignored as `.closing`; queued
        // callbacks retain this generation and cannot affect the next batch.
        PlaudDeviceAgent.shared.endWiFiTransfer()
        PlaudWiFiAgent.shared.disconnect()
        PlaudWiFiAgent.shared.delegate = nil
        if wifiDelegateProxy?.generation == finishingGeneration {
            wifiDelegateProxy?.service = nil
            wifiDelegateProxy = nil
        }
        guard wifiTransferGeneration == finishingGeneration else { return }
        wifiTransferGeneration = nil
        wifiPhase = .idle
        wifiPendingFiles.removeAll()
        wifiCurrentFile = nil
        wifiCurrentContext = nil
        wifiExportCallback = nil
        wifiRequestedFiles.removeAll()
        wifiCompletedCount = 0
        wifiLastExportProgress = nil

        fastTransferState = .restoringBluetooth(
            completed: completed,
            total: total,
            fallbackCount: fallbackFiles.count,
            message: restorationMessage
        )
        wifiLastFallbackMessage = nil
        guard !accessValidationSuspended, let serialNumber else { return }
        shouldMaintainConnection = true
        autoReconnectSerial = serialNumber
        if bluetoothAlreadyRestored {
            // `endWiFiTransfer` already sends the close command while BLE is
            // available. Keep this authenticated connection instead of
            // manufacturing a second disconnect/reconnect callback race.
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                guard let self,
                      !self.accessValidationSuspended,
                      self.connectedDevice?.serialNumber == serialNumber,
                      self.wifiTransferGeneration == nil,
                      PlaudDeviceAgent.shared.isConnected() else { return }
                self.fastTransferState = .idle
                PlaudDeviceAgent.shared.getState()
                PlaudDeviceAgent.shared.getStorage()
                PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
            }
            return
        }
        clearConnectionIdentity()
        state = .idle
        scheduleReconnect(after: 1)
    }

    private func stopFastTransferForLifecycle() {
        clearFastTransferOffer(notifyDismissal: true)
        manualFastTransferRequested = false
        manualFastTransferLookupWorkItem?.cancel()
        manualFastTransferLookupWorkItem = nil
        clearLocationPermissionRequest()
        fastTransferDecisionWorkItem?.cancel()
        fastTransferDecisionWorkItem = nil
        wifiConnectTimeoutWorkItem?.cancel()
        wifiConnectTimeoutWorkItem = nil
        wifiExportWatchdogWorkItem?.cancel()
        wifiExportWatchdogWorkItem = nil
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        wifiExportAbsoluteTimeoutWorkItem = nil
        let activeGeneration = wifiTransferGeneration
        let serialNumber = connectedDevice?.serialNumber
            ?? wifiRequestedFiles.keys.first?.serialNumber
            ?? pendingDeviceWiFiCloseSerial
        if activeGeneration == nil, let pendingSerial = pendingDeviceWiFiCloseSerial {
            pendingDeviceWiFiCloseGeneration = userGeneration
            closePendingDeviceWiFiIfNeeded(serialNumber: pendingSerial)
        }
        if activeGeneration != nil {
            if let sessionID = wifiCurrentFile?.sessionId {
                PlaudWiFiAgent.shared.stopSyncFile(sessionID, 1)
            }
            let hasAuthenticatedBLE = PlaudDeviceAgent.shared.isConnected()
                && serialNumber.flatMap { liveSDKDevice(serialNumber: $0) } != nil
            if hasAuthenticatedBLE {
                PlaudDeviceAgent.shared.setDeviceWiFi(open: false)
                pendingDeviceWiFiCloseSerial = nil
                pendingDeviceWiFiCloseGeneration = nil
            } else {
                deferDeviceWiFiClose(serialNumber: serialNumber)
            }
            PlaudDeviceAgent.shared.endWiFiTransfer()
            PlaudWiFiAgent.shared.disconnect()
        }
        PlaudWiFiAgent.shared.delegate = nil
        wifiDelegateProxy?.service = nil
        wifiDelegateProxy = nil
        wifiTransferGeneration = nil
        wifiPhase = .idle
        wifiRequestedFiles.removeAll()
        wifiPendingFiles.removeAll()
        wifiCurrentFile = nil
        wifiCurrentContext = nil
        wifiExportCallback = nil
        wifiCompletedCount = 0
        wifiLastFallbackMessage = nil
        wifiLastExportProgress = nil
        blePreferredFiles.removeAll()
        fastTransferState = .idle
    }

    private func resetFastTransferStateForNewUser() {
        clearFastTransferOffer(notifyDismissal: false)
        manualFastTransferRequested = false
        manualFastTransferLookupWorkItem?.cancel()
        manualFastTransferLookupWorkItem = nil
        clearLocationPermissionRequest()
        wifiConnectTimeoutWorkItem?.cancel()
        wifiConnectTimeoutWorkItem = nil
        wifiExportWatchdogWorkItem?.cancel()
        wifiExportWatchdogWorkItem = nil
        wifiExportAbsoluteTimeoutWorkItem?.cancel()
        wifiExportAbsoluteTimeoutWorkItem = nil
        wifiDelegateProxy?.service = nil
        wifiDelegateProxy = nil
        wifiTransferGeneration = nil
        wifiPhase = .idle
        wifiRequestedFiles.removeAll()
        wifiPendingFiles.removeAll()
        wifiCurrentFile = nil
        wifiCurrentContext = nil
        wifiExportCallback = nil
        wifiCompletedCount = 0
        wifiLastFallbackMessage = nil
        wifiLastExportProgress = nil
        blePreferredFiles.removeAll()
        pendingDeviceWiFiCloseSerial = nil
        pendingDeviceWiFiCloseGeneration = nil
        reportedWiFiCapabilitySerial = nil
        reportedWiFiCapability = false
        fastTransferState = .idle
    }

    private func finishBluetoothRestoreIfNeeded() {
        if case .restoringBluetooth = fastTransferState {
            fastTransferState = .idle
        }
    }

    private func emitFastTransferState() {
        let snapshot = fastTransferState
        DispatchQueue.main.async { [weak self] in
            self?.onFastTransferStateChange?(snapshot)
        }
    }

    private func isActiveWiFiExport(_ context: BetaAudioExportContext) -> Bool {
        !accessValidationSuspended
            && wifiTransferGeneration != nil
            && wifiCurrentContext == context
            && context.userGeneration == userGeneration
            && isCurrentLocalDataGeneration(context.localDataGeneration)
    }

    private func startCloudPipeline(
        sessionID: Int,
        serialNumber: String,
        outputPath: String?,
        queueFailedRetryIfBusy: Bool = false
    ) {
        guard !accessValidationSuspended,
              let session, let pinpointAPI, let recordingStore else {
            let savedFailure = self.recordingStore?.markFailed(
                sessionID: sessionID,
                serialNumber: serialNumber,
                message: "PinPoint needs a fresh sign-in before upload."
            ) ?? false
            if !savedFailure {
                state = .failed("PinPoint could not save recording status. Free disk space, then reopen PinPoint.")
            }
            emitRecordings()
            return
        }
        guard let recording = recordingStore.recording(
            sessionID: sessionID,
            serialNumber: serialNumber
        ),
        let sourceID = BetaRecordingStore.cloudSourceIdentifier(
            sessionID: sessionID,
            serialNumber: serialNumber,
            duration: recording.duration
        ) else {
            let savedFailure = recordingStore.markFailed(
                sessionID: sessionID,
                serialNumber: serialNumber,
                message: "PinPoint rejected invalid recorder metadata. Reconnect the recorder before retrying."
            )
            if !savedFailure {
                state = .failed("PinPoint could not save recording status. Free disk space, then reopen PinPoint.")
            }
            emitRecordings(from: recordingStore)
            return
        }
        let pipeline = BetaCloudPipeline(session: session, pinpointAPI: pinpointAPI)
        let pipelineKey = session.userID + ":" + serialNumber + ":" + String(sessionID)
        let work = BetaCloudWork(sessionID: sessionID, serialNumber: serialNumber)
        let expectedLocalDataGeneration = currentLocalDataGeneration()
        cloudPipelineLock.lock()
        guard cloudPipelines[pipelineKey] == nil else {
            queuedFailedCloudRetries.remove(work)
            cloudPipelineLock.unlock()
            return
        }
        guard cloudPipelines.count < maxConcurrentCloudPipelines else {
            if queueFailedRetryIfBusy {
                queuedFailedCloudRetries.insert(work)
            }
            cloudPipelineLock.unlock()
            return
        }
        queuedFailedCloudRetries.remove(work)
        cloudPipelines[pipelineKey] = pipeline
        cloudPipelineLock.unlock()
        pipeline.process(
            audioURL: outputPath.map(URL.init(fileURLWithPath:)),
            sourceID: sourceID,
            checkpoint: recordingStore.recording(
                sessionID: sessionID,
                serialNumber: serialNumber
            )?.cloudCheckpoint,
            shouldContinue: { [weak self] in
                self?.onMainSync {
                    guard let self,
                          self.isCurrentLocalDataGeneration(expectedLocalDataGeneration),
                          self.recordingStore === recordingStore else { return false }
                    return !self.accessValidationSuspended
                } ?? false
            },
            onStage: { [weak self] stage in
                self?.onMainSync {
                    guard let self,
                          self.isCurrentLocalDataGeneration(expectedLocalDataGeneration),
                          self.recordingStore === recordingStore else { return }
                    guard recordingStore.updateCloudStage(
                        sessionID: sessionID,
                        serialNumber: serialNumber,
                        stage: stage
                    ) else {
                        // This callback is a durable gate before the pipeline
                        // advances. Fail closed instead of allowing a cloud
                        // side effect that local state cannot safely resume.
                        pipeline.cancel()
                        self.removeCloudPipeline(pipeline, forKey: pipelineKey)
                        self.state = .failed("PinPoint could not save cloud progress. Free disk space; the durable checkpoint will resume safely.")
                        return
                    }
                    self.emitRecordings(from: recordingStore)
                }
            },
            onCheckpoint: { [weak self] checkpoint in
                self?.onMainSync {
                    guard let self,
                          self.isCurrentLocalDataGeneration(expectedLocalDataGeneration),
                          self.recordingStore === recordingStore else { return false }
                    let saved = recordingStore.saveCloudCheckpoint(
                        sessionID: sessionID,
                        serialNumber: serialNumber,
                        checkpoint: checkpoint
                    )
                    if saved { self.emitRecordings(from: recordingStore) }
                    return saved
                } ?? false
            },
            completion: { [weak self] result in
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    guard self.isCurrentLocalDataGeneration(expectedLocalDataGeneration),
                          self.recordingStore === recordingStore else {
                        self.removeCloudPipeline(pipeline, forKey: pipelineKey)
                        return
                    }
                    var terminalStateWasPersisted = true
                    var shouldReacquireRetiredUploadFromRecorder = false
                    switch result {
                    case .success:
                        terminalStateWasPersisted = recordingStore.recording(
                            sessionID: sessionID,
                            serialNumber: serialNumber
                        )?.status == .ready
                        if !terminalStateWasPersisted {
                            self.state = .failed("PinPoint received the transcript but could not safely save it. Free disk space, then reopen PinPoint.")
                        }
                    case .failure(let error):
                        if let cloudError = error as? BetaCloudError,
                           case .localAudioRequiredForSafeRestart = cloudError {
                            terminalStateWasPersisted = recordingStore.markFailed(
                                sessionID: sessionID,
                                serialNumber: serialNumber,
                                message: cloudError.localizedDescription
                            )
                            shouldReacquireRetiredUploadFromRecorder = terminalStateWasPersisted
                        } else if let apiError = error as? PinpointAPIError,
                           case .server(409, let code, let message) = apiError,
                           [
                               "upload_completion_unconfirmed",
                               "transcription_submission_unconfirmed",
                               "recording_source_conflict",
                           ].contains(code) {
                            terminalStateWasPersisted = recordingStore.markNeedsSupport(
                                sessionID: sessionID,
                                serialNumber: serialNumber,
                                message: message ?? "Plaud may already have processed this recording. PinPoint will not repeat an uncertain cloud action."
                            )
                        } else if let cloudError = error as? BetaCloudError,
                                  case .transcriptionFailed(let message) = cloudError {
                            // Plaud reports FAILURE/REVOKED as terminal. Keep
                            // the submitted task checkpoint for operator
                            // reconciliation and do not offer a retry that can
                            // only poll the same failed task forever.
                            terminalStateWasPersisted = recordingStore.markNeedsSupport(
                                sessionID: sessionID,
                                serialNumber: serialNumber,
                                message: "Plaud ended this transcription: \(message) Contact support before starting another transcription."
                            )
                        } else {
                            terminalStateWasPersisted = recordingStore.markFailed(
                                sessionID: sessionID,
                                serialNumber: serialNumber,
                                message: error.localizedDescription
                            )
                        }
                        if !terminalStateWasPersisted {
                            // In particular, never turn a failed checkpoint
                            // write into a tight retry loop when the disk is
                            // still unable to persist metadata.
                            self.state = .failed("PinPoint could not save recording status. Free disk space, then reopen PinPoint.")
                        }
                        if case PinpointAPIError.sessionExpired = error {
                            self.onSessionExpired?()
                        }
                    }
                    self.removeCloudPipeline(pipeline, forKey: pipelineKey)
                    guard self.recordingStore === recordingStore else { return }
                    self.emitRecordings(from: recordingStore)
                    guard terminalStateWasPersisted,
                          !self.accessValidationSuspended,
                          self.recordingStore === recordingStore else { return }
                    if shouldReacquireRetiredUploadFromRecorder,
                       let current = recordingStore.recording(
                        sessionID: sessionID,
                        serialNumber: serialNumber
                       ) {
                        // Reuse the normal failed-copy recovery path. It starts
                        // an authoritative recorder copy only for the durable
                        // association that owns this recording; otherwise it
                        // persists a support-required terminal state.
                        self.retryRecording(current)
                    }
                    self.retryCloudItems(includeFailed: false)
                    self.drainQueuedFailedCloudRetries()
                }
            }
        )
    }

    /// Cloud pipeline callbacks arrive on URLSession and polling queues while
    /// account teardown, store replacement, and UI state live on the main
    /// thread. Synchronously crossing this boundary keeps each durable write
    /// ordered with user/session changes and preserves `onStage` as a hard gate
    /// before the next irreversible cloud action.
    private func onMainSync<T>(_ work: () -> T) -> T {
        if Thread.isMainThread {
            return work()
        }
        return DispatchQueue.main.sync(execute: work)
    }

    private func removeCloudPipeline(_ pipeline: BetaCloudPipeline, forKey key: String) {
        cloudPipelineLock.lock()
        if cloudPipelines[key] === pipeline {
            cloudPipelines.removeValue(forKey: key)
        }
        cloudPipelineLock.unlock()
    }

    private func emitRecordings(from store: BetaRecordingStore) {
        let recordings = store.all
        DispatchQueue.main.async { [weak self] in self?.onRecordingsChange?(recordings) }
    }

    private func retryCloudItems(includeFailed: Bool = true) {
        guard !accessValidationSuspended, let recordingStore else { return }
        for recording in recordingStore.all {
            let resumableStatuses: [BetaRecordingStatus] = includeFailed
                ? [.local, .uploading, .transcribing, .failed]
                : [.local, .uploading, .transcribing]
            guard resumableStatuses.contains(recording.status) else { continue }
            let audioURL = recordingStore.audioURL(for: recording)
            guard audioURL != nil
                    || recording.cloudCheckpoint?.canResumeWithoutLocalAudio == true else { continue }
            startCloudPipeline(
                sessionID: recording.sessionID,
                serialNumber: recording.deviceSerialNumber,
                outputPath: audioURL?.path,
                queueFailedRetryIfBusy: recording.status == .failed
            )
        }
    }

    private func drainQueuedFailedCloudRetries() {
        guard !accessValidationSuspended, let recordingStore else { return }
        cloudPipelineLock.lock()
        let queued = Array(queuedFailedCloudRetries)
        cloudPipelineLock.unlock()
        for work in queued {
            guard let recording = recordingStore.recording(
                sessionID: work.sessionID,
                serialNumber: work.serialNumber
            ), recording.status == .failed else {
                cloudPipelineLock.lock()
                queuedFailedCloudRetries.remove(work)
                cloudPipelineLock.unlock()
                continue
            }
            let audioURL = recordingStore.audioURL(for: recording)
            guard audioURL != nil
                    || recording.cloudCheckpoint?.canResumeWithoutLocalAudio == true else {
                cloudPipelineLock.lock()
                queuedFailedCloudRetries.remove(work)
                cloudPipelineLock.unlock()
                continue
            }
            startCloudPipeline(
                sessionID: work.sessionID,
                serialNumber: work.serialNumber,
                outputPath: audioURL?.path,
                queueFailedRetryIfBusy: true
            )
        }
    }

    private func emitRecordings() {
        let recordings = recordingStore?.all ?? []
        DispatchQueue.main.async { [weak self] in self?.onRecordingsChange?(recordings) }
    }

    func markedMoments(for recording: BetaRecording) -> BetaRecordingMarkedMoments? {
        markedMomentRuntime?.markedMoments(for: recording)
    }

    private func refreshMarkedMoments(deviceConnected: Bool? = nil) {
        let connected = deviceConnected
            ?? (connectedDevice != nil && PlaudDeviceAgent.shared.isConnected())
        markedMomentRuntime?.refresh(
            recordings: recordingStore?.all ?? [],
            connectedDeviceSerialNumber: connectedDevice?.serialNumber,
            deviceConnected: connected,
            activeRecordingSessionID: activeRecordingSessionID
        )
    }

    private func currentLocalDataGeneration() -> Int {
        localDataGenerationLock.lock()
        defer { localDataGenerationLock.unlock() }
        return localDataGeneration
    }

    private func isCurrentLocalDataGeneration(_ generation: Int) -> Bool {
        localDataGenerationLock.lock()
        defer { localDataGenerationLock.unlock() }
        return localDataGeneration == generation
    }

    private func invalidateLocalDataCallbacks() {
        localDataGenerationLock.lock()
        localDataGeneration &+= 1
        localDataGenerationLock.unlock()
    }

    private func updateConnected(_ mutation: @escaping (inout ConnectedPlaudDevice) -> Void) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  !self.accessValidationSuspended,
                  self.activeConnectionGeneration == self.userGeneration,
                  PlaudDeviceAgent.shared.isConnected(),
                  var device = self.connectedDevice,
                  case .ready(let readyDevice) = self.state,
                  readyDevice.serialNumber == device.serialNumber else { return }
            mutation(&device)
            self.connectedDevice = device
            self.state = .ready(device)
        }
    }
    fileprivate func handleExportProgress(
        _ context: BetaAudioExportContext,
        progress: Int,
        message: String
    ) {
        DispatchQueue.main.async { [weak self] in
            self?.handleExportProgressOnMain(context, progress: progress)
        }
    }

    private func handleExportProgressOnMain(
        _ context: BetaAudioExportContext,
        progress: Int
    ) {
        guard isActiveExport(context), let recordingStore else { return }
        if lastExportProgress.map({ progress > $0 }) ?? true {
            lastExportProgress = progress
            armExportWatchdog(for: context)
        }
        guard recordingStore.begin(
            sessionID: context.sessionID,
            serialNumber: context.serialNumber,
            duration: recordingStore.recording(
                sessionID: context.sessionID,
                serialNumber: context.serialNumber
            )?.duration ?? 0
        ) else {
            exportWatchdogWorkItem?.cancel()
            exportWatchdogWorkItem = nil
            PlaudDeviceAgent.shared.stopSyncFile()
            PlaudDeviceAgent.shared.stopDownloadFile()
            currentExportFile = nil
            currentExportContext = nil
            currentExportCallback = nil
            currentExportTailReceived = false
            lastExportProgress = nil
            pendingFiles.removeAll()
            state = .failed("PinPoint could not save recorder-copy progress. The recording remains on the recorder; free disk space and retry.")
            return
        }
        emitRecordings()
    }

    fileprivate func handleExportComplete(
        _ context: BetaAudioExportContext,
        outputPath: String
    ) {
        DispatchQueue.main.async { [weak self] in
            self?.handleExportCompleteOnMain(context, outputPath: outputPath)
        }
    }

    private func handleExportCompleteOnMain(
        _ context: BetaAudioExportContext,
        outputPath: String
    ) {
        guard isActiveExport(context), let recordingStore else { return }
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        let size = (try? FileManager.default.attributesOfItem(atPath: outputPath)[.size] as? NSNumber)?.intValue ?? 0
        guard size > 0 else {
            if !recordingStore.markFailed(
                sessionID: context.sessionID,
                serialNumber: context.serialNumber,
                message: "The recorder returned an empty audio file."
            ) {
                state = .failed("PinPoint could not save the empty-file error. Free disk space; the recording remains on the recorder.")
            }
            emitRecordings()
            if continueManualFastTransferAfterBLEIfNeeded() { return }
            offerFastTransferIfNeeded()
            exportNextFileIfNeeded()
            return
        }
        guard recordingStore.markLocal(
            sessionID: context.sessionID,
            serialNumber: context.serialNumber,
            outputPath: outputPath
        ) else {
            state = .failed("PinPoint copied the audio but could not save its metadata. It was not uploaded; free disk space and reconnect to retry safely.")
            emitRecordings()
            if continueManualFastTransferAfterBLEIfNeeded() { return }
            offerFastTransferIfNeeded()
            exportNextFileIfNeeded()
            return
        }
        blePreferredFiles.remove(BetaCloudWork(
            sessionID: context.sessionID,
            serialNumber: context.serialNumber
        ))
        emitRecordings()
        startCloudPipeline(
            sessionID: context.sessionID,
            serialNumber: context.serialNumber,
            outputPath: outputPath
        )
        if continueManualFastTransferAfterBLEIfNeeded() { return }
        offerFastTransferIfNeeded()
        exportNextFileIfNeeded()
    }

    fileprivate func handleExportError(
        _ context: BetaAudioExportContext,
        error: String
    ) {
        DispatchQueue.main.async { [weak self] in
            self?.handleExportErrorOnMain(context)
        }
    }

    private func handleExportErrorOnMain(_ context: BetaAudioExportContext) {
        guard isActiveExport(context), let recordingStore else { return }
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        if !recordingStore.markFailed(
            sessionID: context.sessionID,
            serialNumber: context.serialNumber,
            message: "Recorder copy failed. It will retry next time."
        ) {
            state = .failed("PinPoint could not save the recorder error. Free disk space; the recording remains on the recorder.")
        }
        emitRecordings()
        if continueManualFastTransferAfterBLEIfNeeded() { return }
        offerFastTransferIfNeeded()
        exportNextFileIfNeeded()
    }

    private func armExportWatchdog(for context: BetaAudioExportContext) {
        exportWatchdogWorkItem?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isActiveExport(context) else { return }
            self.interruptActiveExportForRetry(
                message: "Recorder copy stopped making progress. PinPoint is retrying from the recorder; no recording was deleted."
            )
            if self.connectedDevice?.serialNumber == context.serialNumber,
               PlaudDeviceAgent.shared.isConnected() {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    guard !self.accessValidationSuspended,
                          self.connectedDevice?.serialNumber == context.serialNumber,
                          PlaudDeviceAgent.shared.isConnected() else { return }
                    PlaudDeviceAgent.shared.getFileList(startSessionId: 0)
                }
            } else {
                self.shouldMaintainConnection = true
                self.autoReconnectSerial = context.serialNumber
                self.scheduleReconnect(after: 1)
            }
        }
        exportWatchdogWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 240, execute: work)
    }

    /// Invalidates the per-export callback before stopping the SDK lane. A
    /// reconnect always rebuilds the queue from the recorder's authoritative
    /// file list, so an interrupted copy cannot permanently block later files.
    private func interruptActiveExportForRetry(message: String) {
        exportWatchdogWorkItem?.cancel()
        exportWatchdogWorkItem = nil
        let interrupted = currentExportContext
        currentExportFile = nil
        currentExportContext = nil
        currentExportCallback = nil
        currentExportTailReceived = false
        lastExportProgress = nil
        pendingFiles.removeAll()
        guard let interrupted else { return }
        PlaudDeviceAgent.shared.stopSyncFile()
        PlaudDeviceAgent.shared.stopDownloadFile()
        if let recordingStore {
            if !recordingStore.markFailed(
                sessionID: interrupted.sessionID,
                serialNumber: interrupted.serialNumber,
                message: message
            ) {
                state = .failed("PinPoint could not save the interrupted-copy status. The recording remains on the recorder; free disk space and reconnect.")
            }
            emitRecordings()
        }
    }

    private func isActiveExport(_ context: BetaAudioExportContext) -> Bool {
        !accessValidationSuspended
            && currentExportContext == context
            && context.userGeneration == userGeneration
            && isCurrentLocalDataGeneration(context.localDataGeneration)
    }
}

extension BetaDeviceService {
    fileprivate func handleWiFiConnectResult(
        _ success: Bool,
        errorCode: Int,
        message: String,
        generation: UUID
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .joiningNetwork else { return }
            guard success else {
                self.failWiFiFastTransfer(
                    "PinPoint could not join the recorder's Wi-Fi channel (code \(errorCode))."
                )
                return
            }
        }
    }

    fileprivate func handleWiFiHandshake(_ status: Int, generation: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .joiningNetwork else { return }
            guard status == 0 else {
                self.failWiFiFastTransfer(
                    "The recorder's Wi-Fi handshake failed (status \(status))."
                )
                return
            }
            self.wifiConnectTimeoutWorkItem?.cancel()
            self.wifiConnectTimeoutWorkItem = nil
            self.wifiPhase = .waitingForFileList
            self.armWiFiFileListTimeout()
            PlaudWiFiAgent.shared.getFileList(
                Int(Date().timeIntervalSince1970),
                0,
                false
            )
        }
    }

    fileprivate func handleWiFiFileList(_ files: [BleFile], generation: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .waitingForFileList,
                  let recordingStore = self.recordingStore else { return }
            self.wifiConnectTimeoutWorkItem?.cancel()
            self.wifiConnectTimeoutWorkItem = nil
            self.wifiPhase = .exporting
            let activeSessionID = PlaudDeviceAgent.shared.checkIsRecording()
                ? PlaudDeviceAgent.shared.getCurrentSessionID()
                : nil
            var seen = Set<BetaCloudWork>()
            self.wifiPendingFiles = files.filter { file in
                let identity = self.work(for: file)
                return file.sessionId != activeSessionID
                    && self.wifiRequestedFiles[identity] != nil
                    && recordingStore.needsCopy(
                        sessionID: file.sessionId,
                        serialNumber: file.sn
                    )
                    && seen.insert(identity).inserted
            }.sorted { $0.sessionId < $1.sessionId }

            let found = Set(self.wifiPendingFiles.map(self.work(for:)))
            self.wifiRequestedFiles.keys
                .filter { !found.contains($0) }
                .forEach { self.blePreferredFiles.insert($0) }
            self.startNextWiFiExport()
        }
    }

    fileprivate func handleWiFiFileListFailure(_ status: Int, generation: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .waitingForFileList else { return }
            self.failWiFiFastTransfer(
                "The recorder's file list was unavailable over Wi-Fi (status \(status))."
            )
        }
    }

    fileprivate func handleWiFiClose(_ status: Int, generation: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .joiningNetwork
                    || self.wifiPhase == .waitingForFileList
                    || self.wifiPhase == .exporting else { return }
            if status == 1000 {
                // The recorder also uses a clean WebSocket close when there is
                // nothing left to send. Any unresolved requested file remains
                // in the durable BLE fallback queue, without a false failure.
                self.finishWiFiFastTransfer(message: nil)
            } else {
                self.failWiFiFastTransfer(
                    "The recorder's Wi-Fi channel closed early (status \(status))."
                )
            }
        }
    }

    fileprivate func handleWiFiClientFailure(generation: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.wifiTransferGeneration == generation,
                  self.wifiPhase == .joiningNetwork
                    || self.wifiPhase == .waitingForFileList
                    || self.wifiPhase == .exporting else { return }
            self.failWiFiFastTransfer("The recorder's Wi-Fi connection was interrupted.")
        }
    }
}

extension BetaDeviceService: CLLocationManagerDelegate {
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        DispatchQueue.main.async { [weak self] in
            self?.handleLocationAuthorization(manager.authorizationStatus)
        }
    }
}
