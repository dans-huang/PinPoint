import Foundation

/// Account-neutral intelligence types used by the public PinPoint app. These
/// intentionally contain no Personal Plaud identifiers, credentials, or local
/// project paths.
struct BetaIntelligenceSettings: Equatable {
    var automaticSummaryEnabled: Bool
    var defaultTemplateID: String
}

struct BetaSummaryTemplate: Equatable, Hashable {
    let id: String
    let name: String
    let prompt: String
    let isBuiltIn: Bool
    let isArchived: Bool
    let version: Int
}

struct BetaVocabularyTerm: Equatable, Hashable {
    let id: String
    let term: String
}

struct BetaSummarySnapshot: Equatable {
    let transcriptionID: String
    let text: String
    let version: Int
    let contentHash: String
    let templateID: String?
    let updatedAt: Date?
}

enum BetaSummaryJobKind: String {
    case generate
    case improve
}

enum BetaSummaryJobStatus: String {
    case pending
    case running
    case generating
    case proposed
    case completed
    case approved
    case discarded
    case failed
    case uncertain
    case unknown
}

struct BetaSummaryJob: Equatable {
    let id: String
    let transcriptionID: String
    let kind: BetaSummaryJobKind
    let status: BetaSummaryJobStatus
    let proposedSummary: String?
    let proposedVocabulary: [String]
    let baseSummaryVersion: Int?
    let baseSummaryHash: String?
    let errorMessage: String?
}

protocol BetaIntelligenceProviding: AnyObject {
    func getSettings(
        sessionToken: String,
        completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void
    )

    func updateSettings(
        _ settings: BetaIntelligenceSettings,
        sessionToken: String,
        completion: @escaping (Result<BetaIntelligenceSettings, Error>) -> Void
    )

    func listTemplates(
        sessionToken: String,
        completion: @escaping (Result<[BetaSummaryTemplate], Error>) -> Void
    )

    func createTemplate(
        name: String,
        prompt: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void
    )

    func updateTemplate(
        id: String,
        name: String,
        prompt: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryTemplate, Error>) -> Void
    )

    func archiveTemplate(
        id: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    )

    func listVocabulary(
        sessionToken: String,
        completion: @escaping (Result<[BetaVocabularyTerm], Error>) -> Void
    )

    func addVocabulary(
        term: String,
        sessionToken: String,
        completion: @escaping (Result<BetaVocabularyTerm, Error>) -> Void
    )

    func removeVocabulary(
        id: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    )

    func getSummary(
        transcriptionID: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummarySnapshot?, Error>) -> Void
    )

    func createSummaryJob(
        transcriptionID: String,
        kind: BetaSummaryJobKind,
        templateID: String?,
        markedMoments: [Int],
        background: Bool,
        sessionToken: String,
        idempotencyKey: String,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    )

    func getSummaryJob(
        id: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryJob, Error>) -> Void
    )

    func findSummaryJob(
        idempotencyKey: String,
        sessionToken: String,
        completion: @escaping (Result<BetaSummaryJob?, Error>) -> Void
    )

    func approveSummaryJob(
        id: String,
        expectedVersion: Int?,
        expectedHash: String?,
        approvedVocabulary: [String],
        sessionToken: String,
        completion: @escaping (Result<BetaSummarySnapshot, Error>) -> Void
    )

    func discardSummaryJob(
        id: String,
        sessionToken: String,
        completion: @escaping (Result<Void, Error>) -> Void
    )
}

enum BetaIntelligenceError: LocalizedError {
    case unavailable
    case invalidResponse
    case notReady
    case conflict(String)
    case server(String)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "PinPoint Intelligence is not configured yet. Your transcript is still safe and available."
        case .invalidResponse:
            return "PinPoint received an unexpected intelligence response. Try again in a moment."
        case .notReady:
            return "This conversation is not ready for intelligence actions yet."
        case .conflict(let message), .server(let message):
            return message
        }
    }
}
