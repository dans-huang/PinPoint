import Foundation

final class BetaIntelligenceAPIClient: BetaIntelligenceProviding {
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL, session: URLSession? = nil) {
        self.baseURL = baseURL
        if let session {
            self.session = session
        } else {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
            configuration.urlCache = nil
            self.session = URLSession(configuration: configuration)
        }
    }

    func getSettings(sessionToken: String, completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void) {
        request(path: "v1/intelligence/settings", token: sessionToken) { result in
            completion(result.flatMap(Self.parseSettings))
        }
    }

    func updateSettings(
        _ settings: BetaIntelligenceSettings,
        sessionToken: String,
        completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void
    ) {
        request(
            path: "v1/intelligence/settings",
            method: "PUT",
            token: sessionToken,
            body: [
                "auto_summary_enabled": settings.automaticSummaryEnabled,
                "default_template_id": settings.defaultTemplateID,
            ]
        ) { result in
            completion(result.flatMap(Self.parseSettings))
        }
    }

    func listTemplates(sessionToken: String, completion: @escaping (Result<[BetaSummaryTemplate], Error>) -> Void) {
        request(path: "v1/intelligence/templates", token: sessionToken) { result in
            completion(result.flatMap { object in
                guard let rows = object["templates"] as? [[String: Any]] else {
                    return .failure(BetaIntelligenceError.invalidResponse)
                }
                let values = rows.compactMap(Self.template)
                guard values.count == rows.count else {
                    return .failure(BetaIntelligenceError.invalidResponse)
                }
                return .success(values)
            })
        }
    }

    func createTemplate(
        name: String,
        prompt: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void
    ) {
        request(
            path: "v1/intelligence/templates",
            method: "POST",
            token: sessionToken,
            body: ["name": name, "instructions": prompt]
        ) { result in
            completion(result.flatMap { object in
                Self.template(object).map(Result.success) ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func updateTemplate(
        id: String,
        name: String,
        prompt: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void
    ) {
        guard Self.safeIdentifier(id) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(
            path: "v1/intelligence/templates/\(id)",
            method: "PATCH",
            token: sessionToken,
            body: ["name": name, "instructions": prompt]
        ) { result in
            completion(result.flatMap { object in
                Self.template(object).map(Result.success) ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func archiveTemplate(
        id: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        guard Self.safeIdentifier(id) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(path: "v1/intelligence/templates/\(id)", method: "DELETE", token: sessionToken) {
            completion($0.map { _ in () })
        }
    }

    func listVocabulary(sessionToken: String, completion: @escaping (Result<[BetaVocabularyTerm], Error>) -> Void) {
        request(path: "v1/intelligence/vocabulary", token: sessionToken) { result in
            completion(result.flatMap { object in
                guard let rows = object["terms"] as? [[String: Any]] else {
                    return .failure(BetaIntelligenceError.invalidResponse)
                }
                let values = rows.compactMap(Self.vocabulary)
                guard values.count == rows.count else {
                    return .failure(BetaIntelligenceError.invalidResponse)
                }
                return .success(values)
            })
        }
    }

    func addVocabulary(
        term: String,
        sessionToken: String,
        completion: @escaping (Result<BetaVocabularyTerm, Error>) -> Void
    ) {
        request(
            path: "v1/intelligence/vocabulary",
            method: "POST",
            token: sessionToken,
            body: ["term": term]
        ) { result in
            completion(result.flatMap { object in
                Self.vocabulary(object).map(Result.success) ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func removeVocabulary(id: String, sessionToken: String, completion: @escaping (Result<Void, Error>) -> Void) {
        guard Self.safeIdentifier(id) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(path: "v1/intelligence/vocabulary/\(id)", method: "DELETE", token: sessionToken) {
            completion($0.map { _ in () })
        }
    }

    func getSummary(
        transcriptionID: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummarySnapshot?, Error>) -> Void
    ) {
        guard Self.safeIdentifier(transcriptionID) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(
            path: "v1/transcriptions/\(transcriptionID)/summary",
            token: sessionToken,
            allowsNotFound: true
        ) { result in
            completion(result.flatMap { object in
                if object.isEmpty { return .success(nil) }
                return Self.summary(object).map { .success($0) }
                    ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

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
        guard Self.safeIdentifier(transcriptionID), Self.safeIdentifier(idempotencyKey) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        var body: [String: Any] = [
            "idempotency_key": idempotencyKey,
            "mode": kind.rawValue,
            "execution": background ? "background" : "inline",
            "marked_moments": Array(markedMoments.filter { $0 >= 0 }.prefix(64)),
        ]
        if let templateID { body["template_id"] = templateID }
        request(
            path: "v1/transcriptions/\(transcriptionID)/summary-jobs",
            method: "POST",
            token: sessionToken,
            body: body
        ) { result in
            completion(result.flatMap { object in
                Self.job(object).map(Result.success) ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func getSummaryJob(id: String, sessionToken: String, completion: @escaping (Result<BetaSummaryJob, Error>) -> Void) {
        guard Self.safeIdentifier(id) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(path: "v1/intelligence/summary-jobs/\(id)", token: sessionToken) { result in
            completion(result.flatMap { object in
                Self.job(object).map(Result.success) ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func findSummaryJob(
        idempotencyKey: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryJob?, Error>) -> Void
    ) {
        guard Self.safeIdentifier(idempotencyKey) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(
            path: "v1/intelligence/summary-jobs/by-request/\(idempotencyKey)",
            token: sessionToken,
            allowsNotFound: true
        ) { result in
            completion(result.flatMap { object in
                if object.isEmpty { return .success(nil) }
                return Self.job(object).map { .success($0) }
                    ?? .failure(BetaIntelligenceError.invalidResponse)
            })
        }
    }

    func approveSummaryJob(
        id: String,
        expectedVersion: Int?,
        expectedHash: String?,
        approvedVocabulary: [String],
        sessionToken: String,
        completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void
    ) {
        guard Self.safeIdentifier(id), let expectedVersion, let expectedHash else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(
            path: "v1/intelligence/summary-jobs/\(id)/approve",
            method: "POST",
            token: sessionToken,
            body: [
                "proposal_hash": expectedHash,
                "base_version": expectedVersion,
                "accepted_terms": Array(approvedVocabulary.prefix(20)),
            ]
        ) { result in
            completion(result.flatMap { object in
                guard let nested = object["summary"] as? [String: Any],
                      let value = Self.summary(nested) else {
                    return .failure(BetaIntelligenceError.invalidResponse)
                }
                return .success(value)
            })
        }
    }

    func discardSummaryJob(id: String, sessionToken: String, completion: @escaping (Result<Void, Error>) -> Void) {
        guard Self.safeIdentifier(id) else {
            completion(.failure(BetaIntelligenceError.invalidResponse))
            return
        }
        request(path: "v1/intelligence/summary-jobs/\(id)/discard", method: "POST", token: sessionToken, body: [:]) {
            completion($0.map { _ in () })
        }
    }

    private func request(
        path: String,
        method: String = "GET",
        token: String,
        body: [String: Any]? = nil,
        allowsNotFound: Bool = false,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        let url = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 75
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("PinPoint/0.2", forHTTPHeaderField: "User-Agent")
        if let body {
            guard JSONSerialization.isValidJSONObject(body),
                  let data = try? JSONSerialization.data(withJSONObject: body) else {
                completion(.failure(BetaIntelligenceError.invalidResponse))
                return
            }
            request.httpBody = data
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        session.dataTask(with: request) { data, response, error in
            if error != nil {
                completion(.failure(PinpointAPIError.network("network")))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                completion(.failure(BetaIntelligenceError.invalidResponse))
                return
            }
            if allowsNotFound && http.statusCode == 404 {
                completion(.success([:]))
                return
            }
            guard let data, data.count <= 2 * 1_024 * 1_024 else {
                completion(.failure(BetaIntelligenceError.invalidResponse))
                return
            }
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            guard (200...299).contains(http.statusCode) else {
                if http.statusCode == 401 {
                    completion(.failure(PinpointAPIError.sessionExpired))
                    return
                }
                let rawDetail = object["detail"]
                let detail: String
                if let string = rawDetail as? String {
                    detail = string
                } else if let dictionary = rawDetail as? [String: Any],
                          let message = dictionary["message"] as? String {
                    detail = message
                } else {
                    detail = http.statusCode == 503
                        ? "PinPoint Intelligence is not configured yet."
                        : "PinPoint could not finish this intelligence action."
                }
                let error: Error = http.statusCode == 409
                    ? BetaIntelligenceError.conflict(detail)
                    : (http.statusCode == 503 ? BetaIntelligenceError.unavailable : BetaIntelligenceError.server(detail))
                completion(.failure(error))
                return
            }
            completion(.success(object))
        }.resume()
    }

    private static func parseSettings(_ object: [String: Any]) -> Result<BetaIntelligenceSettings, Error> {
        guard let enabled = object["auto_summary_enabled"] as? Bool,
              let template = object["default_template_id"] as? String,
              safeIdentifier(template) else {
            return .failure(BetaIntelligenceError.invalidResponse)
        }
        return .success(BetaIntelligenceSettings(
            automaticSummaryEnabled: enabled,
            defaultTemplateID: template
        ))
    }

    private static func template(_ object: [String: Any]) -> BetaSummaryTemplate? {
        guard let id = object["template_id"] as? String,
              let name = object["name"] as? String,
              let prompt = object["instructions"] as? String,
              let builtIn = object["builtin"] as? Bool,
              let state = object["state"] as? String,
              safeIdentifier(id) else { return nil }
        return BetaSummaryTemplate(
            id: id,
            name: name,
            prompt: prompt,
            isBuiltIn: builtIn,
            isArchived: state == "archived",
            version: (object["version"] as? NSNumber)?.intValue ?? 1
        )
    }

    private static func vocabulary(_ object: [String: Any]) -> BetaVocabularyTerm? {
        guard let id = object["term_id"] as? String,
              let term = object["term"] as? String,
              safeIdentifier(id), !term.isEmpty else { return nil }
        return BetaVocabularyTerm(id: id, term: term)
    }

    private static func summary(_ object: [String: Any]) -> BetaSummarySnapshot? {
        guard let id = object["transcription_id"] as? String,
              let text = object["summary_text"] as? String,
              let version = (object["version"] as? NSNumber)?.intValue,
              let hash = object["summary_hash"] as? String,
              safeIdentifier(id), !text.isEmpty else { return nil }
        let updatedAt = (object["updated_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        return BetaSummarySnapshot(
            transcriptionID: id,
            text: text,
            version: version,
            contentHash: hash,
            templateID: object["template_id"] as? String,
            updatedAt: updatedAt
        )
    }

    private static func job(_ object: [String: Any]) -> BetaSummaryJob? {
        guard let id = object["job_id"] as? String,
              let transcriptionID = object["transcription_id"] as? String,
              let stateText = object["state"] as? String,
              let status = BetaSummaryJobStatus(rawValue: stateText),
              safeIdentifier(id), safeIdentifier(transcriptionID) else { return nil }
        let kind = BetaSummaryJobKind(rawValue: object["mode"] as? String ?? "")
            ?? (((object["base_summary_version"] as? NSNumber)?.intValue ?? 0) > 0 ? .improve : .generate)
        return BetaSummaryJob(
            id: id,
            transcriptionID: transcriptionID,
            kind: kind,
            status: status,
            proposedSummary: object["proposed_summary"] as? String,
            proposedVocabulary: object["proposed_terms"] as? [String] ?? [],
            baseSummaryVersion: (object["base_summary_version"] as? NSNumber)?.intValue,
            baseSummaryHash: object["proposal_hash"] as? String,
            errorMessage: object["failure_reason"] as? String
        )
    }

    private static func safeIdentifier(_ value: String) -> Bool {
        guard !value.isEmpty, value.count <= 256 else { return false }
        return value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.contains($0) || $0 == "_" || $0 == "-"
        }
    }
}
