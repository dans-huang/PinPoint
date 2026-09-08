import Foundation
import CryptoKit

enum BetaRecordingStatus: String, Codable {
    case copying
    case local
    case uploading
    case transcribing
    case ready
    case failed
    case needsSupport
}

struct BetaCloudCompletedPart: Codable, Equatable {
    let partNumber: Int
    let etag: String
}

struct BetaCloudUploadPart: Codable, Equatable {
    let partNumber: Int
    let presignedURL: String
}

struct BetaCloudCheckpoint: Codable, Equatable {
    var createRequestID: String?
    let uploadJobID: String
    var chunkSize: Int?
    var uploadParts: [BetaCloudUploadPart]?
    var completedParts: [BetaCloudCompletedPart]
    var transcriptionID: String? = nil

    var canResumeWithoutLocalAudio: Bool {
        if let transcriptionID, !transcriptionID.isEmpty { return true }
        let completedNumbers = Set(completedParts.map(\.partNumber))
        if let uploadParts, !uploadParts.isEmpty {
            return Set(uploadParts.map(\.partNumber)).isSubset(of: completedNumbers)
        }
        return !completedParts.isEmpty
    }
}

enum BetaRecorderReleasePhase: String, Codable {
    case requested
    case cloudConfirmed
    case localDepaired
}

/// A local two-phase release intent. PinPoint writes `requested` before the
/// backend unbind call and does not remove this file until BLE depair succeeds.
/// That prevents a crash or lost HTTP response from silently binding the same
/// recorder back to this account on the next launch.
struct BetaRecorderReleaseCheckpoint: Codable, Equatable {
    let serialNumber: String
    let deviceType: String
    var phase: BetaRecorderReleasePhase
    var updatedAt: Date
}

enum BetaRecorderAssociationPhase: String, Codable {
    case bindRequested
    case bound
}

struct BetaRecorderAssociation: Codable, Equatable {
    let serialNumber: String
    let deviceType: String
    var phase: BetaRecorderAssociationPhase
    var updatedAt: Date
}

enum BetaRecorderAssociationBeginResult: Equatable {
    case ready(BetaRecorderAssociation)
    case identityConflict(BetaRecorderAssociation)
    case persistenceFailed
}

struct BetaRecording: Codable, Equatable {
    let sessionID: Int
    let deviceSerialNumber: String
    let createdAt: Date
    var duration: TimeInterval
    var localFileName: String?
    var status: BetaRecordingStatus
    var statusDetail: String?
    var transcript: String?
    /// Durable Partner transcription identity. Summary generation, retries,
    /// and future intelligence actions must not depend on a cleared upload
    /// checkpoint once the transcript is ready.
    var transcriptionID: String?
    var cloudCheckpoint: BetaCloudCheckpoint?
    var updatedAt: Date

    var title: String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return "Conversation · " + formatter.string(from: createdAt)
    }
}

final class BetaRecordingStore {
    private let root: URL
    private let metadataURL: URL
    private let releaseCheckpointURL: URL
    private let recorderAssociationURL: URL
    private let lock = NSLock()
    private var recordings: [BetaRecording]
    private var releaseCheckpointValue: BetaRecorderReleaseCheckpoint?
    private var recorderAssociationValue: BetaRecorderAssociation?

    init(userID: String, fileManager: FileManager = .default) throws {
        let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        var rootURL = base
            .appendingPathComponent("PinPoint", isDirectory: true)
            .appendingPathComponent("Users", isDirectory: true)
            .appendingPathComponent(Self.userDirectoryName(for: userID), isDirectory: true)
        root = rootURL
        metadataURL = root.appendingPathComponent("recordings.json")
        releaseCheckpointURL = root.appendingPathComponent("recorder-release.json")
        recorderAssociationURL = root.appendingPathComponent("recorder-association.json")
        recordings = []
        releaseCheckpointValue = nil
        recorderAssociationValue = nil
        do {
            try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
            try Self.secureDirectory(at: root)
            var resourceValues = URLResourceValues()
            resourceValues.isExcludedFromBackup = true
            try rootURL.setResourceValues(resourceValues)
            try Self.hardenExistingLocalFiles(under: root, fileManager: fileManager)
        } catch {
            throw BetaRecordingStoreError.secureStorageUnavailable
        }

        if fileManager.fileExists(atPath: metadataURL.path) {
            do {
                let data = try Data(contentsOf: metadataURL)
                let decoded = try JSONDecoder.pinpoint.decode([BetaRecording].self, from: data)
                guard Self.isValidRecordingMetadata(decoded) else {
                    throw BetaRecordingStoreError.corruptRecordingMetadata
                }
                recordings = decoded
            } catch {
                throw BetaRecordingStoreError.corruptRecordingMetadata
            }
        }

        if fileManager.fileExists(atPath: releaseCheckpointURL.path) {
            do {
                let data = try Data(contentsOf: releaseCheckpointURL)
                let checkpoint = try JSONDecoder.pinpoint.decode(
                    BetaRecorderReleaseCheckpoint.self,
                    from: data
                )
                guard let model = PlaudDeviceModel(serialNumber: checkpoint.serialNumber),
                      model.partnerBindingType == checkpoint.deviceType else {
                    throw BetaRecordingStoreError.corruptRecorderReleaseCheckpoint
                }
                releaseCheckpointValue = checkpoint
            } catch {
                throw BetaRecordingStoreError.corruptRecorderReleaseCheckpoint
            }
        }

        if fileManager.fileExists(atPath: recorderAssociationURL.path) {
            do {
                let data = try Data(contentsOf: recorderAssociationURL)
                let association = try JSONDecoder.pinpoint.decode(
                    BetaRecorderAssociation.self,
                    from: data
                )
                guard let model = PlaudDeviceModel(serialNumber: association.serialNumber),
                      model.partnerBindingType == association.deviceType else {
                    throw BetaRecordingStoreError.corruptRecorderAssociation
                }
                recorderAssociationValue = association
            } catch {
                throw BetaRecordingStoreError.corruptRecorderAssociation
            }
        }

        if let checkpoint = releaseCheckpointValue {
            if let association = recorderAssociationValue {
                guard association.serialNumber == checkpoint.serialNumber,
                      association.deviceType == checkpoint.deviceType else {
                    throw BetaRecordingStoreError.corruptRecorderReleaseCheckpoint
                }
            } else if checkpoint.phase != .localDepaired {
                // A requested/cloud-confirmed release must retain the exact
                // association it is releasing. Only the final crash window
                // may contain localDepaired with the association already gone.
                throw BetaRecordingStoreError.corruptRecorderReleaseCheckpoint
            }
        }
    }

    static func userDirectoryName(for userID: String) -> String {
        SHA256.hash(data: Data(userID.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    var all: [BetaRecording] {
        lock.lock()
        defer { lock.unlock() }
        return recordings.sorted { $0.createdAt > $1.createdAt }
    }

    func recorderReleaseCheckpoint() -> BetaRecorderReleaseCheckpoint? {
        lock.lock()
        defer { lock.unlock() }
        return releaseCheckpointValue
    }

    func recorderAssociation() -> BetaRecorderAssociation? {
        lock.lock()
        defer { lock.unlock() }
        return recorderAssociationValue
    }

    func beginRecorderAssociation(
        serialNumber: String,
        deviceType: String
    ) -> BetaRecorderAssociationBeginResult {
        lock.lock()
        defer { lock.unlock() }
        guard let model = PlaudDeviceModel(serialNumber: serialNumber),
              model.partnerBindingType == deviceType else {
            return .persistenceFailed
        }
        if let existing = recorderAssociationValue {
            guard existing.serialNumber == serialNumber,
                  existing.deviceType == deviceType else {
                return .identityConflict(existing)
            }
            // Repeated bind checks never downgrade a confirmed association.
            return .ready(existing)
        }
        let association = BetaRecorderAssociation(
            serialNumber: serialNumber,
            deviceType: deviceType,
            phase: .bindRequested,
            updatedAt: Date()
        )
        guard writeRecorderAssociationUnlocked(association) else {
            return .persistenceFailed
        }
        return .ready(association)
    }

    @discardableResult
    func markRecorderAssociationBound(serialNumber: String, deviceType: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard var association = recorderAssociationValue,
              association.serialNumber == serialNumber,
              association.deviceType == deviceType else { return false }
        if association.phase == .bound { return true }
        association.phase = .bound
        association.updatedAt = Date()
        return writeRecorderAssociationUnlocked(association)
    }

    @discardableResult
    func clearRecorderAssociation(matchingSerialNumber serialNumber: String, deviceType: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let association = recorderAssociationValue else { return true }
        guard association.serialNumber == serialNumber,
              association.deviceType == deviceType else { return false }
        let removed = Self.removeFileIfPresent(at: recorderAssociationURL)
        if removed { recorderAssociationValue = nil }
        return removed
    }

    @discardableResult
    func saveRecorderReleaseCheckpoint(_ checkpoint: BetaRecorderReleaseCheckpoint) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let model = PlaudDeviceModel(serialNumber: checkpoint.serialNumber),
              model.partnerBindingType == checkpoint.deviceType,
              let association = recorderAssociationValue,
              association.serialNumber == checkpoint.serialNumber,
              association.deviceType == checkpoint.deviceType else {
            return false
        }
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            let data = try JSONEncoder.pinpoint.encode(checkpoint)
            try data.write(
                to: releaseCheckpointURL,
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            try Self.secureFile(at: releaseCheckpointURL)
            releaseCheckpointValue = checkpoint
            return true
        } catch {
            return false
        }
    }

    @discardableResult
    func clearRecorderReleaseCheckpoint() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        let removed = Self.removeFileIfPresent(at: releaseCheckpointURL)
        if removed { releaseCheckpointValue = nil }
        return removed
    }

    func deleteAllLocalData() throws {
        lock.lock()
        defer { lock.unlock() }
        let manager = FileManager.default
        do {
            if manager.fileExists(atPath: root.path) {
                try manager.removeItem(at: root)
            }
        } catch {
            throw BetaRecordingStoreError.localDataNotDeleted
        }
        // The destructive part has completed. Clear the in-memory mirror before
        // attempting to recreate the empty storage root so an initialization
        // error can never resurrect deleted rows in this process.
        recordings = []
        releaseCheckpointValue = nil
        recorderAssociationValue = nil
        do {
            try manager.createDirectory(at: root, withIntermediateDirectories: true)
            try Self.secureDirectory(at: root)
            var resourceValues = URLResourceValues()
            resourceValues.isExcludedFromBackup = true
            var rootURL = root
            try rootURL.setResourceValues(resourceValues)
        } catch {
            throw BetaRecordingStoreError.localDataDeletedButStorageUnavailable
        }
    }

    func needsCopy(sessionID: Int, serialNumber: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let recording = recordings.first(where: {
            $0.sessionID == sessionID && $0.deviceSerialNumber == serialNumber
        }) else { return true }
        // Cloud-complete and support-frozen rows are terminal. Losing the
        // optional local MP3 must never turn either one into a new upload.
        if recording.status == .ready || recording.status == .needsSupport {
            return false
        }
        if recording.cloudCheckpoint?.canResumeWithoutLocalAudio == true {
            return false
        }
        // `.copying` is not a durable completion proof. The SDK may have
        // created a non-empty partial MP3 before a crash, so the recorder must
        // remain authoritative until `markLocal` commits the completed export.
        if recording.status == .copying {
            return true
        }
        return audioURLUnlocked(for: recording) == nil
    }

    func recording(sessionID: Int, serialNumber: String) -> BetaRecording? {
        lock.lock()
        defer { lock.unlock() }
        return recordings.first {
            $0.sessionID == sessionID && $0.deviceSerialNumber == serialNumber
        }
    }

    @discardableResult
    func begin(sessionID: Int, serialNumber: String, duration: TimeInterval) -> Bool {
        guard sessionID > 0,
              !serialNumber.isEmpty,
              duration.isFinite,
              duration >= 0 else { return false }
        return mutate(sessionID: sessionID, serialNumber: serialNumber, create: BetaRecording(
            sessionID: sessionID,
            deviceSerialNumber: serialNumber,
            createdAt: Date(timeIntervalSince1970: TimeInterval(sessionID)),
            duration: duration,
            localFileName: nil,
            status: .copying,
            statusDetail: nil,
            transcript: nil,
            cloudCheckpoint: nil,
            updatedAt: Date()
        )) { recording in
            recording.status = .copying
            recording.statusDetail = nil
        }
    }

    func audioDirectory(serialNumber: String) -> URL {
        lock.lock()
        defer { lock.unlock() }
        let directory = audioRootUnlocked()
            .appendingPathComponent(Self.deviceDirectoryName(for: serialNumber), isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? Self.secureDirectory(at: directory)
        return directory
    }

    private func audioRootUnlocked() -> URL {
        let directory = root.appendingPathComponent("Audio", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? Self.secureDirectory(at: directory)
        return directory
    }

    @discardableResult
    func markLocal(sessionID: Int, serialNumber: String, outputPath: String) -> Bool {
        let outputURL = URL(fileURLWithPath: outputPath).standardizedFileURL
        let rootPath = root.standardizedFileURL.path + "/"
        guard outputURL.path.hasPrefix(rootPath) else { return false }
        do {
            try Self.secureFile(at: outputURL)
        } catch {
            return false
        }
        let relativePath = String(outputURL.path.dropFirst(rootPath.count))
        return mutate(sessionID: sessionID, serialNumber: serialNumber) {
            $0.localFileName = relativePath
            $0.status = .local
            $0.statusDetail = nil
            // A missing local file may be reacquired while an idempotent
            // upload/transcription checkpoint already exists. Preserve that
            // checkpoint so a crash cannot allocate a duplicate cloud job.
        }
    }

    @discardableResult
    func saveCloudCheckpoint(sessionID: Int, serialNumber: String, checkpoint: BetaCloudCheckpoint?) -> Bool {
        if let checkpoint, !Self.isValidCloudCheckpoint(checkpoint) {
            return false
        }
        return mutate(sessionID: sessionID, serialNumber: serialNumber) {
            $0.cloudCheckpoint = checkpoint
        }
    }

    @discardableResult
    func updateCloudStage(sessionID: Int, serialNumber: String, stage: BetaCloudStage) -> Bool {
        mutate(sessionID: sessionID, serialNumber: serialNumber) { recording in
            switch stage {
            case .uploading(let progress):
                recording.status = .uploading
                recording.statusDetail = "Uploading \(Int(progress * 100))%"
            case .transcribing(let status):
                recording.status = .transcribing
                recording.statusDetail = status.capitalized
            case .ready(let transcript):
                recording.status = .ready
                recording.statusDetail = nil
                recording.transcript = transcript
                if let transcriptionID = recording.cloudCheckpoint?.transcriptionID,
                   !transcriptionID.isEmpty {
                    recording.transcriptionID = transcriptionID
                }
                recording.cloudCheckpoint = nil
            }
        }
    }

    @discardableResult
    func markFailed(sessionID: Int, serialNumber: String, message: String) -> Bool {
        mutate(sessionID: sessionID, serialNumber: serialNumber) {
            $0.status = .failed
            $0.statusDetail = message
        }
    }

    @discardableResult
    func markNeedsSupport(sessionID: Int, serialNumber: String, message: String) -> Bool {
        mutate(sessionID: sessionID, serialNumber: serialNumber) {
            $0.status = .needsSupport
            $0.statusDetail = message
        }
    }

    func audioURL(for recording: BetaRecording) -> URL? {
        lock.lock()
        defer { lock.unlock() }
        return audioURLUnlocked(for: recording)
    }

    @discardableResult
    private func mutate(
        sessionID: Int,
        serialNumber: String,
        create: BetaRecording? = nil,
        mutation: (inout BetaRecording) -> Void
    ) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        let previous = recordings
        if let index = recordings.firstIndex(where: {
            $0.sessionID == sessionID && $0.deviceSerialNumber == serialNumber
        }) {
            mutation(&recordings[index])
            recordings[index].updatedAt = Date()
        } else if var create {
            mutation(&create)
            create.updatedAt = Date()
            recordings.append(create)
        } else {
            return false
        }
        guard Self.isValidRecordingMetadata(recordings) else {
            recordings = previous
            return false
        }
        do {
            let data = try JSONEncoder.pinpoint.encode(recordings)
            try data.write(
                to: metadataURL,
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            try Self.secureFile(at: metadataURL)
            return true
        } catch {
            recordings = previous
            return false
        }
    }

    private func audioURLUnlocked(for recording: BetaRecording) -> URL? {
        guard let relativePath = recording.localFileName, !relativePath.isEmpty else { return nil }
        let url: URL
        if relativePath.contains("/") {
            url = root.appendingPathComponent(relativePath)
        } else {
            // Backward compatibility for early Beta metadata that stored only
            // the filename in the shared Audio directory.
            url = audioRootUnlocked().appendingPathComponent(relativePath)
        }
        let standardized = url.standardizedFileURL
        guard standardized.path.hasPrefix(root.standardizedFileURL.path + "/") else { return nil }
        guard let values = try? standardized.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
            .fileSizeKey,
        ]), values.isRegularFile == true,
            values.isSymbolicLink != true,
            (values.fileSize ?? 0) > 0,
            FileManager.default.isReadableFile(atPath: standardized.path) else { return nil }
        return standardized
    }

    private static func deviceDirectoryName(for serialNumber: String) -> String {
        SHA256.hash(data: Data(serialNumber.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    /// Stable, privacy-safe identity for the same recorder file across local
    /// metadata loss, app reinstall, or a move to another Mac. The backend
    /// scopes this digest to the signed-in user before using it as a source
    /// ledger key.
    static func cloudSourceIdentifier(
        sessionID: Int,
        serialNumber: String,
        duration: TimeInterval
    ) -> String? {
        guard sessionID > 0,
              !serialNumber.isEmpty,
              duration.isFinite,
              duration >= 0 else { return nil }
        // The SDK may report the same finished recording with slightly
        // different rounded durations across devices or file-list refreshes.
        // The recorder serial plus its positive session id is the durable
        // source identity; duration remains validated metadata, not identity.
        let material = "pinpoint-recording-v3\u{0}\(serialNumber)\u{0}\(sessionID)"
        return SHA256.hash(data: Data(material.utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    private static func secureFile(at url: URL) throws {
        try FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o600))],
            ofItemAtPath: url.path
        )
    }

    private static func secureDirectory(at url: URL) throws {
        try FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o700))],
            ofItemAtPath: url.path
        )
    }

    private static func hardenExistingLocalFiles(
        under root: URL,
        fileManager: FileManager
    ) throws {
        guard let enumerator = fileManager.enumerator(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
            options: [.skipsHiddenFiles]
        ) else { throw BetaRecordingStoreError.secureStorageUnavailable }
        for case let url as URL in enumerator {
            let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isSymbolicLink != true else { continue }
            if values.isDirectory == true {
                try secureDirectory(at: url)
            } else {
                try secureFile(at: url)
            }
        }
    }

    private static func isValidRecordingMetadata(_ recordings: [BetaRecording]) -> Bool {
        var identities = Set<String>()
        for recording in recordings {
            guard recording.sessionID > 0,
                  !recording.deviceSerialNumber.isEmpty,
                  recording.createdAt.timeIntervalSince1970.isFinite,
                  recording.duration.isFinite,
                  recording.duration >= 0,
                  recording.updatedAt.timeIntervalSince1970.isFinite else { return false }
            let identity = recording.deviceSerialNumber + "\u{0}" + String(recording.sessionID)
            guard identities.insert(identity).inserted else { return false }
            if let relativePath = recording.localFileName {
                guard !relativePath.isEmpty,
                      !relativePath.hasPrefix("/"),
                      !relativePath.split(separator: "/").contains("..") else { return false }
            }
            if let checkpoint = recording.cloudCheckpoint,
               !isValidCloudCheckpoint(checkpoint) {
                return false
            }
            if let transcriptionID = recording.transcriptionID,
               !Self.isValidOpaqueIdentifier(transcriptionID) {
                return false
            }
        }
        return true
    }

    private static func isValidCloudCheckpoint(_ checkpoint: BetaCloudCheckpoint) -> Bool {
        func validIdentifier(_ value: String?, allowEmpty: Bool = false) -> Bool {
            guard let value else { return true }
            if value.isEmpty { return allowEmpty }
            guard value.count <= 256 else { return false }
            return !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
                && !value.contains("/")
                && !value.contains("?")
                && !value.contains("#")
        }

        guard validIdentifier(checkpoint.createRequestID),
              validIdentifier(checkpoint.uploadJobID, allowEmpty: true),
              validIdentifier(checkpoint.transcriptionID),
              !checkpoint.uploadJobID.isEmpty || checkpoint.createRequestID != nil else {
            return false
        }
        if let transcriptionID = checkpoint.transcriptionID, transcriptionID.isEmpty {
            return false
        }
        if (checkpoint.chunkSize == nil) != (checkpoint.uploadParts == nil) {
            return false
        }
        let completedNumbers = checkpoint.completedParts.map(\.partNumber)
        guard checkpoint.completedParts.count <= 10_000,
              Set(completedNumbers).count == completedNumbers.count,
              checkpoint.completedParts.allSatisfy({ part in
                  (1...10_000).contains(part.partNumber)
                      && !part.etag.isEmpty
                      && part.etag.count <= 256
                      && !part.etag.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
              }) else { return false }
        if let chunkSize = checkpoint.chunkSize,
           let uploadParts = checkpoint.uploadParts {
            guard chunkSize > 0,
                  chunkSize <= 256 * 1_024 * 1_024,
                  !uploadParts.isEmpty,
                  uploadParts.count <= 10_000 else { return false }
            let partNumbers = uploadParts.map(\.partNumber)
            guard Set(partNumbers).count == partNumbers.count,
                  Set(completedNumbers).isSubset(of: Set(partNumbers)) else { return false }
            for part in uploadParts {
                let (_, overflow) = max(0, part.partNumber - 1)
                    .multipliedReportingOverflow(by: chunkSize)
                guard (1...10_000).contains(part.partNumber),
                      !overflow,
                      part.presignedURL.count <= 8_192,
                      let url = URL(string: part.presignedURL),
                      url.scheme?.lowercased() == "https",
                      url.host != nil else { return false }
            }
        }
        return true
    }

    private static func isValidOpaqueIdentifier(_ value: String) -> Bool {
        guard !value.isEmpty, value.count <= 256 else { return false }
        return !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
            && !value.contains("/")
            && !value.contains("?")
            && !value.contains("#")
    }

    private static func removeFileIfPresent(at url: URL) -> Bool {
        do {
            if FileManager.default.fileExists(atPath: url.path) {
                try FileManager.default.removeItem(at: url)
            }
            return true
        } catch {
            return false
        }
    }

    private func writeRecorderAssociationUnlocked(_ association: BetaRecorderAssociation) -> Bool {
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            let data = try JSONEncoder.pinpoint.encode(association)
            try data.write(
                to: recorderAssociationURL,
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            try Self.secureFile(at: recorderAssociationURL)
            recorderAssociationValue = association
            return true
        } catch {
            return false
        }
    }
}

enum BetaRecordingStoreError: LocalizedError {
    case secureStorageUnavailable
    case corruptRecordingMetadata
    case corruptRecorderAssociation
    case corruptRecorderReleaseCheckpoint
    case localDataNotDeleted
    case localDataDeletedButStorageUnavailable

    var errorDescription: String? {
        switch self {
        case .secureStorageUnavailable:
            return "PinPoint could not open its protected local storage. No recorder or cloud work was started. Quit the app and check disk access."
        case .corruptRecordingMetadata:
            return "PinPoint found unreadable local recording metadata. No recorder or cloud work was started; contact support before resetting local data."
        case .corruptRecorderAssociation:
            return "PinPoint found an unreadable recorder ownership checkpoint. It will not connect or bind another recorder; contact support."
        case .corruptRecorderReleaseCheckpoint:
            return "PinPoint found an unreadable recorder-release checkpoint. It will not connect or bind another recorder; contact support."
        case .localDataNotDeleted:
            return "PinPoint could not remove all local data. Check disk access and try again."
        case .localDataDeletedButStorageUnavailable:
            return "Local data was deleted, but PinPoint could not recreate its protected storage. Quit and reopen PinPoint before recording again."
        }
    }
}
