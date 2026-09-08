import CryptoKit
import Foundation

/// A recording identity that is safe to hand to the best-effort marked-moment
/// lane. The audio transfer and cloud pipeline remain authoritative and never
/// wait for this lane.
struct BetaMarkedMomentCandidate: Codable, Equatable, Hashable {
    let deviceSerialNumber: String
    let sessionID: Int
    let duration: TimeInterval

    var queryStartTimestamp: Int { max(0, sessionID - 2) }

    var queryEndTimestamp: Int {
        let durationSeconds = max(0, Int(duration.rounded(.up)))
        let (end, overflow) = sessionID.addingReportingOverflow(durationSeconds + 5)
        return overflow ? Int.max : end
    }

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.deviceSerialNumber == rhs.deviceSerialNumber && lhs.sessionID == rhs.sessionID
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(deviceSerialNumber)
        hasher.combine(sessionID)
    }
}

/// The raw values are retained exactly as Plaud's pinned SDK reports them.
struct BetaMarkedMomentTag: Codable, Equatable, Hashable {
    let timestamp: Int
    let type: Int
    let status: Int
    let reserved: [Int]
}

enum BetaMarkedMomentStatus: String, Codable, Equatable {
    case pending
    case ready
    case none
    case unavailable
}

/// Per-recording durable retrieval state. A completed recording's marks cannot
/// change, but a late authoritative callback may upgrade an exhausted or empty
/// result. This lets the NotePin answer during a later recording window without
/// ever blocking or deleting the recording itself.
struct BetaRecordingMarkedMoments: Codable, Equatable {
    static let schemaVersion = 1
    static let maximumRecordingWindowAttempts = 8

    let schemaVersion: Int
    let userScope: String
    let deviceSerialNumber: String
    let sessionID: Int
    var fetchedAt: Date?
    var complete: Bool
    var attempts: Int
    var source: String
    var queryStartTimestamp: Int?
    var queryEndTimestamp: Int?
    var tags: [BetaMarkedMomentTag]

    var status: BetaMarkedMomentStatus {
        if complete { return tags.isEmpty ? .none : .ready }
        return attempts >= Self.maximumRecordingWindowAttempts ? .unavailable : .pending
    }

    var needsFetch: Bool { !complete && attempts < Self.maximumRecordingWindowAttempts }
}

enum BetaMarkedMomentStoreError: LocalizedError {
    case secureStorageUnavailable
    case invalidCandidate
    case invalidRecord
    case persistenceFailed

    var errorDescription: String? {
        switch self {
        case .secureStorageUnavailable:
            return "PinPoint could not open protected storage for marked moments. Recordings will continue normally."
        case .invalidCandidate:
            return "PinPoint ignored invalid marked-moment recording metadata."
        case .invalidRecord:
            return "PinPoint ignored an invalid marked-moment response."
        case .persistenceFailed:
            return "PinPoint could not save marked moments. It will retry during a later recording."
        }
    }
}

/// Separate per-user storage keeps marked moments out of the transfer state
/// machine while still placing them beneath the same user root. Deleting that
/// user's local Beta data therefore removes both recordings and their marks.
final class BetaMarkedMomentStore {
    let userScope: String

    private let root: URL
    private let fileManager: FileManager
    private let lock = NSLock()
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(
        userID: String,
        fileManager: FileManager = .default,
        applicationSupportRoot: URL? = nil
    ) throws {
        guard !userID.isEmpty else { throw BetaMarkedMomentStoreError.secureStorageUnavailable }
        self.fileManager = fileManager
        userScope = Self.scopeIdentifier(for: userID)
        let base = applicationSupportRoot
            ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        var rootURL = base
            .appendingPathComponent("PinPoint", isDirectory: true)
            .appendingPathComponent("Users", isDirectory: true)
            .appendingPathComponent(userScope, isDirectory: true)
            .appendingPathComponent("MarkedMoments", isDirectory: true)
        root = rootURL

        encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970

        do {
            try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
            try Self.secureDirectory(at: root, fileManager: fileManager)
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try rootURL.setResourceValues(values)
        } catch {
            throw BetaMarkedMomentStoreError.secureStorageUnavailable
        }
    }

    static func scopeIdentifier(for userID: String) -> String {
        SHA256.hash(data: Data(userID.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    func record(for candidate: BetaMarkedMomentCandidate) -> BetaRecordingMarkedMoments? {
        lock.lock()
        defer { lock.unlock() }
        return loadUnlocked(candidate)
    }

    func needsFetch(_ candidate: BetaMarkedMomentCandidate) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return loadUnlocked(candidate)?.needsFetch ?? Self.isValid(candidate)
    }

    /// Creates the visible `pending` state without counting a device attempt.
    /// Attempts count only when a later active-recording window actually sends
    /// a request, matching the observed NotePin behavior.
    @discardableResult
    func ensurePending(_ candidate: BetaMarkedMomentCandidate) throws -> BetaRecordingMarkedMoments {
        lock.lock()
        defer { lock.unlock() }
        guard Self.isValid(candidate) else { throw BetaMarkedMomentStoreError.invalidCandidate }
        if let existing = loadUnlocked(candidate) { return existing }
        let record = BetaRecordingMarkedMoments(
            schemaVersion: BetaRecordingMarkedMoments.schemaVersion,
            userScope: userScope,
            deviceSerialNumber: candidate.deviceSerialNumber,
            sessionID: candidate.sessionID,
            fetchedAt: nil,
            complete: false,
            attempts: 0,
            source: "",
            queryStartTimestamp: candidate.queryStartTimestamp,
            queryEndTimestamp: candidate.queryEndTimestamp,
            tags: []
        )
        try saveUnlocked(record)
        return record
    }

    /// Persist the attempt before sending so a crash cannot create an unlimited
    /// retry loop. The eighth request is still sent; its late callback can
    /// replace the computed `unavailable` state with a final result.
    @discardableResult
    func recordRecordingWindowAttempt(
        _ candidate: BetaMarkedMomentCandidate,
        now: Date = Date()
    ) throws -> BetaRecordingMarkedMoments {
        lock.lock()
        defer { lock.unlock() }
        guard Self.isValid(candidate) else { throw BetaMarkedMomentStoreError.invalidCandidate }
        var record = loadUnlocked(candidate) ?? BetaRecordingMarkedMoments(
            schemaVersion: BetaRecordingMarkedMoments.schemaVersion,
            userScope: userScope,
            deviceSerialNumber: candidate.deviceSerialNumber,
            sessionID: candidate.sessionID,
            fetchedAt: nil,
            complete: false,
            attempts: 0,
            source: "",
            queryStartTimestamp: candidate.queryStartTimestamp,
            queryEndTimestamp: candidate.queryEndTimestamp,
            tags: []
        )
        guard !record.complete,
              record.attempts < BetaRecordingMarkedMoments.maximumRecordingWindowAttempts else {
            return record
        }
        record.attempts += 1
        record.fetchedAt = now
        try saveUnlocked(record)
        return record
    }

    /// Saves an authoritative SDK response. Existing tags are unioned so a
    /// later paginated or legacy callback can add information but never remove
    /// an already captured button press.
    @discardableResult
    func saveComplete(
        _ candidate: BetaMarkedMomentCandidate,
        tags: [BetaMarkedMomentTag],
        source: String,
        now: Date = Date()
    ) throws -> BetaRecordingMarkedMoments {
        lock.lock()
        defer { lock.unlock() }
        guard Self.isValid(candidate),
              tags.allSatisfy(Self.isValid) else {
            throw BetaMarkedMomentStoreError.invalidRecord
        }
        let existing = loadUnlocked(candidate)
        let merged = Set((existing?.tags ?? []) + tags).sorted {
            if $0.timestamp != $1.timestamp { return $0.timestamp < $1.timestamp }
            if $0.type != $1.type { return $0.type < $1.type }
            if $0.status != $1.status { return $0.status < $1.status }
            return $0.reserved.lexicographicallyPrecedes($1.reserved)
        }
        let record = BetaRecordingMarkedMoments(
            schemaVersion: BetaRecordingMarkedMoments.schemaVersion,
            userScope: userScope,
            deviceSerialNumber: candidate.deviceSerialNumber,
            sessionID: candidate.sessionID,
            fetchedAt: now,
            complete: true,
            attempts: max(existing?.attempts ?? 0, 1),
            source: String(source.prefix(64)),
            queryStartTimestamp: candidate.queryStartTimestamp,
            queryEndTimestamp: candidate.queryEndTimestamp,
            tags: merged
        )
        try saveUnlocked(record)
        return record
    }

    private func loadUnlocked(_ candidate: BetaMarkedMomentCandidate) -> BetaRecordingMarkedMoments? {
        guard Self.isValid(candidate),
              let data = try? Data(contentsOf: recordURL(for: candidate)),
              let record = try? decoder.decode(BetaRecordingMarkedMoments.self, from: data),
              Self.isValid(record),
              record.userScope == userScope,
              record.deviceSerialNumber == candidate.deviceSerialNumber,
              record.sessionID == candidate.sessionID else { return nil }
        return record
    }

    private func saveUnlocked(_ record: BetaRecordingMarkedMoments) throws {
        guard Self.isValid(record), record.userScope == userScope else {
            throw BetaMarkedMomentStoreError.invalidRecord
        }
        let url = recordURL(
            deviceSerialNumber: record.deviceSerialNumber,
            sessionID: record.sessionID
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try Self.secureDirectory(at: url.deletingLastPathComponent(), fileManager: fileManager)
            let data = try encoder.encode(record)
            try data.write(
                to: url,
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch {
            throw BetaMarkedMomentStoreError.persistenceFailed
        }
    }

    private func recordURL(for candidate: BetaMarkedMomentCandidate) -> URL {
        recordURL(deviceSerialNumber: candidate.deviceSerialNumber, sessionID: candidate.sessionID)
    }

    private func recordURL(deviceSerialNumber: String, sessionID: Int) -> URL {
        root
            .appendingPathComponent(Self.deviceDirectoryName(deviceSerialNumber), isDirectory: true)
            .appendingPathComponent("\(sessionID).json")
    }

    private static func deviceDirectoryName(_ serialNumber: String) -> String {
        SHA256.hash(data: Data(serialNumber.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func isValid(_ candidate: BetaMarkedMomentCandidate) -> Bool {
        candidate.sessionID > 0
            && !candidate.deviceSerialNumber.isEmpty
            && candidate.deviceSerialNumber.count <= 256
            && !candidate.deviceSerialNumber.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
            && candidate.duration.isFinite
            && candidate.duration >= 0
    }

    private static func isValid(_ tag: BetaMarkedMomentTag) -> Bool {
        tag.timestamp >= 0
            && tag.timestamp <= Int(UInt32.max)
            && (0...Int(UInt8.max)).contains(tag.type)
            && (0...Int(UInt8.max)).contains(tag.status)
            && tag.reserved.count <= 64
            && tag.reserved.allSatisfy { (0...Int(UInt8.max)).contains($0) }
    }

    private static func isValid(_ record: BetaRecordingMarkedMoments) -> Bool {
        record.schemaVersion == BetaRecordingMarkedMoments.schemaVersion
            && record.userScope.count == 64
            && record.userScope.allSatisfy { $0.isHexDigit }
            && isValid(BetaMarkedMomentCandidate(
                deviceSerialNumber: record.deviceSerialNumber,
                sessionID: record.sessionID,
                duration: 0
            ))
            && record.attempts >= 0
            && record.attempts <= BetaRecordingMarkedMoments.maximumRecordingWindowAttempts
            && record.source.count <= 64
            && record.tags.count <= 10_000
            && record.tags.allSatisfy(isValid)
            && (!record.complete || record.fetchedAt != nil)
    }

    private static func secureDirectory(at url: URL, fileManager: FileManager) throws {
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }
}

/// Kept deliberately smaller than the Plaud SDK. A live adapter may borrow the
/// low-level BLE delegate only for a scoped transaction and must forward every
/// unrelated callback to the SDK's original delegate.
protocol BetaMarkedMomentCommandTransport: AnyObject {
    func beginCapture(scopeIdentifier: String)
    func endCapture(scopeIdentifier: String)
    func requestRecordMarkingTags(
        scopeIdentifier: String,
        uid: Int,
        startTimestamp: Int,
        endTimestamp: Int
    )
    func requestLegacyMarking(scopeIdentifier: String, sessionID: Int)
}

enum BetaMarkedMomentCoordinatorEvent: Equatable {
    case pending(Int)
    case completed(BetaRecordingMarkedMoments)
    case pausedUntilNextRecording(Int)
    case storageWarning(String)
}

/// Coordinates only the small, read-only marking query lane. The owner keeps
/// audio transfer and cloud upload running independently and calls `refresh`
/// whenever the file list or recording state changes.
final class BetaMarkedMomentCoordinator {
    typealias Scheduler = (_ delay: TimeInterval, _ work: @escaping () -> Void) -> Void

    private enum QueryVariant: String { case range, sweep }
    private struct Scope: Equatable {
        let identifier: String
        let userScope: String
        let userGeneration: Int
    }
    private struct OpenQuery {
        let scope: Scope
        let candidate: BetaMarkedMomentCandidate
        let variant: QueryVariant
        var tags: [BetaMarkedMomentTag]
        var indexes: Set<Int>
    }

    var onEvent: ((BetaMarkedMomentCoordinatorEvent) -> Void)?

    private let store: BetaMarkedMomentStore
    private let transport: BetaMarkedMomentCommandTransport
    private let scheduler: Scheduler
    private var scope: Scope?
    private var candidates: [BetaMarkedMomentCandidate] = []
    private var sendQueue: [BetaMarkedMomentCandidate] = []
    private var sendStage = 0
    private var activeRecordingSessionID: Int?
    private var attemptedInCurrentWindow = Set<BetaMarkedMomentCandidate>()
    private var openQueries: [Int: OpenQuery] = [:]
    private var legacyQueries: [Int: (scope: Scope, candidate: BetaMarkedMomentCandidate)] = [:]
    private var uidCounter = 0
    private var scheduledToken = UUID()
    private var captureActive = false

    init(
        store: BetaMarkedMomentStore,
        transport: BetaMarkedMomentCommandTransport,
        scheduler: @escaping Scheduler = { delay, work in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    ) {
        self.store = store
        self.transport = transport
        self.scheduler = scheduler
    }

    /// A new UUID is created for every signed-in activation. SDK callbacks carry
    /// this identifier, so replies from a previous user or connection are
    /// ignored even when a session number happens to match.
    @discardableResult
    func activate(userID: String, userGeneration: Int) -> String? {
        deactivate()
        guard store.userScope == BetaMarkedMomentStore.scopeIdentifier(for: userID) else {
            onEvent?(.storageWarning("Marked-moment storage did not match the signed-in user."))
            return nil
        }
        let value = Scope(
            identifier: UUID().uuidString,
            userScope: store.userScope,
            userGeneration: userGeneration
        )
        scope = value
        return value.identifier
    }

    func deactivate() {
        let previousScope = scope
        scheduledToken = UUID()
        scope = nil
        candidates = []
        sendQueue = []
        sendStage = 0
        activeRecordingSessionID = nil
        attemptedInCurrentWindow = []
        openQueries = [:]
        legacyQueries = [:]
        if captureActive, let previousScope {
            transport.endCapture(scopeIdentifier: previousScope.identifier)
        }
        captureActive = false
    }

    /// `activeRecordingSessionID` is the observed device recording, not a mark
    /// target. Finished recordings are queried only while this value is present;
    /// the current recording waits for the next recording window.
    func refresh(
        recordings: [BetaMarkedMomentCandidate],
        deviceConnected: Bool,
        activeRecordingSessionID: Int?
    ) {
        guard let scope else { return }
        let valid = recordings.filter {
            $0.sessionID != activeRecordingSessionID && $0.duration.isFinite && $0.duration >= 0
        }
        candidates = Array(Dictionary(
            valid.map { ("\($0.deviceSerialNumber)\u{0}\($0.sessionID)", $0) },
            uniquingKeysWith: { first, _ in first }
        ).values)

        var pendingCount = 0
        for candidate in candidates {
            do {
                let record = try store.ensurePending(candidate)
                if record.status == .pending { pendingCount += 1 }
            } catch {
                onEvent?(.storageWarning(error.localizedDescription))
            }
        }
        if pendingCount > 0 { onEvent?(.pending(pendingCount)) }

        guard deviceConnected else {
            pauseCapture(for: scope)
            return
        }
        let recordingWindowChanged = self.activeRecordingSessionID != activeRecordingSessionID
        if recordingWindowChanged {
            self.activeRecordingSessionID = activeRecordingSessionID
            attemptedInCurrentWindow = []
            scheduledToken = UUID()
        }
        if recordingWindowChanged || sendQueue.isEmpty { rebuildQueue() }
        guard activeRecordingSessionID != nil else {
            onEvent?(.pausedUntilNextRecording(sendQueue.count))
            return
        }
        if !captureActive {
            captureActive = true
            transport.beginCapture(scopeIdentifier: scope.identifier)
        }
        sendNextIfPossible(scope: scope)
    }

    func receiveRecordMarkingTags(
        scopeIdentifier: String,
        uid: Int,
        totals: Int,
        index: Int,
        tags: [BetaMarkedMomentTag]
    ) {
        guard let current = scope,
              current.identifier == scopeIdentifier,
              var query = openQueries[uid],
              query.scope == current,
              !query.indexes.contains(index) else { return }
        query.indexes.insert(index)
        for tag in tags where !query.tags.contains(tag) { query.tags.append(tag) }
        if totals <= 0 || query.tags.count >= totals {
            openQueries.removeValue(forKey: uid)
            let filtered: [BetaMarkedMomentTag]
            if query.variant == .sweep {
                filtered = query.tags.filter {
                    $0.timestamp >= query.candidate.queryStartTimestamp
                        && $0.timestamp <= query.candidate.queryEndTimestamp
                }
            } else {
                filtered = query.tags
            }
            complete(query.candidate, tags: filtered, source: query.variant.rawValue)
        } else {
            openQueries[uid] = query
        }
    }

    func receiveLegacyMarking(
        scopeIdentifier: String,
        sessionID: Int,
        status: Int,
        marks: [UInt32]
    ) {
        guard let current = scope,
              current.identifier == scopeIdentifier,
              let query = legacyQueries[sessionID],
              query.scope == current else { return }
        legacyQueries.removeValue(forKey: sessionID)
        guard status == 0 else { return }
        complete(
            query.candidate,
            tags: marks.map {
                BetaMarkedMomentTag(timestamp: Int($0), type: 0, status: 0, reserved: [])
            },
            source: "legacy"
        )
    }

    private func rebuildQueue() {
        sendQueue = candidates.filter {
            store.needsFetch($0) && !attemptedInCurrentWindow.contains($0)
        }.sorted { $0.sessionID > $1.sessionID }
        if sendQueue.count > 16 { sendQueue = Array(sendQueue.prefix(16)) }
        sendStage = 0
    }

    private func sendNextIfPossible(scope expectedScope: Scope) {
        guard scope == expectedScope,
              activeRecordingSessionID != nil,
              let candidate = sendQueue.first else { return }

        uidCounter = (uidCounter + 1) & 0x0FFF_FFFF
        let timestampPart = Int(Date().timeIntervalSince1970) & 0x07FF_FFFF
        let uid = ((timestampPart << 4) | (uidCounter & 0xF)) & 0x7FFF_FFFF

        switch sendStage {
        case 0:
            do {
                _ = try store.recordRecordingWindowAttempt(candidate)
            } catch {
                onEvent?(.storageWarning(error.localizedDescription))
                sendQueue.removeFirst()
                scheduleNext(scope: expectedScope)
                return
            }
            attemptedInCurrentWindow.insert(candidate)
            openQueries[uid] = OpenQuery(
                scope: expectedScope,
                candidate: candidate,
                variant: .range,
                tags: [],
                indexes: []
            )
            transport.requestRecordMarkingTags(
                scopeIdentifier: expectedScope.identifier,
                uid: uid,
                startTimestamp: candidate.queryStartTimestamp,
                endTimestamp: candidate.queryEndTimestamp
            )
            sendStage = 1
        case 1:
            openQueries[uid] = OpenQuery(
                scope: expectedScope,
                candidate: candidate,
                variant: .sweep,
                tags: [],
                indexes: []
            )
            transport.requestRecordMarkingTags(
                scopeIdentifier: expectedScope.identifier,
                uid: uid,
                startTimestamp: 0,
                endTimestamp: Int(Date().timeIntervalSince1970) + 86_400
            )
            sendStage = 2
        default:
            legacyQueries[candidate.sessionID] = (expectedScope, candidate)
            transport.requestLegacyMarking(
                scopeIdentifier: expectedScope.identifier,
                sessionID: candidate.sessionID
            )
            sendQueue.removeFirst()
            sendStage = 0
        }
        if openQueries.count > 64 {
            for key in openQueries.keys.sorted().prefix(openQueries.count - 64) {
                openQueries.removeValue(forKey: key)
            }
        }
        scheduleNext(scope: expectedScope)
    }

    private func scheduleNext(scope expectedScope: Scope) {
        let token = UUID()
        scheduledToken = token
        scheduler(5) { [weak self] in
            guard let self,
                  self.scheduledToken == token,
                  self.scope == expectedScope else { return }
            self.sendNextIfPossible(scope: expectedScope)
        }
    }

    private func complete(
        _ candidate: BetaMarkedMomentCandidate,
        tags: [BetaMarkedMomentTag],
        source: String
    ) {
        do {
            let result = try store.saveComplete(candidate, tags: tags, source: source)
            openQueries = openQueries.filter { $0.value.candidate != candidate }
            legacyQueries = legacyQueries.filter { $0.value.candidate != candidate }
            if sendQueue.first == candidate {
                sendQueue.removeFirst()
                sendStage = 0
            } else {
                sendQueue.removeAll { $0 == candidate }
            }
            onEvent?(.completed(result))
        } catch {
            onEvent?(.storageWarning(error.localizedDescription))
        }
    }

    private func pauseCapture(for expectedScope: Scope) {
        scheduledToken = UUID()
        sendQueue = []
        sendStage = 0
        openQueries = [:]
        legacyQueries = [:]
        if captureActive {
            transport.endCapture(scopeIdentifier: expectedScope.identifier)
            captureActive = false
        }
    }
}
