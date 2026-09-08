import Foundation
import Security

final class BetaSessionStore {
    private let service: String
    private let account = "public-beta-session"

    init(bundleIdentifier: String = Bundle.main.bundleIdentifier ?? "com.pinpoint.beta") {
        service = bundleIdentifier + ".session"
    }

    func load() throws -> BetaSession? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw SessionStoreError.keychain(status)
        }
        do {
            return try JSONDecoder.pinpoint.decode(BetaSession.self, from: data)
        } catch {
            // A damaged bearer must be removed before the app may offer a new
            // sign-in. Propagate a Keychain deletion failure instead of
            // treating an uncleared credential as a recoverable login error.
            try clear()
            throw SessionStoreError.invalidSession
        }
    }

    func save(_ session: BetaSession) throws {
        let data = try JSONEncoder.pinpoint.encode(session)
        let identity: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecAttrLabel as String: "PinPoint session",
        ]
        let update = SecItemUpdate(identity as CFDictionary, attributes as CFDictionary)
        if update == errSecItemNotFound {
            var add = identity
            attributes.forEach { add[$0.key] = $0.value }
            let status = SecItemAdd(add as CFDictionary, nil)
            guard status == errSecSuccess else { throw SessionStoreError.keychain(status) }
        } else if update != errSecSuccess {
            throw SessionStoreError.keychain(update)
        }
    }

    func clear() throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw SessionStoreError.keychain(status)
        }
    }
}

enum SessionStoreError: LocalizedError {
    case keychain(OSStatus)
    case invalidSession

    var errorDescription: String? {
        switch self {
        case .keychain:
            return "PinPoint could not securely access your saved session."
        case .invalidSession:
            return "Your saved PinPoint session was damaged and has been removed."
        }
    }
}

extension JSONDecoder {
    static var pinpoint: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

extension JSONEncoder {
    static var pinpoint: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}
