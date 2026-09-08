import CryptoKit
import Foundation

/// Keeps automatic summaries idempotent across reconnects and app launches.
/// The backend remains canonical; this object is only a scoped UI cache.
final class BetaIntelligenceCoordinator {
    private struct DurableJobRequest: Codable {
        let idempotencyKey: String
        let templateID: String?
        let markedMoments: [Int]
    }

    var onSummaryChange: ((String, BetaSummarySnapshot?) -> Void)?
    var onAutomaticStatusChange: ((String?) -> Void)?
    var markedMomentsProvider: ((BetaRecording) -> BetaRecordingMarkedMoments?)?

    let client: BetaIntelligenceProviding
    private(set) var session: BetaSession
    private var settings: BetaIntelligenceSettings?
    private var summaries: [String: BetaSummarySnapshot] = [:]
    private var inFlight = Set<String>()
    private var observedReadyIDs: Set<String>?
    private var automaticRetryCounts: [String: Int] = [:]
    private var automaticRetryWorkItems: [String: DispatchWorkItem] = [:]
    private let defaults: UserDefaults
    private let pendingPrefix: String

    init(
        client: BetaIntelligenceProviding,
        session: BetaSession,
        defaults: UserDefaults = .standard
    ) {
        self.client = client
        self.session = session
        self.defaults = defaults
        pendingPrefix = "PinPoint.Intelligence."
            + BetaRecordingStore.userDirectoryName(for: session.userID)
    }

    /// Keep long-lived summary/detail screens on the refreshed bearer token.
    /// A coordinator is never reused across users.
    func updateSession(_ session: BetaSession) {
        guard session.userID == self.session.userID else { return }
        self.session = session
    }

    var summaryReadyTranscriptionIDs: Set<String> {
        Set(summaries.keys)
    }

    func cachedSummary(for transcriptionID: String) -> BetaSummarySnapshot? {
        summaries[transcriptionID]
    }

    func prepare(recordings: [BetaRecording]) {
        loadSettingsIfNeeded { [weak self] settings in
            guard let self, settings.automaticSummaryEnabled else { return }
            let ready = recordings.filter { $0.status == .ready && $0.transcriptionID != nil }
            let readyIDs = Set(ready.compactMap(\.transcriptionID))
            if self.observedReadyIDs == nil {
                // First launch establishes a privacy boundary: enabling a new
                // build must not silently send an account's entire local
                // history to the model. Newly completed recordings still flow
                // automatically after this baseline.
                self.observedReadyIDs = readyIDs
                return
            }
            let newlyReady = readyIDs.subtracting(self.observedReadyIDs ?? [])
            self.observedReadyIDs?.formUnion(readyIDs)
            for recording in ready where newlyReady.contains(recording.transcriptionID ?? "") {
                self.ensureAutomaticSummary(for: recording, templateID: settings.defaultTemplateID)
            }
        }
    }

    func loadSummary(
        for recording: BetaRecording,
        completion: @escaping (Result<BetaSummarySnapshot?, Error>) -> Void
    ) {
        guard let id = recording.transcriptionID else {
            completion(.success(nil))
            return
        }
        let cached = summaries[id]
        client.getSummary(transcriptionID: id, sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                if case .success(let value?) = result {
                    self?.summaries[id] = value
                    self?.onSummaryChange?(id, value)
                    completion(.success(value))
                } else if case .failure = result, let cached {
                    completion(.success(cached))
                } else {
                    completion(result)
                }
            }
        }
    }

    func generateSummary(
        for recording: BetaRecording,
        templateID: String?,
        completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void
    ) {
        let key = "manual_" + UUID().uuidString.replacingOccurrences(of: "-", with: "")
        performGenerate(
            for: recording,
            templateID: templateID ?? settings?.defaultTemplateID,
            idempotencyKey: key,
            completion: completion
        )
    }

    private func performGenerate(
        for recording: BetaRecording,
        templateID: String?,
        idempotencyKey: String,
        completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void
    ) {
        guard let transcriptionID = recording.transcriptionID else {
            completion(.failure(BetaIntelligenceError.notReady))
            return
        }
        createJob(
            transcriptionID: transcriptionID,
            kind: .generate,
            templateID: templateID,
            markedMoments: markedMomentOffsets(for: recording),
            idempotencyKey: idempotencyKey
        ) { [weak self] result in
            switch result {
            case .success:
                self?.client.getSummary(
                    transcriptionID: transcriptionID,
                    sessionToken: self?.session.sessionToken ?? ""
                ) { summaryResult in
                    DispatchQueue.main.async {
                        switch summaryResult {
                        case .success(let summary?):
                            self?.summaries[transcriptionID] = summary
                            self?.onSummaryChange?(transcriptionID, summary)
                            completion(.success(summary))
                        case .success(nil):
                            completion(.failure(BetaIntelligenceError.invalidResponse))
                        case .failure(let error):
                            completion(.failure(error))
                        }
                    }
                }
            case .failure(let error):
                DispatchQueue.main.async { completion(.failure(error)) }
            }
        }
    }

    func improveSummary(
        for recording: BetaRecording,
        templateID: String?,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    ) {
        guard let transcriptionID = recording.transcriptionID else {
            completion(.failure(BetaIntelligenceError.notReady))
            return
        }
        // Each explicit click is a new review request. The canonical version is
        // still checked by the backend before approval.
        let key = "manual_" + UUID().uuidString.replacingOccurrences(of: "-", with: "")
        createJob(
            transcriptionID: transcriptionID,
            kind: .improve,
            templateID: templateID ?? settings?.defaultTemplateID,
            markedMoments: markedMomentOffsets(for: recording),
            idempotencyKey: key,
            completion: completion
        )
    }

    func hasPendingWordingReview(for recording: BetaRecording) -> Bool {
        guard let transcriptionID = recording.transcriptionID else { return false }
        let key = pendingKey(kind: .improve, transcriptionID: transcriptionID)
        return defaults.string(forKey: key + ".job") != nil
            || defaults.string(forKey: key + ".request") != nil
    }

    /// Reopens the exact background review authorized by the person before an
    /// app quit or lost HTTP response. It never starts a new unrelated review.
    func resumePendingWordingReview(
        for recording: BetaRecording,
        completion: @escaping (Result<BetaSummaryJob?, Error>) -> Void
    ) {
        guard let transcriptionID = recording.transcriptionID else {
            completion(.success(nil))
            return
        }
        let durableKey = pendingKey(kind: .improve, transcriptionID: transcriptionID)
        if let jobID = defaults.string(forKey: durableKey + ".job"), !jobID.isEmpty {
            pollJob(
                id: jobID,
                kind: .improve,
                durableKey: durableKey,
                attempt: 0
            ) { result in completion(result.map(Optional.some)) }
            return
        }
        guard let requestKey = defaults.string(forKey: durableKey + ".request"),
              !requestKey.isEmpty else {
            completion(.success(nil))
            return
        }
        // Older pre-release builds persisted the request key before the exact
        // payload. `createJob` first looks the key up on the backend; only when
        // no server job exists does this reconstructed payload start work.
        let request = durableRequest(durableKey: durableKey) ?? DurableJobRequest(
            idempotencyKey: requestKey,
            templateID: settings?.defaultTemplateID,
            markedMoments: markedMomentOffsets(for: recording)
        )
        createJob(
            transcriptionID: transcriptionID,
            kind: .improve,
            templateID: request.templateID,
            markedMoments: request.markedMoments,
            idempotencyKey: request.idempotencyKey
        ) { result in completion(result.map(Optional.some)) }
    }

    func approve(
        _ job: BetaSummaryJob,
        acceptedVocabulary: [String],
        completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void
    ) {
        client.approveSummaryJob(
            id: job.id,
            expectedVersion: job.baseSummaryVersion,
            expectedHash: job.baseSummaryHash,
            approvedVocabulary: acceptedVocabulary,
            sessionToken: session.sessionToken
        ) { [weak self] result in
            DispatchQueue.main.async {
                if case .success(let summary) = result, let self {
                    self.summaries[summary.transcriptionID] = summary
                    self.onSummaryChange?(summary.transcriptionID, summary)
                    self.clearPending(
                        self.pendingKey(kind: .improve, transcriptionID: summary.transcriptionID)
                    )
                }
                completion(result)
            }
        }
    }

    func discard(_ job: BetaSummaryJob, completion: @escaping (Result<Void, Error>) -> Void) {
        client.discardSummaryJob(id: job.id, sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                if case .success = result, let self {
                    self.clearPending(
                        self.pendingKey(kind: .improve, transcriptionID: job.transcriptionID)
                    )
                }
                completion(result)
            }
        }
    }

    func reloadSettings(completion: ((Result<BetaIntelligenceSettings, Error>) -> Void)? = nil) {
        settings = nil
        automaticRetryWorkItems.values.forEach { $0.cancel() }
        automaticRetryWorkItems.removeAll()
        automaticRetryCounts.removeAll()
        loadSettingsIfNeeded { settings in completion?(.success(settings)) }
    }

    private func loadSettingsIfNeeded(completion: @escaping (BetaIntelligenceSettings) -> Void) {
        if let settings {
            completion(settings)
            return
        }
        client.getSettings(sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let settings):
                    self.settings = settings
                    self.onAutomaticStatusChange?(nil)
                    completion(settings)
                case .failure(let error):
                    self.onAutomaticStatusChange?(error.localizedDescription)
                }
            }
        }
    }

    private func ensureAutomaticSummary(for recording: BetaRecording, templateID: String) {
        guard let transcriptionID = recording.transcriptionID,
              summaries[transcriptionID] == nil,
              !inFlight.contains(transcriptionID),
              automaticRetryWorkItems[transcriptionID] == nil else { return }
        inFlight.insert(transcriptionID)
        client.getSummary(transcriptionID: transcriptionID, sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let summary?):
                    self.inFlight.remove(transcriptionID)
                    self.clearAutomaticFailure(transcriptionID)
                    self.summaries[transcriptionID] = summary
                    self.onSummaryChange?(transcriptionID, summary)
                case .success(nil):
                    self.performGenerate(
                        for: recording,
                        templateID: templateID,
                        idempotencyKey: Self.idempotencyKey(
                            userID: self.session.userID,
                            transcriptionID: transcriptionID,
                            purpose: "generate:\(templateID):attempt:\(self.automaticRetryCounts[transcriptionID] ?? 0)"
                        )
                    ) { [weak self] generation in
                        self?.inFlight.remove(transcriptionID)
                        switch generation {
                        case .success:
                            self?.clearAutomaticFailure(transcriptionID)
                        case .failure(let error):
                            self?.scheduleAutomaticRetry(
                                recording: recording,
                                templateID: templateID,
                                error: error
                            )
                        }
                    }
                case .failure(let error):
                    self.inFlight.remove(transcriptionID)
                    self.scheduleAutomaticRetry(recording: recording, templateID: templateID, error: error)
                }
            }
        }
    }

    private func createJob(
        transcriptionID: String,
        kind: BetaSummaryJobKind,
        templateID: String?,
        markedMoments: [Int],
        idempotencyKey: String,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    ) {
        let durableKey = pendingKey(kind: kind, transcriptionID: transcriptionID)
        let existingRequestKey = defaults.string(forKey: durableKey + ".request")
        let requestKey = existingRequestKey ?? idempotencyKey
        let request = durableRequest(durableKey: durableKey) ?? DurableJobRequest(
            idempotencyKey: requestKey,
            templateID: templateID,
            markedMoments: Array(markedMoments.filter { $0 >= 0 }.prefix(64))
        )
        persist(request, durableKey: durableKey)
        if let jobID = defaults.string(forKey: durableKey + ".job"), !jobID.isEmpty {
            pollJob(
                id: jobID,
                kind: kind,
                durableKey: durableKey,
                attempt: 0,
                completion: completion
            )
            return
        }
        if existingRequestKey != nil {
            // A create response can be lost after the backend committed the
            // job. Resolve that exact user-scoped idempotency key before ever
            // issuing another provider request.
            client.findSummaryJob(
                idempotencyKey: request.idempotencyKey,
                sessionToken: session.sessionToken
            ) { [weak self] result in
                DispatchQueue.main.async {
                    guard let self else { return }
                    switch result {
                    case .success(let job?):
                        self.defaults.set(job.id, forKey: durableKey + ".job")
                        self.resolveOrPoll(
                            job,
                            kind: kind,
                            durableKey: durableKey,
                            attempt: 0,
                            completion: completion
                        )
                    case .success(nil):
                        self.submitJob(
                            transcriptionID: transcriptionID,
                            kind: kind,
                            request: request,
                            durableKey: durableKey,
                            completion: completion
                        )
                    case .failure(let error):
                        completion(.failure(error))
                    }
                }
            }
            return
        }
        submitJob(
            transcriptionID: transcriptionID,
            kind: kind,
            request: request,
            durableKey: durableKey,
            completion: completion
        )
    }

    private func submitJob(
        transcriptionID: String,
        kind: BetaSummaryJobKind,
        request: DurableJobRequest,
        durableKey: String,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    ) {
        client.createSummaryJob(
            transcriptionID: transcriptionID,
            kind: kind,
            templateID: request.templateID,
            markedMoments: request.markedMoments,
            background: true,
            sessionToken: session.sessionToken,
            idempotencyKey: request.idempotencyKey
        ) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let job):
                    self.defaults.set(job.id, forKey: durableKey + ".job")
                    self.resolveOrPoll(
                        job,
                        kind: kind,
                        durableKey: durableKey,
                        attempt: 0,
                        completion: completion
                    )
                case .failure(let error):
                    // Keep the request key. A response may have been lost after
                    // the backend reserved the job; the next explicit/automatic
                    // attempt reuses the idempotency key instead of duplicating it.
                    completion(.failure(error))
                }
            }
        }
    }

    private func durableRequest(durableKey: String) -> DurableJobRequest? {
        guard let data = defaults.data(forKey: durableKey + ".payload") else { return nil }
        return try? JSONDecoder().decode(DurableJobRequest.self, from: data)
    }

    private func persist(_ request: DurableJobRequest, durableKey: String) {
        defaults.set(request.idempotencyKey, forKey: durableKey + ".request")
        if let data = try? JSONEncoder().encode(request) {
            defaults.set(data, forKey: durableKey + ".payload")
        }
    }

    private func pollJob(
        id: String,
        kind: BetaSummaryJobKind,
        durableKey: String,
        attempt: Int,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    ) {
        client.getSummaryJob(id: id, sessionToken: session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let job):
                    self.resolveOrPoll(
                        job,
                        kind: kind,
                        durableKey: durableKey,
                        attempt: attempt,
                        completion: completion
                    )
                case .failure(let error):
                    // A transient poll failure must not forget a server job.
                    // Leave its durable identity intact for reopening/retry.
                    completion(.failure(error))
                }
            }
        }
    }

    private func resolveOrPoll(
        _ job: BetaSummaryJob,
        kind: BetaSummaryJobKind,
        durableKey: String,
        attempt: Int,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    ) {
        switch job.status {
        case .pending, .running, .generating:
            guard attempt < 180 else {
                completion(.failure(BetaIntelligenceError.server(
                    "This summary is still running. Reopen the conversation later; PinPoint will resume the same job."
                )))
                return
            }
            let delay: TimeInterval = attempt < 5 ? 2 : 5
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.pollJob(
                    id: job.id,
                    kind: kind,
                    durableKey: durableKey,
                    attempt: attempt + 1,
                    completion: completion
                )
            }
        case .proposed:
            if kind == .improve {
                completion(.success(job))
            } else {
                clearPending(durableKey)
                completion(.failure(BetaIntelligenceError.invalidResponse))
            }
        case .approved, .completed:
            clearPending(durableKey)
            if kind == .generate {
                completion(.success(job))
            } else {
                completion(.failure(BetaIntelligenceError.conflict(
                    "This wording proposal has already been applied. Reload the latest summary."
                )))
            }
        case .failed:
            clearPending(durableKey)
            completion(.failure(BetaIntelligenceError.server(
                readableFailure(job.errorMessage, fallback: "PinPoint could not generate this summary.")
            )))
        case .unknown, .uncertain:
            clearPending(durableKey)
            completion(.failure(BetaIntelligenceError.conflict(
                "PinPoint could not confirm the model result and will not repeat it automatically. You can start a fresh review safely."
            )))
        case .discarded:
            clearPending(durableKey)
            completion(.failure(BetaIntelligenceError.conflict(
                "That wording proposal was discarded. Start Improve wording again for a new review."
            )))
        }
    }

    private func pendingKey(kind: BetaSummaryJobKind, transcriptionID: String) -> String {
        "\(pendingPrefix).\(kind.rawValue).\(transcriptionID)"
    }

    private func clearPending(_ durableKey: String) {
        guard !durableKey.isEmpty else { return }
        defaults.removeObject(forKey: durableKey + ".request")
        defaults.removeObject(forKey: durableKey + ".job")
        defaults.removeObject(forKey: durableKey + ".payload")
    }

    private func readableFailure(_ reason: String?, fallback: String) -> String {
        switch reason {
        case "provider_unavailable": return "PinPoint Intelligence is not configured yet."
        case "transcript_unavailable": return "Plaud's transcript is temporarily unavailable."
        case "transcript_not_ready": return "Plaud is still preparing this transcript. Try again shortly."
        case "provider_rejected": return "The intelligence provider could not produce a valid summary."
        case "marked_moment_out_of_range": return "A marked moment fell outside this recording. Reconnect the recorder and try again."
        case "background_executor_unavailable": return "Background summary processing is temporarily unavailable."
        case .some: return fallback
        case .none: return fallback
        }
    }

    private func markedMomentOffsets(for recording: BetaRecording) -> [Int] {
        Array((markedMomentsProvider?(recording)?.tags ?? [])
            .map(\.timestamp)
            .filter { $0 >= 0 }
            .prefix(64))
    }

    private func scheduleAutomaticRetry(
        recording: BetaRecording,
        templateID: String,
        error: Error
    ) {
        guard let transcriptionID = recording.transcriptionID else { return }
        let attempt = (automaticRetryCounts[transcriptionID] ?? 0) + 1
        automaticRetryCounts[transcriptionID] = attempt
        onAutomaticStatusChange?(error.localizedDescription)
        guard attempt <= 4 else { return }
        let delays: [TimeInterval] = [15, 60, 300, 900]
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.automaticRetryWorkItems.removeValue(forKey: transcriptionID)
            self.ensureAutomaticSummary(for: recording, templateID: templateID)
        }
        automaticRetryWorkItems[transcriptionID] = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delays[attempt - 1], execute: work)
    }

    private func clearAutomaticFailure(_ transcriptionID: String) {
        automaticRetryWorkItems.removeValue(forKey: transcriptionID)?.cancel()
        automaticRetryCounts.removeValue(forKey: transcriptionID)
        onAutomaticStatusChange?(nil)
    }

    private static func idempotencyKey(
        userID: String,
        transcriptionID: String,
        purpose: String
    ) -> String {
        let digest = SHA256.hash(data: Data("\(userID)\u{0}\(transcriptionID)\u{0}\(purpose)".utf8))
        return "auto_" + digest.map { String(format: "%02x", $0) }.joined()
    }
}
