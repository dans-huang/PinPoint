import AuthenticationServices
import CryptoKit
import UIKit

struct AppleSignInPayload {
    let identityToken: String
    let authorizationCode: String?
    let nonce: String
    let inviteCode: String?
}

final class BetaWelcomeViewController: UIViewController {
    var onRequestNonce: ((@escaping (Result<String, Error>) -> Void) -> Void)?
    var onSignIn: ((AppleSignInPayload, @escaping (Result<Void, Error>) -> Void) -> Void)?
    var onCreateLocalSession: ((String, @escaping (Result<Void, Error>) -> Void) -> Void)?

    private let deploymentMode: DeploymentMode
    private let blockingConfigurationError: String?
    private let initialError: String?
    private let scrollView = UIScrollView()
    private let contentWrapper = UIView()
    private let contentStack = UIStackView()
    private let appleButtonContainer = UIView()
    private let errorLabel = UILabel()
    private let progress = UIActivityIndicatorView(style: .medium)
    private let detailLabel = UILabel()
    private let detailButton = UIButton(type: .system)
    private let inviteField = UITextField()
    private let inviteStatusLabel = UILabel()
    private let inviteActionButton = UIButton(type: .system)
    private var currentNonce: String?

    init(
        deploymentMode: DeploymentMode = .hosted,
        blockingConfigurationError: String? = nil,
        initialError: String? = nil,
        activationCode: String? = nil
    ) {
        self.deploymentMode = deploymentMode
        self.blockingConfigurationError = blockingConfigurationError
        self.initialError = initialError
        super.init(nibName: nil, bundle: nil)
        if let activationCode,
           (deploymentMode == .hosted
               ? BetaInvitationLink.isValid(code: activationCode)
               : PinpointLocalActivationLink.isValid(code: activationCode)) {
            inviteField.text = activationCode
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        buildInterface()
        updateInvitationPresentation()
        rebuildPrimaryButton()
        if let message = blockingConfigurationError ?? initialError { showError(message) }
    }

    func applyInvitationCode(_ code: String) {
        guard BetaInvitationLink.isValid(code: code) else {
            showInvitationLinkError(
                "This invitation link is not valid. Ask the sender for a new invitation or enter the code manually."
            )
            return
        }
        inviteField.text = code
        errorLabel.isHidden = true
        updateInvitationPresentation()
        UIAccessibility.post(notification: .announcement, argument: "PinPoint invitation ready")
    }

    func showInvitationLinkError(_ message: String) {
        inviteField.text = nil
        revealManualInvitation(clearExisting: true)
        showError(message)
    }

    func applyLocalActivationCode(_ code: String) {
        guard deploymentMode == .selfHosted,
              PinpointLocalActivationLink.isValid(code: code) else {
            showLocalActivationLinkError(
                "This local activation link is not valid. Create a new activation code from your PinPoint service."
            )
            return
        }
        inviteField.text = code
        errorLabel.isHidden = true
        updateInvitationPresentation()
        UIAccessibility.post(notification: .announcement, argument: "PinPoint activation ready")
    }

    func showLocalActivationLinkError(_ message: String) {
        inviteField.text = nil
        revealManualInvitation(clearExisting: true)
        showError(message)
    }

    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        if deploymentMode == .hosted,
           previousTraitCollection?.userInterfaceStyle != traitCollection.userInterfaceStyle {
            rebuildPrimaryButton()
        }
    }

    private func buildInterface() {
        let glow = UIView()
        glow.translatesAutoresizingMaskIntoConstraints = false
        glow.backgroundColor = UIColor.systemPurple.withAlphaComponent(0.16)
        glow.layer.cornerRadius = 160
        glow.isUserInteractionEnabled = false
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.alwaysBounceVertical = true
        view.addSubview(scrollView)

        contentWrapper.translatesAutoresizingMaskIntoConstraints = false
        scrollView.addSubview(contentWrapper)
        contentWrapper.addSubview(glow)

        let mark = UIImageView(image: UIImage(systemName: "waveform.badge.mic"))
        mark.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 42, weight: .medium)
        mark.tintColor = .label
        mark.contentMode = .scaleAspectFit
        mark.heightAnchor.constraint(equalToConstant: 58).isActive = true

        let eyebrow = UILabel()
        eyebrow.text = "PINPOINT"
        eyebrow.font = .systemFont(ofSize: 13, weight: .semibold)
        eyebrow.textColor = .secondaryLabel
        eyebrow.textAlignment = .center

        let title = UILabel()
        title.text = "Your Plaud recorder,\nworking quietly from your Mac."
        title.font = BetaTheme.title(38)
        title.textAlignment = .center
        title.numberOfLines = 0
        title.adjustsFontForContentSizeCategory = true

        let subtitle = UILabel()
        subtitle.text = deploymentMode == .selfHosted
            ? "Connect to the private PinPoint service running on this Mac."
            : "PinPoint connects your recorder and prepares each new conversation for review."
        subtitle.font = .preferredFont(forTextStyle: .title3)
        subtitle.textColor = .secondaryLabel
        subtitle.textAlignment = .center
        subtitle.numberOfLines = 0
        subtitle.adjustsFontForContentSizeCategory = true

        appleButtonContainer.heightAnchor.constraint(equalToConstant: 52).isActive = true

        inviteField.placeholder = deploymentMode == .selfHosted
            ? "Paste local activation code"
            : "Paste invitation code"
        inviteField.borderStyle = .roundedRect
        inviteField.font = .preferredFont(forTextStyle: .body)
        inviteField.adjustsFontForContentSizeCategory = true
        inviteField.autocorrectionType = .no
        inviteField.autocapitalizationType = .none
        inviteField.textContentType = .oneTimeCode
        inviteField.clearButtonMode = .whileEditing
        inviteField.returnKeyType = .continue
        inviteField.delegate = self
        inviteField.accessibilityLabel = deploymentMode == .selfHosted
            ? "Local activation code"
            : "Invitation code"
        inviteField.accessibilityHint = deploymentMode == .selfHosted
            ? "Paste the complete code created by the PinPoint service on this Mac"
            : "Paste the complete code from your PinPoint invitation"

        inviteStatusLabel.text = deploymentMode == .selfHosted
            ? "✓  Activation ready"
            : "✓  Invitation ready"
        inviteStatusLabel.font = .preferredFont(forTextStyle: .headline)
        inviteStatusLabel.textColor = .systemGreen
        inviteStatusLabel.textAlignment = .center
        inviteStatusLabel.adjustsFontForContentSizeCategory = true
        inviteStatusLabel.accessibilityLabel = deploymentMode == .selfHosted
            ? "PinPoint activation ready"
            : "PinPoint invitation ready"

        inviteActionButton.titleLabel?.font = .preferredFont(forTextStyle: .subheadline)
        inviteActionButton.addTarget(self, action: #selector(showManualInvitation), for: .touchUpInside)

        progress.hidesWhenStopped = true
        progress.accessibilityLabel = deploymentMode == .selfHosted ? "Connecting" : "Signing in"

        errorLabel.font = .preferredFont(forTextStyle: .subheadline)
        errorLabel.textColor = .systemRed
        errorLabel.textAlignment = .center
        errorLabel.numberOfLines = 0
        errorLabel.isHidden = true
        errorLabel.adjustsFontForContentSizeCategory = true

        let support = UILabel()
        support.text = deploymentMode == .selfHosted
            ? "The service and app stay on this Mac. Your activation code is used once and is never saved.\n\nApple silicon Mac  ·  Plaud NotePin S  ·  Plaud Note Pro"
            : "Returning user? Continue with Apple. New here? Use the invitation you received.\n\nMac only  ·  Plaud NotePin S  ·  Plaud Note Pro"
        support.font = .preferredFont(forTextStyle: .footnote)
        support.textColor = .tertiaryLabel
        support.textAlignment = .center
        support.numberOfLines = 0

        detailButton.setTitle("How PinPoint and Plaud Personal stay separate", for: .normal)
        detailButton.titleLabel?.font = .preferredFont(forTextStyle: .footnote)
        detailButton.titleLabel?.numberOfLines = 0
        detailButton.addTarget(self, action: #selector(toggleDetails), for: .touchUpInside)
        detailButton.accessibilityHint = "Shows account and recorder ownership details"

        detailLabel.text = "PinPoint uses Plaud’s Partner platform. It does not import or change your Plaud Personal recordings or subscription. A recorder can have one owner at a time, so moving a recorder to PinPoint stops that recorder from syncing to the Plaud Personal app."
        detailLabel.font = .preferredFont(forTextStyle: .footnote)
        detailLabel.textColor = .secondaryLabel
        detailLabel.textAlignment = .left
        detailLabel.numberOfLines = 0
        detailLabel.isHidden = true
        detailLabel.adjustsFontForContentSizeCategory = true

        contentStack.axis = .vertical
        contentStack.alignment = .fill
        contentStack.spacing = 18
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        [mark, eyebrow, title, subtitle, inviteStatusLabel, inviteField, inviteActionButton, appleButtonContainer, progress, errorLabel, support, detailButton, detailLabel]
            .forEach(contentStack.addArrangedSubview)
        contentStack.setCustomSpacing(8, after: mark)
        contentStack.setCustomSpacing(28, after: subtitle)
        contentStack.setCustomSpacing(10, after: appleButtonContainer)
        contentStack.setCustomSpacing(28, after: support)
        contentWrapper.addSubview(contentStack)

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            contentWrapper.leadingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.leadingAnchor),
            contentWrapper.trailingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.trailingAnchor),
            contentWrapper.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor),
            contentWrapper.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor),
            contentWrapper.widthAnchor.constraint(equalTo: scrollView.frameLayoutGuide.widthAnchor),
            contentWrapper.heightAnchor.constraint(greaterThanOrEqualTo: scrollView.frameLayoutGuide.heightAnchor),
            glow.widthAnchor.constraint(equalToConstant: 320),
            glow.heightAnchor.constraint(equalToConstant: 320),
            glow.centerXAnchor.constraint(equalTo: contentWrapper.centerXAnchor),
            glow.centerYAnchor.constraint(equalTo: contentWrapper.centerYAnchor, constant: -120),
            contentStack.centerYAnchor.constraint(equalTo: contentWrapper.centerYAnchor),
            contentStack.centerXAnchor.constraint(equalTo: contentWrapper.centerXAnchor),
            contentStack.leadingAnchor.constraint(greaterThanOrEqualTo: contentWrapper.leadingAnchor, constant: 24),
            contentStack.trailingAnchor.constraint(lessThanOrEqualTo: contentWrapper.trailingAnchor, constant: -24),
            contentStack.topAnchor.constraint(greaterThanOrEqualTo: contentWrapper.topAnchor, constant: 36),
            contentStack.bottomAnchor.constraint(lessThanOrEqualTo: contentWrapper.bottomAnchor, constant: -36),
            contentStack.widthAnchor.constraint(lessThanOrEqualToConstant: 620),
        ])
    }

    private func rebuildPrimaryButton() {
        appleButtonContainer.subviews.forEach { $0.removeFromSuperview() }
        let button: UIControl
        switch deploymentMode {
        case .hosted:
            let style: ASAuthorizationAppleIDButton.Style = traitCollection.userInterfaceStyle == .dark ? .white : .black
            let appleButton = ASAuthorizationAppleIDButton(type: .continue, style: style)
            appleButton.cornerRadius = 12
            appleButton.addTarget(self, action: #selector(beginAppleSignIn), for: .touchUpInside)
            appleButton.accessibilityHint = "Creates or restores your PinPoint account"
            button = appleButton
        case .selfHosted:
            let localButton = BetaTheme.primaryButton(title: "Connect this Mac", image: "lock.open.fill")
            localButton.addTarget(self, action: #selector(beginLocalActivation), for: .touchUpInside)
            localButton.accessibilityHint = "Uses this activation code to create a private local PinPoint session"
            button = localButton
        }
        button.translatesAutoresizingMaskIntoConstraints = false
        button.isEnabled = blockingConfigurationError == nil && !progress.isAnimating
        appleButtonContainer.addSubview(button)
        NSLayoutConstraint.activate([
            button.leadingAnchor.constraint(equalTo: appleButtonContainer.leadingAnchor),
            button.trailingAnchor.constraint(equalTo: appleButtonContainer.trailingAnchor),
            button.topAnchor.constraint(equalTo: appleButtonContainer.topAnchor),
            button.bottomAnchor.constraint(equalTo: appleButtonContainer.bottomAnchor),
        ])
    }

    @objc private func toggleDetails() {
        detailLabel.isHidden.toggle()
        detailButton.accessibilityValue = detailLabel.isHidden ? "Collapsed" : "Expanded"
        UIAccessibility.post(notification: .layoutChanged, argument: detailLabel.isHidden ? detailButton : detailLabel)
    }

    @objc private func beginAppleSignIn() {
        guard deploymentMode == .hosted else { return }
        guard blockingConfigurationError == nil else { return }
        let invitationCode = inviteField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard invitationCode.isEmpty || BetaInvitationLink.isValid(code: invitationCode) else {
            revealManualInvitation(clearExisting: false)
            showError("Paste the complete invitation code before continuing.")
            return
        }
        errorLabel.isHidden = true
        setBusy(true)
        onRequestNonce? { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let nonce):
                    self.currentNonce = nonce
                    let request = ASAuthorizationAppleIDProvider().createRequest()
                    request.nonce = Self.sha256(nonce)
                    let controller = ASAuthorizationController(authorizationRequests: [request])
                    controller.delegate = self
                    controller.presentationContextProvider = self
                    controller.performRequests()
                case .failure(let error):
                    self.setBusy(false)
                    self.showError(error.localizedDescription)
                }
            }
        }
    }

    @objc private func beginLocalActivation() {
        guard deploymentMode == .selfHosted,
              blockingConfigurationError == nil else { return }
        let activationCode = inviteField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard PinpointLocalActivationLink.isValid(code: activationCode) else {
            revealManualInvitation(clearExisting: false)
            showError("Paste the complete local activation code before connecting.")
            return
        }
        errorLabel.isHidden = true
        setBusy(true)
        onCreateLocalSession?(activationCode) { [weak self] result in
            DispatchQueue.main.async {
                self?.setBusy(false)
                if case .failure(let error) = result {
                    self?.revealManualInvitation(clearExisting: false)
                    self?.showError(error.localizedDescription)
                }
            }
        }
    }

    private func setBusy(_ busy: Bool) {
        busy ? progress.startAnimating() : progress.stopAnimating()
        rebuildPrimaryButton()
        detailButton.isEnabled = !busy
        inviteField.isEnabled = !busy
        inviteActionButton.isEnabled = !busy
    }

    @objc private func showManualInvitation() {
        revealManualInvitation(clearExisting: inviteStatusLabel.isHidden == false)
    }

    private func revealManualInvitation(clearExisting: Bool) {
        if clearExisting { inviteField.text = nil }
        inviteStatusLabel.isHidden = true
        inviteField.isHidden = false
        inviteActionButton.isHidden = true
        inviteField.becomeFirstResponder()
        UIAccessibility.post(notification: .layoutChanged, argument: inviteField)
    }

    private func updateInvitationPresentation() {
        let code = inviteField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let hasInvitation = deploymentMode == .hosted
            ? BetaInvitationLink.isValid(code: code)
            : PinpointLocalActivationLink.isValid(code: code)
        inviteStatusLabel.isHidden = !hasInvitation
        inviteField.isHidden = hasInvitation
        inviteActionButton.isHidden = !hasInvitation && deploymentMode == .selfHosted
        inviteActionButton.setTitle(
            hasInvitation
                ? (deploymentMode == .selfHosted ? "Use a different activation code" : "Use a different invitation")
                : "Enter invitation code",
            for: .normal
        )
        inviteActionButton.accessibilityHint = hasInvitation
            ? (deploymentMode == .selfHosted
                ? "Replaces the local activation code"
                : "Replaces the invitation attached to this sign-in")
            : "For first-time PinPoint users"
    }

    private func handleSignInFailure(_ error: Error) {
        if case PinpointAPIError.server(let status, let code, _) = error,
           status == 403,
           code == "invitation_required" || code == "invitation_unavailable" {
            revealManualInvitation(clearExisting: code == "invitation_unavailable")
        }
        showError(error.localizedDescription)
    }

    private func showError(_ message: String) {
        errorLabel.text = message
        errorLabel.isHidden = false
        UIAccessibility.post(notification: .announcement, argument: message)
    }

    private static func sha256(_ input: String) -> String {
        SHA256.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

extension BetaWelcomeViewController: ASAuthorizationControllerDelegate {
    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization
    ) {
        guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
              let tokenData = credential.identityToken,
              let identityToken = String(data: tokenData, encoding: .utf8),
              let nonce = currentNonce else {
            setBusy(false)
            showError("Apple did not return a complete sign-in. Please try again.")
            return
        }
        let payload = AppleSignInPayload(
            identityToken: identityToken,
            authorizationCode: credential.authorizationCode
                .flatMap { String(data: $0, encoding: .utf8) }
                .flatMap { $0.isEmpty ? nil : $0 },
            nonce: nonce,
            inviteCode: inviteField.text?
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .nilIfEmpty
        )
        onSignIn?(payload) { [weak self] result in
            DispatchQueue.main.async {
                self?.setBusy(false)
                if case .failure(let error) = result {
                    self?.handleSignInFailure(error)
                }
            }
        }
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
        setBusy(false)
        if (error as? ASAuthorizationError)?.code == .canceled { return }
        showError("Apple sign-in did not finish. Please try again.")
    }
}

extension BetaWelcomeViewController: UITextFieldDelegate {
    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        textField.resignFirstResponder()
        if deploymentMode == .selfHosted {
            beginLocalActivation()
        } else {
            beginAppleSignIn()
        }
        return true
    }
}

extension BetaWelcomeViewController: ASAuthorizationControllerPresentationContextProviding {
    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        view.window ?? UIWindow()
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
