#if DEBUG
import UIKit

enum BetaPreviewFactory {
    static func viewController(arguments: [String]) -> UIViewController? {
        guard let argument = arguments.first(where: { $0.hasPrefix("--pinpoint-preview=") }) else {
            return nil
        }
        let mode = argument.replacingOccurrences(of: "--pinpoint-preview=", with: "")
        switch mode {
        case "welcome":
            return BetaWelcomeViewController()
        case "welcome-invited":
            return BetaWelcomeViewController(activationCode: "preview_invitation_code_123456")
        case "welcome-invite-error":
            let welcome = BetaWelcomeViewController()
            welcome.loadViewIfNeeded()
            welcome.showInvitationLinkError("This invitation is invalid, expired, or already used. Ask the sender for a new one.")
            return welcome
        case "welcome-local":
            return BetaWelcomeViewController(deploymentMode: .selfHosted)
        case "welcome-local-ready":
            return BetaWelcomeViewController(
                deploymentMode: .selfHosted,
                activationCode: "preview_local_activation_123456"
            )
        case "welcome-local-error":
            let welcome = BetaWelcomeViewController(deploymentMode: .selfHosted)
            welcome.loadViewIfNeeded()
            welcome.showLocalActivationLinkError("This activation code has expired. Create a new code from your local PinPoint service.")
            return welcome
        case "ownership":
            let setup = BetaDeviceSetupViewController()
            setup.loadViewIfNeeded()
            setup.render(.boundElsewhere(ScannedPlaudDevice(
                serialNumber: "882PREVIEWDEVICE",
                model: .notePinS,
                rssi: -48
            )))
            return setup
        case "permission":
            let setup = BetaDeviceSetupViewController()
            setup.loadViewIfNeeded()
            setup.render(.bluetoothUnavailable)
            return setup
        case "home":
            return BetaHomeViewController(
                device: ConnectedPlaudDevice(
                    serialNumber: "881PREVIEWDEVICE",
                    model: .notePro,
                    batteryLevel: 78,
                    isCharging: false,
                    storageUsed: 214_000_000,
                    storageTotal: 64_000_000_000,
                    supportsWiFiFastTransfer: true
                ),
                recordings: sampleRecordings
            )
        default:
            return nil
        }
    }

    private static var sampleRecordings: [BetaRecording] {
        let now = Date()
        return [
            sample(1, date: now.addingTimeInterval(-600), status: .ready, detail: nil, transcript: "We agreed to ship the first beta to a small Mac-only group, verify Note Pro and NotePin S separately, and collect transfer reliability evidence before expanding."),
            sample(2, date: now.addingTimeInterval(-3_600), status: .transcribing, detail: "Separating speakers", transcript: nil),
            sample(3, date: now.addingTimeInterval(-7_200), status: .uploading, detail: "Uploading 64%", transcript: nil),
            sample(4, date: now.addingTimeInterval(-86_400), status: .failed, detail: "The network connection was interrupted. PinPoint will retry.", transcript: nil),
            sample(5, date: now.addingTimeInterval(-172_800), status: .needsSupport, detail: "Plaud may already be processing this recording. PinPoint will not submit it twice.", transcript: nil),
        ]
    }

    private static func sample(
        _ id: Int,
        date: Date,
        status: BetaRecordingStatus,
        detail: String?,
        transcript: String?
    ) -> BetaRecording {
        BetaRecording(
            sessionID: Int(date.timeIntervalSince1970) + id,
            deviceSerialNumber: "881PREVIEWDEVICE",
            createdAt: date,
            duration: TimeInterval(1_200 + id * 240),
            localFileName: status == .copying ? nil : "preview-\(id).mp3",
            status: status,
            statusDetail: detail,
            transcript: transcript,
            cloudCheckpoint: nil,
            updatedAt: date
        )
    }
}
#endif
