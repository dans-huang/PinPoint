import Foundation

enum DeploymentMode: String, Codable, CaseIterable {
    case hosted
    case selfHosted = "self_hosted"
}

struct BetaConfiguration {
    let apiBaseURL: URL
    let plaudDomain: String
    let deploymentMode: DeploymentMode

    static func load(bundle: Bundle = .main) -> Result<BetaConfiguration, ConfigurationError> {
        let rawMode = (bundle.object(forInfoDictionaryKey: "PinpointDeploymentMode") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let deploymentMode = DeploymentMode(rawValue: rawMode) else {
            return .failure(.invalidDeploymentMode)
        }

        let rawURL = (bundle.object(forInfoDictionaryKey: "PinpointAPIBaseURL") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let url: URL
        do {
            url = try validatedAPIBaseURL(rawValue: rawURL, deploymentMode: deploymentMode)
        } catch let error as ConfigurationError {
            return .failure(error)
        } catch {
            return .failure(.missingAPIBaseURL)
        }

        let domain = (bundle.object(forInfoDictionaryKey: "PlaudServerDomain") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let normalizedDomain = domain.lowercased()
        guard domain == normalizedDomain,
              !domain.isEmpty,
              !domain.contains("://"),
              !domain.contains("/"),
              domain.hasSuffix(".plaud.ai"),
              let serviceURL = URL(string: "https://\(domain)"),
              serviceURL.host == domain else {
            return .failure(.invalidPlaudDomain)
        }
        return .success(BetaConfiguration(
            apiBaseURL: url,
            plaudDomain: domain,
            deploymentMode: deploymentMode
        ))
    }

    static func validatedAPIBaseURL(
        rawValue: String,
        deploymentMode: DeploymentMode
    ) throws -> URL {
        guard !rawValue.isEmpty,
              !rawValue.contains("$("),
              let components = URLComponents(string: rawValue),
              let url = components.url else {
            throw ConfigurationError.missingAPIBaseURL
        }

        switch deploymentMode {
        case .hosted:
            guard components.scheme?.lowercased() == "https",
                  components.host != nil,
                  components.user == nil,
                  components.password == nil,
                  components.query == nil,
                  components.fragment == nil else {
                throw ConfigurationError.invalidHostedAPIBaseURL
            }
        case .selfHosted:
            guard components.scheme?.lowercased() == "http",
                  components.host == "127.0.0.1",
                  let port = components.port,
                  (1...65_535).contains(port),
                  components.user == nil,
                  components.password == nil,
                  components.path.isEmpty,
                  components.query == nil,
                  components.fragment == nil,
                  rawValue.range(of: #"^http://127\.0\.0\.1:[0-9]+$"#, options: .regularExpression) != nil else {
                throw ConfigurationError.invalidSelfHostedAPIBaseURL
            }
        }
        return url
    }
}

enum ConfigurationError: LocalizedError {
    case missingAPIBaseURL
    case invalidDeploymentMode
    case invalidHostedAPIBaseURL
    case invalidSelfHostedAPIBaseURL
    case invalidPlaudDomain

    var errorDescription: String? {
        switch self {
        case .missingAPIBaseURL:
            return "PinPoint is not connected to its service yet."
        case .invalidDeploymentMode:
            return "PinPoint has an invalid deployment mode."
        case .invalidHostedAPIBaseURL:
            return "PinPoint hosted mode requires a secure HTTPS service URL."
        case .invalidSelfHostedAPIBaseURL:
            return "Self-hosted PinPoint must connect directly to http://127.0.0.1 with an explicit port."
        case .invalidPlaudDomain:
            return "PinPoint has an invalid Plaud service configuration."
        }
    }
}
