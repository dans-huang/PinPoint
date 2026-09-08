import Foundation

enum PinpointLocalActivationLink {
    static let scheme = "pinpoint"
    static let host = "local"

    static func code(from url: URL) -> String? {
        guard url.scheme?.lowercased() == scheme,
              url.host?.lowercased() == host,
              url.user == nil,
              url.password == nil,
              url.port == nil,
              url.path.isEmpty,
              url.fragment == nil,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }

        let queryItems = components.queryItems ?? []
        guard queryItems.count == 1,
              queryItems[0].name == "code",
              let code = queryItems[0].value,
              isValid(code: code) else {
            return nil
        }
        return code
    }

    static func isValid(code: String) -> Bool {
        guard (16...128).contains(code.count),
              code == code.trimmingCharacters(in: .whitespacesAndNewlines) else {
            return false
        }
        return code.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil
    }
}
