import Foundation

enum BetaCloudStage: Equatable {
    case uploading(Double)
    case transcribing(String)
    case ready(String)
}

private final class BetaCompletedPartsAccumulator: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [PinpointCompletedPart]

    init(_ values: [PinpointCompletedPart]) {
        self.values = values.sorted { $0.partNumber < $1.partNumber }
    }

    func append(_ value: PinpointCompletedPart) -> [PinpointCompletedPart] {
        lock.lock()
        defer { lock.unlock() }
        values.append(value)
        values.sort { $0.partNumber < $1.partNumber }
        return values
    }

    func snapshot() -> [PinpointCompletedPart] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }
}

// URLSession invokes this single-use pipeline from its callback queues. Mutable
// lifecycle state is protected by `cancellationLock`; upload progress has its
// own locked accumulator.
final class BetaCloudPipeline: @unchecked Sendable {
    private let session: BetaSession
    private let pinpointAPI: PinpointAPIClient
    private let urlSession: URLSession
    private let pollInterval: TimeInterval
    private let maxPolls: Int
    private let maxRetiredUploadRestarts = 2
    private let cancellationLock = NSLock()
    private var cancelled = false
    private var authorizationGate: () -> Bool = { true }

    init(
        session: BetaSession,
        pinpointAPI: PinpointAPIClient,
        urlSession: URLSession = URLSession(configuration: .ephemeral),
        pollInterval: TimeInterval = 5,
        maxPolls: Int = 240
    ) {
        self.session = session
        self.pinpointAPI = pinpointAPI
        self.urlSession = urlSession
        self.pollInterval = pollInterval
        self.maxPolls = maxPolls
    }

    func cancel() {
        cancellationLock.lock()
        cancelled = true
        cancellationLock.unlock()
        urlSession.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }

    func process(
        audioURL: URL?,
        sourceID: String,
        checkpoint: BetaCloudCheckpoint?,
        shouldContinue: @escaping () -> Bool,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        cancellationLock.lock()
        authorizationGate = shouldContinue
        cancellationLock.unlock()
        guard canContinue else { return }
        if let transcriptionID = checkpoint?.transcriptionID, !transcriptionID.isEmpty {
            onStage(.transcribing("Resuming"))
            pollTranscription(
                identifier: transcriptionID,
                attempt: 1,
                onStage: onStage,
                completion: completion
            )
            return
        }
        if let checkpoint {
            if checkpoint.uploadJobID.isEmpty,
               let requestID = checkpoint.createRequestID,
               !requestID.isEmpty {
                guard let audioURL else {
                    completion(.failure(BetaCloudError.emptyAudio))
                    return
                }
                guard let size = try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
                      size > 0 else {
                    completion(.failure(BetaCloudError.emptyAudio))
                    return
                }
                requestUpload(
                    audioURL: audioURL,
                    fileSize: size,
                    fileType: audioURL.pathExtension.lowercased(),
                    requestID: requestID,
                    sourceID: sourceID,
                    onStage: onStage,
                    onCheckpoint: onCheckpoint,
                    completion: completion
                )
            } else if let uploadParts = checkpoint.uploadParts,
               let chunkSize = checkpoint.chunkSize,
               chunkSize > 0,
               !uploadParts.isEmpty {
                if checkpoint.canResumeWithoutLocalAudio {
                    completeCheckpoint(
                        checkpoint,
                        audioURL: audioURL,
                        sourceID: sourceID,
                        retiredAttempt: 0,
                        onStage: onStage,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                } else if let audioURL {
                    resumeMultipartUpload(
                        audioURL: audioURL,
                        checkpoint: checkpoint,
                        sourceID: sourceID,
                        retiredAttempt: 0,
                        persistedUploadParts: uploadParts,
                        chunkSize: chunkSize,
                        onStage: onStage,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                } else {
                    completion(.failure(BetaCloudError.emptyAudio))
                }
            } else if !checkpoint.completedParts.isEmpty {
                // Compatibility with checkpoints written by the first Beta,
                // which persisted only after every part had uploaded.
                completeCheckpoint(
                    checkpoint,
                    audioURL: audioURL,
                    sourceID: sourceID,
                    retiredAttempt: 0,
                    onStage: onStage,
                    onCheckpoint: onCheckpoint,
                    completion: completion
                )
            } else {
                abandonCheckpoint(
                    checkpoint,
                    error: BetaCloudError.invalidResponse,
                    onCheckpoint: onCheckpoint,
                    completion: completion
                )
            }
            return
        }
        guard let audioURL else {
            completion(.failure(BetaCloudError.emptyAudio))
            return
        }
        guard let size = try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size > 0 else {
            completion(.failure(BetaCloudError.emptyAudio))
            return
        }
        let requestID = UUID().uuidString.lowercased()
        let pending = BetaCloudCheckpoint(
            createRequestID: requestID,
            uploadJobID: "",
            chunkSize: nil,
            uploadParts: nil,
            completedParts: [],
            transcriptionID: nil
        )
        // Persist the idempotency key before asking the backend to allocate
        // quota. A lost HTTP response can then retrieve the same job.
        guard onCheckpoint(pending) else {
            completion(.failure(BetaCloudError.checkpointPersistence))
            return
        }
        requestUpload(
            audioURL: audioURL,
            fileSize: size,
            fileType: audioURL.pathExtension.lowercased(),
            requestID: requestID,
            sourceID: sourceID,
            onStage: onStage,
            onCheckpoint: onCheckpoint,
            completion: completion
        )
    }

    private func requestUpload(
        audioURL: URL,
        fileSize: Int,
        fileType: String,
        requestID: String,
        sourceID: String,
        allocationAttempt: Int = 1,
        retiredAttempt: Int = 0,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        generatePresignedURLs(
            fileSize: fileSize,
            fileType: fileType,
            requestID: requestID,
            sourceID: sourceID
        ) { [weak self] result in
            guard let self, self.canContinue else { return }
            switch result {
            case .success(let upload):
                if let transcriptionID = upload.transcriptionID,
                   !transcriptionID.isEmpty {
                    let recovered = BetaCloudCheckpoint(
                        createRequestID: requestID,
                        uploadJobID: upload.jobID,
                        chunkSize: nil,
                        uploadParts: nil,
                        completedParts: [],
                        transcriptionID: transcriptionID
                    )
                    guard onCheckpoint(recovered) else {
                        completion(.failure(BetaCloudError.checkpointPersistence))
                        return
                    }
                    onStage(.transcribing("Resuming"))
                    self.pollTranscription(
                        identifier: transcriptionID,
                        attempt: 1,
                        onStage: onStage,
                        completion: completion
                    )
                    return
                }
                guard !upload.parts.isEmpty else {
                    onStage(.uploading(0))
                    self.pollUploadAllocation(
                        jobID: upload.jobID,
                        audioURL: audioURL,
                        fileSize: fileSize,
                        fileType: fileType,
                        requestID: requestID,
                        sourceID: sourceID,
                        attempt: allocationAttempt,
                        retiredAttempt: retiredAttempt,
                        onStage: onStage,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                    return
                }
                var persisted = BetaCloudCheckpoint(
                    createRequestID: requestID,
                    uploadJobID: upload.jobID,
                    chunkSize: upload.chunkSize,
                    uploadParts: upload.parts.map {
                        BetaCloudUploadPart(partNumber: $0.partNumber, presignedURL: $0.presignedURL)
                    },
                    completedParts: [],
                    transcriptionID: nil
                )
                guard onCheckpoint(persisted) else {
                    self.abandonCheckpoint(
                        persisted,
                        error: BetaCloudError.checkpointPersistence,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                    return
                }
                self.uploadParts(
                    audioURL: audioURL,
                    jobID: upload.jobID,
                    requestID: requestID,
                    parts: upload.parts,
                    chunkSize: upload.chunkSize,
                    completedParts: [],
                    onStage: onStage,
                    onProgress: { parts in
                        persisted.completedParts = parts.map {
                            BetaCloudCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
                        }
                        return onCheckpoint(persisted)
                    }
                ) { result in
                    guard self.canContinue else { return }
                    switch result {
                    case .success(let parts):
                        persisted.completedParts = parts.map {
                            BetaCloudCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
                        }
                        guard onCheckpoint(persisted) else {
                            completion(.failure(BetaCloudError.checkpointPersistence))
                            return
                        }
                        self.completeCheckpoint(
                            persisted,
                            audioURL: audioURL,
                            sourceID: sourceID,
                            retiredAttempt: retiredAttempt,
                            onStage: onStage,
                            onCheckpoint: onCheckpoint,
                            completion: completion
                        )
                    case .failure(let error):
                        if self.canSafelyRestartUpload(after: error) {
                            self.restartRetiredUpload(
                                audioURL: audioURL,
                                fileSize: fileSize,
                                fileType: fileType,
                                sourceID: sourceID,
                                retiredAttempt: retiredAttempt,
                                originalError: error,
                                onStage: onStage,
                                onCheckpoint: onCheckpoint,
                                completion: completion
                            )
                        } else {
                            self.handleUploadFailure(
                                error,
                                checkpoint: persisted,
                                onCheckpoint: onCheckpoint,
                                completion: completion
                            )
                        }
                    }
                }
            case .failure(let error):
                if self.canSafelyRestartUpload(after: error) {
                    self.restartRetiredUpload(
                        audioURL: audioURL,
                        fileSize: fileSize,
                        fileType: fileType,
                        sourceID: sourceID,
                        retiredAttempt: retiredAttempt,
                        originalError: error,
                        onStage: onStage,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                    return
                }
                // The pending checkpoint remains. Retrying uses the same
                // request id when a response may merely have been lost. A 410
                // is handled above by atomically persisting a fresh attempt in
                // this same pipeline; it never replays the retired one.
                completion(.failure(error))
            }
        }
    }

    private func restartRetiredUpload(
        audioURL: URL,
        fileSize: Int,
        fileType: String,
        sourceID: String,
        retiredAttempt: Int,
        originalError: Error,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        guard onCheckpoint(nil) else {
            completion(.failure(BetaCloudError.checkpointPersistence))
            return
        }
        guard retiredAttempt < maxRetiredUploadRestarts else {
            completion(.failure(originalError))
            return
        }
        let requestID = UUID().uuidString.lowercased()
        let pending = BetaCloudCheckpoint(
            createRequestID: requestID,
            uploadJobID: "",
            chunkSize: nil,
            uploadParts: nil,
            completedParts: [],
            transcriptionID: nil
        )
        guard onCheckpoint(pending) else {
            completion(.failure(BetaCloudError.checkpointPersistence))
            return
        }
        onStage(.uploading(0))
        requestUpload(
            audioURL: audioURL,
            fileSize: fileSize,
            fileType: fileType,
            requestID: requestID,
            sourceID: sourceID,
            allocationAttempt: 1,
            retiredAttempt: retiredAttempt + 1,
            onStage: onStage,
            onCheckpoint: onCheckpoint,
            completion: completion
        )
    }

    private func generatePresignedURLs(
        fileSize: Int,
        fileType: String,
        requestID: String,
        sourceID: String,
        completion: @escaping (Result<PinpointUploadPlan, Error>) -> Void
    ) {
        pinpointAPI.createUpload(
            fileSize: fileSize,
            fileType: fileType,
            requestID: requestID,
            sourceID: sourceID,
            sessionToken: session.sessionToken,
            completion: completion
        )
    }

    private func pollUploadAllocation(
        jobID: String,
        audioURL: URL,
        fileSize: Int,
        fileType: String,
        requestID: String,
        sourceID: String,
        attempt: Int,
        retiredAttempt: Int,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        guard attempt <= maxPolls else {
            completion(.failure(BetaCloudError.uploadAllocationTimedOut))
            return
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + pollInterval) { [weak self] in
            guard let self, self.canContinue else { return }
            self.pinpointAPI.getUploadJob(
                jobID: jobID,
                sessionToken: self.session.sessionToken
            ) { result in
                guard self.canContinue else { return }
                switch result {
                case .success(let status):
                    if let transcriptionID = status.transcriptionID,
                       !transcriptionID.isEmpty {
                        let recovered = BetaCloudCheckpoint(
                            createRequestID: requestID,
                            uploadJobID: jobID,
                            chunkSize: nil,
                            uploadParts: nil,
                            completedParts: [],
                            transcriptionID: transcriptionID
                        )
                        guard onCheckpoint(recovered) else {
                            completion(.failure(BetaCloudError.checkpointPersistence))
                            return
                        }
                        onStage(.transcribing("Resuming"))
                        self.pollTranscription(
                            identifier: transcriptionID,
                            attempt: 1,
                            onStage: onStage,
                            completion: completion
                        )
                    } else if status.state == "uploaded" {
                        self.pinpointAPI.resumeUploadSubmission(
                            jobID: jobID,
                            sessionToken: self.session.sessionToken
                        ) { result in
                            guard self.canContinue else { return }
                            switch result {
                            case .success(let transcriptionID):
                                let recovered = BetaCloudCheckpoint(
                                    createRequestID: requestID,
                                    uploadJobID: jobID,
                                    chunkSize: nil,
                                    uploadParts: nil,
                                    completedParts: [],
                                    transcriptionID: transcriptionID
                                )
                                guard onCheckpoint(recovered) else {
                                    completion(.failure(BetaCloudError.checkpointPersistence))
                                    return
                                }
                                onStage(.transcribing("Resuming"))
                                self.pollTranscription(
                                    identifier: transcriptionID,
                                    attempt: 1,
                                    onStage: onStage,
                                    completion: completion
                                )
                            case .failure(let error):
                                if let apiError = error as? PinpointAPIError,
                                   case .server(409, let code, _) = apiError,
                                   code == "transcription_submission_in_progress" {
                                    self.pollUploadAllocation(
                                        jobID: jobID,
                                        audioURL: audioURL,
                                        fileSize: fileSize,
                                        fileType: fileType,
                                        requestID: requestID,
                                        sourceID: sourceID,
                                        attempt: attempt + 1,
                                        retiredAttempt: retiredAttempt,
                                        onStage: onStage,
                                        onCheckpoint: onCheckpoint,
                                        completion: completion
                                    )
                                } else {
                                    completion(.failure(error))
                                }
                            }
                        }
                    } else if status.state == "uploading" {
                        // The same attempt receives its persisted multipart
                        // plan. A second Mac receives poll-only state until the
                        // canonical attempt submits or safely retires.
                        self.requestUpload(
                            audioURL: audioURL,
                            fileSize: fileSize,
                            fileType: fileType,
                            requestID: requestID,
                            sourceID: sourceID,
                            allocationAttempt: attempt + 1,
                            retiredAttempt: retiredAttempt,
                            onStage: onStage,
                            onCheckpoint: onCheckpoint,
                            completion: completion
                        )
                    } else {
                        onStage(.uploading(0))
                        self.pollUploadAllocation(
                            jobID: jobID,
                            audioURL: audioURL,
                            fileSize: fileSize,
                            fileType: fileType,
                            requestID: requestID,
                            sourceID: sourceID,
                            attempt: attempt + 1,
                            retiredAttempt: retiredAttempt,
                            onStage: onStage,
                            onCheckpoint: onCheckpoint,
                            completion: completion
                        )
                    }
                case .failure(let error):
                    if self.canSafelyRestartUpload(after: error) {
                        self.restartRetiredUpload(
                            audioURL: audioURL,
                            fileSize: fileSize,
                            fileType: fileType,
                            sourceID: sourceID,
                            retiredAttempt: retiredAttempt,
                            originalError: error,
                            onStage: onStage,
                            onCheckpoint: onCheckpoint,
                            completion: completion
                        )
                        return
                    }
                    completion(.failure(error))
                }
            }
        }
    }

    private func uploadParts(
        audioURL: URL,
        jobID: String,
        requestID: String,
        parts: [PinpointUploadPart],
        chunkSize: Int,
        completedParts: [PinpointCompletedPart],
        onStage: @escaping (BetaCloudStage) -> Void,
        onProgress: @escaping ([PinpointCompletedPart]) -> Bool,
        completion: @escaping (Result<[PinpointCompletedPart], Error>) -> Void
    ) {
        let sortedParts = parts.sorted { $0.partNumber < $1.partNumber }
        let completedNumbers = Set(completedParts.map(\.partNumber))
        guard !sortedParts.isEmpty,
              chunkSize > 0,
              completedNumbers.isSubset(of: Set(sortedParts.map(\.partNumber))) else {
            completion(.failure(BetaCloudError.invalidResponse))
            return
        }
        let pendingParts = sortedParts.filter { !completedNumbers.contains($0.partNumber) }
        let completed = BetaCompletedPartsAccumulator(completedParts)

        @Sendable
        func uploadPart(_ part: PinpointUploadPart, index: Int) {
            let (offsetValue, overflow) = max(0, part.partNumber - 1)
                .multipliedReportingOverflow(by: chunkSize)
            guard !overflow, offsetValue >= 0 else {
                completion(.failure(BetaCloudError.invalidResponse))
                return
            }
            let offset = UInt64(offsetValue)
            do {
                let handle = try FileHandle(forReadingFrom: audioURL)
                try handle.seek(toOffset: offset)
                let data = handle.readData(ofLength: chunkSize)
                try handle.close()
                guard !data.isEmpty, let url = URL(string: part.presignedURL) else {
                    completion(.failure(BetaCloudError.invalidResponse))
                    return
                }
                var request = URLRequest(url: url)
                request.httpMethod = "PUT"
                request.timeoutInterval = 180
                urlSession.uploadTask(with: request, from: data) { _, response, error in
                    guard self.canContinue else { return }
                    if let error {
                        completion(.failure(BetaCloudError.network(error.localizedDescription)))
                        return
                    }
                    guard let http = response as? HTTPURLResponse else {
                        completion(.failure(BetaCloudError.invalidResponse))
                        return
                    }
                    guard (200...299).contains(http.statusCode) else {
                        if (400...499).contains(http.statusCode),
                           ![408, 429].contains(http.statusCode) {
                            completion(.failure(BetaCloudError.uploadRejected(http.statusCode)))
                        } else {
                            completion(.failure(BetaCloudError.uploadUnavailable(http.statusCode)))
                        }
                        return
                    }
                    let etag = http.value(forHTTPHeaderField: "ETag")?
                        .replacingOccurrences(of: "\"", with: "") ?? ""
                    guard !etag.isEmpty else {
                        completion(.failure(BetaCloudError.invalidResponse))
                        return
                    }
                    self.pinpointAPI.heartbeatUpload(
                        jobID: jobID,
                        requestID: requestID,
                        sessionToken: self.session.sessionToken
                    ) { heartbeatResult in
                        guard self.canContinue else { return }
                        switch heartbeatResult {
                        case .success:
                            let updated = completed.append(
                                PinpointCompletedPart(partNumber: part.partNumber, etag: etag)
                            )
                            guard onProgress(updated) else {
                                completion(.failure(BetaCloudError.checkpointPersistence))
                                return
                            }
                            onStage(.uploading(Double(updated.count) / Double(sortedParts.count)))
                            uploadNext(index + 1)
                        case .failure(let error):
                            completion(.failure(error))
                        }
                    }
                }.resume()
            } catch {
                completion(.failure(BetaCloudError.cannotReadAudio))
            }
        }

        @Sendable
        func uploadNext(_ index: Int) {
            guard canContinue else { return }
            guard index < pendingParts.count else {
                completion(.success(completed.snapshot()))
                return
            }
            pinpointAPI.heartbeatUpload(
                jobID: jobID,
                requestID: requestID,
                sessionToken: session.sessionToken
            ) { result in
                guard self.canContinue else { return }
                switch result {
                case .success:
                    uploadPart(pendingParts[index], index: index)
                case .failure(let error):
                    completion(.failure(error))
                }
            }
        }
        onStage(.uploading(Double(completed.snapshot().count) / Double(sortedParts.count)))
        uploadNext(0)
    }

    private func resumeMultipartUpload(
        audioURL: URL,
        checkpoint: BetaCloudCheckpoint,
        sourceID: String,
        retiredAttempt: Int,
        persistedUploadParts: [BetaCloudUploadPart],
        chunkSize: Int,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        var persisted = checkpoint
        guard let requestID = checkpoint.createRequestID,
              !requestID.isEmpty,
              let fileSize = try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              fileSize > 0 else {
            completion(.failure(BetaCloudError.invalidResponse))
            return
        }
        let fileType = audioURL.pathExtension.lowercased()
        let parts = persistedUploadParts.map {
            PinpointUploadPart(partNumber: $0.partNumber, presignedURL: $0.presignedURL)
        }
        let completed = checkpoint.completedParts.map {
            PinpointCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
        }
        self.uploadParts(
            audioURL: audioURL,
            jobID: checkpoint.uploadJobID,
            requestID: requestID,
            parts: parts,
            chunkSize: chunkSize,
            completedParts: completed,
            onStage: onStage,
            onProgress: { parts in
                persisted.completedParts = parts.map {
                    BetaCloudCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
                }
                return onCheckpoint(persisted)
            }
        ) { [weak self] result in
            guard let self, self.canContinue else { return }
            switch result {
            case .success(let parts):
                persisted.completedParts = parts.map {
                    BetaCloudCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
                }
                guard onCheckpoint(persisted) else {
                    completion(.failure(BetaCloudError.checkpointPersistence))
                    return
                }
                self.completeCheckpoint(
                    persisted,
                    audioURL: audioURL,
                    sourceID: sourceID,
                    retiredAttempt: retiredAttempt,
                    onStage: onStage,
                    onCheckpoint: onCheckpoint,
                    completion: completion
                )
            case .failure(let error):
                if self.canSafelyRestartUpload(after: error) {
                    self.restartRetiredUpload(
                        audioURL: audioURL,
                        fileSize: fileSize,
                        fileType: fileType,
                        sourceID: sourceID,
                        retiredAttempt: retiredAttempt,
                        originalError: error,
                        onStage: onStage,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                } else {
                    self.handleUploadFailure(
                        error,
                        checkpoint: persisted,
                        onCheckpoint: onCheckpoint,
                        completion: completion
                    )
                }
            }
        }
    }

    private func completeCheckpoint(
        _ checkpoint: BetaCloudCheckpoint,
        audioURL: URL?,
        sourceID: String,
        retiredAttempt: Int,
        onStage: @escaping (BetaCloudStage) -> Void,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        let parts = checkpoint.completedParts.map {
            PinpointCompletedPart(partNumber: $0.partNumber, etag: $0.etag)
        }
        guard !parts.isEmpty,
              let requestID = checkpoint.createRequestID,
              !requestID.isEmpty else {
            abandonCheckpoint(
                checkpoint,
                error: BetaCloudError.invalidResponse,
                onCheckpoint: onCheckpoint,
                completion: completion
            )
            return
        }
        onStage(.transcribing("Submitting"))
        // `onStage` is also the caller's durable metadata gate. It cancels
        // this pipeline when that write fails, so re-check before the
        // irreversible upload-completion/transcription side effect.
        guard canContinue else { return }
        completeUpload(
            jobID: checkpoint.uploadJobID,
            parts: parts,
            requestID: requestID
        ) { [weak self] result in
            guard let self, self.canContinue else { return }
            switch result {
            case .success(let identifier):
                var updated = checkpoint
                updated.transcriptionID = identifier
                // This write happens before polling, so a process exit after
                // Plaud accepted the request can only resume the same task.
                guard onCheckpoint(updated) else {
                    completion(.failure(BetaCloudError.checkpointPersistence))
                    return
                }
                self.pollTranscription(
                    identifier: identifier,
                    attempt: 1,
                    onStage: onStage,
                    completion: completion
                )
            case .failure(let error):
                if self.canSafelyRestartUpload(after: error) {
                    if let audioURL,
                       let fileSize = try? audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
                       fileSize > 0 {
                        self.restartRetiredUpload(
                            audioURL: audioURL,
                            fileSize: fileSize,
                            fileType: audioURL.pathExtension.lowercased(),
                            sourceID: sourceID,
                            retiredAttempt: retiredAttempt,
                            originalError: error,
                            onStage: onStage,
                            onCheckpoint: onCheckpoint,
                            completion: completion
                        )
                        return
                    }
                    // The backend has proved this pre-completion attempt can no
                    // longer advance. Keeping an all-parts checkpoint would make
                    // every later retry call the same retired job forever. Clear
                    // it durably so the device layer can reacquire the recording
                    // from its recorder, or freeze it for support when that
                    // recorder is no longer associated with this account.
                    guard onCheckpoint(nil) else {
                        completion(.failure(BetaCloudError.checkpointPersistence))
                        return
                    }
                    completion(.failure(BetaCloudError.localAudioRequiredForSafeRestart))
                    return
                }
                completion(.failure(error))
            }
        }
    }

    private func handleUploadFailure(
        _ error: Error,
        checkpoint: BetaCloudCheckpoint,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        switch error {
        case BetaCloudError.uploadRejected, BetaCloudError.invalidResponse:
            abandonCheckpoint(
                checkpoint,
                error: error,
                onCheckpoint: onCheckpoint,
                completion: completion
            )
        default:
            completion(.failure(error))
        }
    }

    private func abandonCheckpoint(
        _ checkpoint: BetaCloudCheckpoint,
        error: Error,
        onCheckpoint: @escaping (BetaCloudCheckpoint?) -> Bool,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        guard let requestID = checkpoint.createRequestID,
              !requestID.isEmpty else {
            completion(.failure(BetaCloudError.invalidResponse))
            return
        }
        pinpointAPI.abandonUpload(
            jobID: checkpoint.uploadJobID,
            requestID: requestID,
            sessionToken: session.sessionToken
        ) { [weak self] result in
            guard let self, self.canContinue else { return }
            if case .success = result {
                _ = onCheckpoint(nil)
            }
            // Preserve the original cause for the row. If abandoning failed,
            // its checkpoint remains available for a safe later retry.
            completion(.failure(error))
        }
    }

    private func completeUpload(
        jobID: String,
        parts: [PinpointCompletedPart],
        requestID: String,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        pinpointAPI.completeUploadAndSubmit(
            jobID: jobID,
            parts: parts,
            requestID: requestID,
            sessionToken: session.sessionToken,
            completion: completion
        )
    }

    private func pollTranscription(
        identifier: String,
        attempt: Int,
        onStage: @escaping (BetaCloudStage) -> Void,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        guard canContinue else { return }
        guard attempt <= maxPolls else {
            completion(.failure(BetaCloudError.transcriptionTimedOut))
            return
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + pollInterval) { [weak self] in
            guard let self, self.canContinue else { return }
            self.pinpointAPI.getTranscription(identifier: identifier, sessionToken: self.session.sessionToken) { result in
                guard self.canContinue else { return }
                switch result {
                case .success(.completed(let text)):
                    onStage(.ready(text))
                    completion(.success(text))
                case .success(.processing(let status)):
                    onStage(.transcribing(status))
                    self.pollTranscription(identifier: identifier, attempt: attempt + 1, onStage: onStage, completion: completion)
                case .success(.failed(let message)):
                    completion(.failure(BetaCloudError.transcriptionFailed(message)))
                case .failure(let error):
                    completion(.failure(error))
                }
            }
        }
    }

    private var canContinue: Bool {
        cancellationLock.lock()
        let isCancelled = cancelled
        let gate = authorizationGate
        cancellationLock.unlock()
        return !isCancelled && gate()
    }

    private func canSafelyRestartUpload(after error: Error) -> Bool {
        guard let apiError = error as? PinpointAPIError,
              case .server(let status, let code, _) = apiError else { return false }
        return status == 410 || (status == 409 && code == "upload_attempt_mismatch")
    }
}

enum BetaCloudError: LocalizedError {
    case emptyAudio
    case cannotReadAudio
    case uploadRejected(Int)
    case uploadUnavailable(Int)
    case invalidResponse
    case network(String)
    case transcriptionTimedOut
    case uploadAllocationTimedOut
    case transcriptionFailed(String)
    case checkpointPersistence
    case localAudioRequiredForSafeRestart

    var errorDescription: String? {
        switch self {
        case .emptyAudio: return "The copied recording is empty."
        case .cannotReadAudio: return "PinPoint could not read the copied recording."
        case .uploadRejected: return "The recording upload link expired. Retry to request a fresh one."
        case .uploadUnavailable: return "Plaud’s upload service is temporarily unavailable. Retry will resume this upload."
        case .invalidResponse: return "Plaud returned an unexpected response."
        case .network: return "The network connection was interrupted."
        case .transcriptionTimedOut: return "Transcription is taking longer than expected and will need a retry."
        case .uploadAllocationTimedOut: return "Another PinPoint session is still handling this recording. Retry after it finishes."
        case .transcriptionFailed(let message): return message
        case .checkpointPersistence: return "PinPoint could not safely save upload progress. Free disk space, then retry."
        case .localAudioRequiredForSafeRestart:
            return "The previous upload attempt ended safely. PinPoint needs to copy this recording from the recorder again."
        }
    }
}
