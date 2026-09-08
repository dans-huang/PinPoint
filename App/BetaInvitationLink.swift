import Foundation

enum BetaInvitationLink {
    static let scheme = "pinpoint"
    static let host = "invite"
    private static let inviteBaseURLInfoKey = "PinpointInviteBaseURL"

    static func code(from url: URL) -> String? {
        code(from: url, allowedHTTPSBaseURL: configuredHTTPSBaseURL)
    }

    static func code(from url: URL, allowedHTTPSBaseURL: URL?) -> String? {
        if url.scheme?.lowercased() == scheme {
            guard url.host?.lowercased() == host,
                  url.user == nil,
                  url.password == nil,
                  url.port == nil,
                  url.path.isEmpty || url.path == "/" else {
                return nil
            }
            return queryCode(from: url)
        }

        guard let baseURL = allowedHTTPSBaseURL,
              let urlComponents = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
              ),
              let baseComponents = URLComponents(
                url: baseURL,
                resolvingAgainstBaseURL: false
              ),
              baseURL.scheme?.lowercased() == "https",
              url.scheme?.lowercased() == "https",
              url.host?.lowercased() == baseURL.host?.lowercased(),
              url.port == baseURL.port,
              url.user == nil,
              url.password == nil,
              urlComponents.percentEncodedPath == baseComponents.percentEncodedPath else {
            return nil
        }
        return queryCode(from: url)
    }

    private static func queryCode(from url: URL) -> String? {
        guard url.fragment == nil,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        let codeItems = (components.queryItems ?? []).filter { $0.name == "code" }
        guard codeItems.count == 1,
              (components.queryItems ?? []).count == 1,
              let code = codeItems[0].value?.trimmingCharacters(in: .whitespacesAndNewlines),
              isValid(code: code) else {
            return nil
        }
        return code
    }

    private static var configuredHTTPSBaseURL: URL? {
        guard let rawValue = Bundle.main.object(
            forInfoDictionaryKey: inviteBaseURLInfoKey
        ) as? String else {
            return nil
        }
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty,
              !value.contains("$("),
              let url = URL(string: value),
              let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
              ),
              url.scheme?.lowercased() == "https",
              url.host != nil,
              url.port == nil || url.port == 443,
              url.user == nil,
              url.password == nil,
              components.percentEncodedPath == "/invite",
              url.query == nil,
              url.fragment == nil else {
            return nil
        }
        return url
    }

    static func isValid(code: String) -> Bool {
        guard (16...128).contains(code.count) else { return false }
        return code.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil
    }
}
