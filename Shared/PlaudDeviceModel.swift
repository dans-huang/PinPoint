import Foundation

/// What a recognized Plaud device model is allowed to do in this codebase.
/// Unknown serials never get a capability set, so they fail closed.
struct PlaudDeviceCapabilities: Equatable {
    let isBindable: Bool
    let supportsBluetoothTransfer: Bool
    /// Whether this Partner SDK family supports the model's device-hotspot
    /// transfer protocol. The connected device's live `supportWiFi` flag is
    /// still the final runtime gate.
    let supportsWiFiFastTransfer: Bool
}

/// Single source of truth for Plaud device identity. Both the Personal
/// bridge and PinPoint resolve serial numbers through this type
/// instead of scattering prefix checks.
enum PlaudDeviceModel: CaseIterable, Equatable {
    case notePinS
    case notePro

    /// Unknown serial prefixes resolve to nil and are never bindable.
    init?(serialNumber: String) {
        guard let match = Self.allCases.first(
            where: { serialNumber.hasPrefix($0.serialNumberPrefix) }
        ) else { return nil }
        self = match
    }

    var serialNumberPrefix: String {
        switch self {
        case .notePinS: return "882"
        case .notePro: return "881"
        }
    }

    /// The `type` value the official Plaud Partner bind/unbind API expects.
    var partnerBindingType: String {
        switch self {
        case .notePinS: return "notepins"
        case .notePro: return "notepro"
        }
    }

    var displayName: String {
        switch self {
        case .notePinS: return "Plaud NotePin S"
        case .notePro: return "Plaud Note Pro"
        }
    }

    var capabilities: PlaudDeviceCapabilities {
        switch self {
        case .notePinS:
            return PlaudDeviceCapabilities(
                isBindable: true,
                supportsBluetoothTransfer: true,
                supportsWiFiFastTransfer: true
            )
        case .notePro:
            return PlaudDeviceCapabilities(
                isBindable: true,
                supportsBluetoothTransfer: true,
                supportsWiFiFastTransfer: true
            )
        }
    }

    static func isBindable(serialNumber: String) -> Bool {
        PlaudDeviceModel(serialNumber: serialNumber)?.capabilities.isBindable ?? false
    }
}
