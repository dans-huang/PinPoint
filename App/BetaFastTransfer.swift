import Foundation

/// One explicit choice covers every newly discovered recording in the batch.
/// The offer is intentionally free of recorder/session identifiers so the UI
/// cannot accidentally persist or expose another account's device metadata.
struct BetaFastTransferOffer: Equatable {
    let id: UUID
    let recordingCount: Int
    let totalDuration: TimeInterval
    let expiresAt: Date
}

/// A UI-safe snapshot of the Beta-only fast-transfer state machine.
enum BetaFastTransferState: Equatable {
    case idle
    case checkingForRecordings
    case requestingLocationPermission(recordingCount: Int)
    case waitingForDecision(BetaFastTransferOffer)
    case connecting(recordingCount: Int)
    case transferring(completed: Int, total: Int, speed: String?)
    case restoringBluetooth(completed: Int, total: Int, fallbackCount: Int, message: String?)
    case unavailable(String)
}
