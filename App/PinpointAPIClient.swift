import Foundation

final class PinpointAPIClient {
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL, session: URLSession? = nil) {
        self.baseURL = baseURL
        self.session = session ?? URLSession(configuration: .ephemeral)
    }

    func requestSignInNonce(completion: @escaping (Result<String, Error>) -> Void) {
        let url = baseURL.appendingPathComponent("v1/session/nonce")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("PinPoint/1.0", forHTTPHeaderField: "User-Agent")
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(PinpointAPIError.network(error.localizedDescription)))
                return
            }
            guard let http = response as? HTTPURLResponse,
                  (200...299).contains(http.statusCode),
                  let data,
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let nonce = object["nonce"] as? String,
                  !nonce.isEmpty else {
                completion(.failure(PinpointAPIError.invalidResponse))
                return
            }
            completion(.success(nonce))
        }.resume()
    }

    func signInWithApple(
        identityToken: String,
        authorizationCode: String?,
        nonce: String,
        inviteCode: String?,
        completion: @escaping (Result<BetaSession, Error>) -> Void
    ) {
        var body: [String: Any] = [
            "identity_token": identityToken,
            "nonce": nonce,
        ]
        if let authorizationCode { body["authorization_code"] = authorizationCode }
        if let inviteCode { body["invite_code"] = inviteCode }
        perform(path: "v1/session/apple", method: "POST", body: body, completion: completion)
    }

    func createLocalSession(
        activationCode: String,
        completion: @escaping (Result<BetaSession, Error>) -> Void
    ) {
        perform(
            path: "v1/session/local",
            method: "POST",
            body: ["activation_code": activationCode],
            completion: completion
        )
    }

    func refresh(
        sessionToken: String,
        completion: @escaping (Result<BetaSession, Error>) -> Void
    ) {
        perform(
            path: "v1/session/refresh",
            method: "POST",
            bearerToken: sessionToken,
            body: [:],
            completion: completion
        )
    }

    func validateSession(
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/session/status",
            method: "GET",
            bearerToken: sessionToken,
            body: nil
        ) { result in
            switch result {
            case .success(let object):
                guard object["status"] as? String == "active" else {
                    completion(.failure(PinpointAPIError.invalidResponse))
                    return
                }
                completion(.success(()))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func revokeSession(
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/session/logout",
            method: "POST",
            bearerToken: sessionToken,
            body: [:]
        ) { result in
            switch result {
            case .success: completion(.success(()))
            case .failure(let error): completion(.failure(error))
            }
        }
    }

    func bindRecorder(
        serialNumber: String,
        model: PlaudDeviceModel,
        sessionToken: String,
        completion: @escaping (Result<Void, PartnerBindingError>) -> Void
    ) {
        setRecorderBinding(
            action: "bind",
            serialNumber: serialNumber,
            model: model,
            sessionToken: sessionToken,
            completion: completion
        )
    }

    func unbindRecorder(
        serialNumber: String,
        model: PlaudDeviceModel,
        sessionToken: String,
        completion: @escaping (Result<Void, PartnerBindingError>) -> Void
    ) {
        setRecorderBinding(
            action: "unbind",
            serialNumber: serialNumber,
            model: model,
            sessionToken: sessionToken,
            completion: completion
        )
    }

    func createUpload(
        fileSize: Int,
        fileType: String,
        requestID: String,
        sourceID: String,
        sessionToken: String,
        completion: @escaping (Result<PinpointUploadPlan, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads",
            method: "POST",
            bearerToken: sessionToken,
            body: [
                "file_size": fileSize,
                "file_type": fileType,
                "idempotency_key": requestID,
                "source_id": sourceID,
            ]
        ) { result in
            switch result {
            case .success(let object):
                guard let data = try? JSONSerialization.data(withJSONObject: object),
                      let plan = try? JSONDecoder().decode(PinpointUploadPlan.self, from: data) else {
                    completion(.failure(PinpointAPIError.invalidResponse))
                    return
                }
                completion(.success(plan))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func completeUploadAndSubmit(
        jobID: String,
        parts: [PinpointCompletedPart],
        requestID: String,
        sessionToken: String,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads/\(jobID)/complete",
            method: "POST",
            bearerToken: sessionToken,
            body: [
                "part_list": parts.map { ["PartNumber": $0.partNumber, "ETag": $0.etag] },
                "idempotency_key": requestID,
            ]
        ) { result in
            switch result {
            case .success(let object):
                let data = object["data"] as? [String: Any]
                let identifier = object["transcription_id"] as? String
                    ?? data?["task_id"] as? String
                guard let identifier, !identifier.isEmpty else {
                    completion(.failure(PinpointAPIError.invalidResponse))
                    return
                }
                completion(.success(identifier))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func heartbeatUpload(
        jobID: String,
        requestID: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads/\(jobID)/heartbeat",
            method: "POST",
            bearerToken: sessionToken,
            body: ["idempotency_key": requestID]
        ) { result in
            switch result {
            case .success: completion(.success(()))
            case .failure(let error): completion(.failure(error))
            }
        }
    }

    func abandonUpload(
        jobID: String,
        requestID: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads/\(jobID)/abandon",
            method: "POST",
            bearerToken: sessionToken,
            body: ["idempotency_key": requestID]
        ) { result in
            switch result {
            case .success: completion(.success(()))
            case .failure(let error): completion(.failure(error))
            }
        }
    }

    func getUploadJob(
        jobID: String,
        sessionToken: String,
        completion: @escaping (Result<PinpointUploadJobStatus, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads/\(jobID)",
            method: "GET",
            bearerToken: sessionToken,
            body: nil
        ) { result in
            switch result {
            case .success(let object):
                guard let data = try? JSONSerialization.data(withJSONObject: object),
                      let status = try? JSONDecoder().decode(PinpointUploadJobStatus.self, from: data) else {
                    completion(.failure(PinpointAPIError.invalidResponse))
                    return
                }
                completion(.success(status))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func resumeUploadSubmission(
        jobID: String,
        sessionToken: String,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/uploads/\(jobID)/resume",
            method: "POST",
            bearerToken: sessionToken,
            body: [:]
        ) { result in
            switch result {
            case .success(let object):
                let data = object["data"] as? [String: Any]
                let identifier = object["transcription_id"] as? String
                    ?? data?["task_id"] as? String
                guard let identifier, !identifier.isEmpty else {
                    completion(.failure(PinpointAPIError.invalidResponse))
                    return
                }
                completion(.success(identifier))
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    func getTranscription(
        identifier: String,
        sessionToken: String,
        completion: @escaping (Result<TranscriptionPollResult, Error>) -> Void
    ) {
        performJSONObject(
            path: "v1/transcriptions/\(identifier)",
            method: "GET",
            bearerToken: sessionToken,
            body: nil
        ) { result in
            switch result {
            case .success(let object):
                let statusValue = object["status"]
                let status = (statusValue as? String)
                    ?? (statusValue as? NSNumber)?.stringValue
                    ?? ((object["data"] as? [String: Any])?["task_status"] as? String)
                    ?? ""
                let upper = status.uppercased()
                if upper == "SUCCESS" {
                    let data = object["data"] as? [String: Any]
                    var text = data?["text"] as? String ?? ""
                    if text.isEmpty, let results = data?["results"] as? [[String: Any]] {
                        text = results.compactMap { $0["text"] as? String }
                            .filter { !$0.isEmpty }
                            .joined(separator: "\n\n")
                    }
                    completion(.success(.completed(text)))
                } else if ["FAILURE", "REVOKED"].contains(upper) {
                    completion(.success(.failed(object["message"] as? String ?? "Transcription failed.")))
                } else {
                    completion(.success(.processing(status.isEmpty ? "PENDING" : status)))
                }
            case .failure(let error):
                completion(.failure(error))
            }
        }
    }

    private func setRecorderBinding(
        action: String,
        serialNumber: String,
        model: PlaudDeviceModel,
        sessionToken: String,
        completion: @escaping (Result<Void, PartnerBindingError>) -> Void
    ) {
        guard model.capabilities.isBindable,
              serialNumber.hasPrefix(model.serialNumberPrefix),
              ["bind", "unbind"].contains(action) else {
            completion(.failure(.unsupportedDevice))
            return
        }
        performJSONObject(
            path: "v1/devices/\(action)",
            method: "POST",
            bearerToken: sessionToken,
            body: [
                "serial_number": serialNumber,
                "device_type": model.partnerBindingType,
            ]
        ) { result in
            switch result {
            case .success:
                completion(.success(()))
            case .failure(PinpointAPIError.sessionExpired):
                completion(.failure(.expiredSession))
            case .failure(PinpointAPIError.network(_)):
                completion(.failure(.unreachable))
            case .failure(PinpointAPIError.server(let status, let code, _)):
                if action == "bind",
                   status == 409,
                   code == "recorder_claimed_elsewhere" {
                    completion(.failure(.boundElsewhere))
                } else {
                    completion(.failure(.server(status, code)))
                }
            case .failure:
                completion(.failure(.server(0, nil)))
            }
        }
    }

    private func perform(
        path: String,
        method: String,
        bearerToken: String? = nil,
        body: [String: Any],
        completion: @escaping (Result<BetaSession, Error>) -> Void
    ) {
        let url = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("PinPoint/1.0", forHTTPHeaderField: "User-Agent")
        if let bearerToken { request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization") }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(PinpointAPIError.network(error.localizedDescription)))
                return
            }
            guard let http = response as? HTTPURLResponse, let data else {
                completion(.failure(PinpointAPIError.invalidResponse))
                return
            }
            guard (200...299).contains(http.statusCode) else {
                let serverError = Self.serverError(from: data)
                if http.statusCode == 401, bearerToken != nil {
                    completion(.failure(PinpointAPIError.sessionExpired))
                } else {
                    completion(.failure(PinpointAPIError.server(
                        http.statusCode,
                        code: serverError.code,
                        message: serverError.message
                    )))
                }
                return
            }
            do {
                let response = try JSONDecoder.pinpoint.decode(BetaSessionResponse.self, from: data)
                completion(.success(BetaSession(
                    sessionToken: response.sessionToken,
                    plaudUserAccessToken: response.plaudUserAccessToken,
                    userID: response.userID,
                    plaudDomain: response.plaudDomain,
                    deploymentMode: response.deploymentMode,
                    sessionExpiresAt: response.sessionExpiresAt,
                    plaudTokenExpiresAt: response.plaudTokenExpiresAt
                )))
            } catch {
                completion(.failure(PinpointAPIError.invalidResponse))
            }
        }.resume()
    }

    private static func serverError(from data: Data) -> (code: String?, message: String?) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (nil, nil)
        }
        if let detail = object["detail"] as? [String: Any] {
            return (detail["code"] as? String, detail["message"] as? String)
        }
        if let detail = object["detail"] as? String { return (nil, detail) }
        if let error = object["error"] as? [String: Any] {
            return (error["code"] as? String, error["message"] as? String)
        }
        return (object["code"] as? String, object["message"] as? String)
    }

    private func performJSONObject(
        path: String,
        method: String,
        bearerToken: String,
        body: [String: Any]?,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        let url = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpMethod = method
        request.timeoutInterval = 60
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        request.setValue("PinPoint/1.0", forHTTPHeaderField: "User-Agent")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(PinpointAPIError.network(error.localizedDescription)))
                return
            }
            guard let http = response as? HTTPURLResponse, let data else {
                completion(.failure(PinpointAPIError.invalidResponse))
                return
            }
            guard (200...299).contains(http.statusCode) else {
                if http.statusCode == 401 {
                    completion(.failure(PinpointAPIError.sessionExpired))
                } else {
                    let serverError = Self.serverError(from: data)
                    completion(.failure(PinpointAPIError.server(
                        http.statusCode,
                        code: serverError.code,
                        message: serverError.message
                    )))
                }
                return
            }
            guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                completion(.failure(PinpointAPIError.invalidResponse))
                return
            }
            completion(.success(object))
        }.resume()
    }
}

struct PinpointUploadPlan: Decodable {
    let jobID: String
    private let suppliedChunkSize: Int?
    let parts: [PinpointUploadPart]
    let state: String?
    let transcriptionID: String?

    var chunkSize: Int {
        guard let suppliedChunkSize, suppliedChunkSize > 0 else { return 5 * 1024 * 1024 }
        return suppliedChunkSize
    }

    enum CodingKeys: String, CodingKey {
        case jobID = "UploadJobId"
        case suppliedChunkSize = "ChunkSize"
        case parts = "Parts"
        case state = "State"
        case transcriptionID = "TranscriptionId"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        jobID = try container.decode(String.self, forKey: .jobID)
        suppliedChunkSize = try container.decodeIfPresent(Int.self, forKey: .suppliedChunkSize)
        parts = try container.decodeIfPresent([PinpointUploadPart].self, forKey: .parts) ?? []
        state = try container.decodeIfPresent(String.self, forKey: .state)
        transcriptionID = try container.decodeIfPresent(String.self, forKey: .transcriptionID)
    }
}

struct PinpointUploadJobStatus: Decodable {
    let state: String
    let transcriptionID: String?

    enum CodingKeys: String, CodingKey {
        case state
        case transcriptionID = "transcription_id"
    }
}

struct PinpointUploadPart: Decodable {
    let partNumber: Int
    let presignedURL: String

    enum CodingKeys: String, CodingKey {
        case partNumber = "PartNumber"
        case presignedURL = "PresignedUrl"
    }
}

struct PinpointCompletedPart {
    let partNumber: Int
    let etag: String
}

enum TranscriptionPollResult: Equatable {
    case processing(String)
    case completed(String)
    case failed(String)
}

enum PinpointAPIError: LocalizedError {
    case network(String)
    case invalidResponse
    case sessionExpired
    case server(Int, code: String?, message: String?)

    var errorDescription: String? {
        switch self {
        case .network:
            return "PinPoint could not reach the service. Check your connection and try again."
        case .invalidResponse:
            return "PinPoint received an unexpected response. Please try again."
        case .sessionExpired:
            return "Your PinPoint session expired. Sign in again to continue."
        case .server(let status, let code, let message):
            switch code {
            case "invitation_required":
                return "Enter your PinPoint invitation code to finish your first sign-in."
            case "invitation_unavailable":
                return "This invitation is invalid, expired, or already used. Ask the sender for a new one."
            case "membership_disabled":
                return "This PinPoint account is not active. Contact the service operator."
            case "local_activation_invalid":
                return "This activation code is invalid, expired, or already used. Create a new code from your local PinPoint service."
            case "local_activation_disabled":
                return "Local activation is disabled on this PinPoint service."
            case "deployment_mode_mismatch":
                return "This PinPoint app and service use different deployment modes."
            case "apple_identity_invalid":
                return "Apple sign-in could not be verified. Please try again."
            default:
                if status == 429 {
                    return "Too many sign-in attempts. Wait a moment, then try again."
                }
                return message ?? "PinPoint could not complete sign-in. Please try again."
            }
        }
    }
}

enum PartnerBindingError: Error, Equatable {
    case unsupportedDevice
    case unreachable
    case expiredSession
    case boundElsewhere
    case server(Int, String?)
}
