import Foundation

struct BetaSession: Codable, Equatable {
    let sessionToken: String
    let plaudUserAccessToken: String
    let userID: String
    let plaudDomain: String
    let deploymentMode: DeploymentMode
    let sessionExpiresAt: Date
    let plaudTokenExpiresAt: Date

    var canResumeWithoutRefresh: Bool {
        min(sessionExpiresAt, plaudTokenExpiresAt).timeIntervalSinceNow > 300
    }

    private enum CodingKeys: String, CodingKey {
        case sessionToken
        case plaudUserAccessToken
        case userID = "userId"
        case plaudDomain
        case deploymentMode
        case sessionExpiresAt
        case plaudTokenExpiresAt
    }
}

struct BetaSessionResponse: Decodable {
    let sessionToken: String
    let plaudUserAccessToken: String
    let userID: String
    let plaudDomain: String
    let deploymentMode: DeploymentMode
    let sessionExpiresAt: Date
    let plaudTokenExpiresAt: Date

    private enum CodingKeys: String, CodingKey {
        case sessionToken
        case plaudUserAccessToken
        case userID = "userId"
        case plaudDomain
        case deploymentMode
        case sessionExpiresAt
        case plaudTokenExpiresAt
    }
}
