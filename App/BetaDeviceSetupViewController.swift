import UIKit

final class BetaDeviceSetupViewController: UIViewController {
    var onConnect: ((ScannedPlaudDevice) -> Void)?
    var onScan: (() -> Void)?
    var onRetryOwnership: (() -> Void)?
    var onUseAnotherRecorder: (() -> Void)?
    var onContinue: ((ConnectedPlaudDevice) -> Void)?
    var onRemoveRecorder: (() -> Void)?
    var onDeleteLocalData: (() -> Void)?
    var onSignOut: (() -> Void)?
    var showsReleaseAction = false
    var showsLocalDataAction = false

    private let scrollView = UIScrollView()
    private let contentStack = UIStackView()
    private var currentState: BetaDeviceState = .idle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        buildShell()
        render(currentState)
    }

    func render(_ state: BetaDeviceState) {
        currentState = state
        guard isViewLoaded else { return }
        contentStack.arrangedSubviews.forEach {
            contentStack.removeArrangedSubview($0)
            $0.removeFromSuperview()
        }

        addHeader()
        let focusTarget: UIView?
        switch state {
        case .idle:
            focusTarget = addIntro()
        case .scanning:
            focusTarget = addProgress(title: "Looking for Plaud recorders…", detail: "Keep your recorder powered on and close to this Mac.")
        case .discovered(let devices):
            focusTarget = addDeviceResults(devices)
        case .noDevicesFound:
            focusTarget = addMessage(
                symbol: "wave.3.right.slash",
                title: "No supported recorder found",
                detail: "Make sure your NotePin S or Note Pro is powered on, nearby, and not connected to another device."
            )
            addButton("Scan again", primary: true, action: #selector(scanTapped))
        case .connecting(let device):
            focusTarget = addProgress(title: "Connecting \(device.model.displayName)…", detail: "Securing the Bluetooth connection.")
        case .checkingOwnership(let device):
            focusTarget = addProgress(title: "Checking recorder ownership…", detail: "Preparing \(device.model.displayName) for automatic sync.")
        case .ready(let device):
            focusTarget = addReady(device)
        case .bluetoothUnavailable:
            focusTarget = addMessage(
                symbol: "bluetooth.slash",
                title: "Bluetooth is required",
                detail: "Turn on Bluetooth and allow PinPoint to find your recorder."
            )
            addButton("Open System Settings", primary: true, action: #selector(settingsTapped))
            addButton("Try again", primary: false, action: #selector(scanTapped))
        case .boundElsewhere(let device):
            focusTarget = addBoundElsewhere(device)
        case .releaseWaiting(let model):
            focusTarget = addMessage(
                symbol: "checkmark.icloud.fill",
                title: "Released from Plaud Cloud",
                detail: "Bring \(model.displayName) near this Mac to finish clearing its local pairing. PinPoint will keep looking and will not bind it again. If the recorder is permanently unavailable, contact the service operator for recovery."
            )
            addButton("Look again", primary: true, action: #selector(scanTapped))
        case .accessPaused(let message):
            focusTarget = addMessage(
                symbol: "lock.shield.fill",
                title: "Automatic sync is paused",
                detail: message
            )
        case .sessionExpired:
            focusTarget = addMessage(
                symbol: "person.crop.circle.badge.exclamationmark",
                title: "Sign in again",
                detail: "Your PinPoint session expired before recorder setup finished."
            )
            addButton("Return to sign in", primary: true, action: #selector(signOutTapped))
        case .failed(let message):
            focusTarget = addMessage(symbol: "exclamationmark.triangle", title: "Setup paused", detail: message)
            addButton("Try again", primary: true, action: #selector(scanTapped))
        }
        addDataControls()
        UIAccessibility.post(notification: .layoutChanged, argument: focusTarget)
    }

    private func buildShell() {
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.alwaysBounceVertical = true
        view.addSubview(scrollView)

        contentStack.axis = .vertical
        contentStack.alignment = .fill
        contentStack.spacing = 16
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        scrollView.addSubview(contentStack)

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            contentStack.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor, constant: 36),
            contentStack.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor, constant: -36),
            contentStack.centerXAnchor.constraint(equalTo: scrollView.frameLayoutGuide.centerXAnchor),
            contentStack.leadingAnchor.constraint(greaterThanOrEqualTo: scrollView.frameLayoutGuide.leadingAnchor, constant: 24),
            contentStack.trailingAnchor.constraint(lessThanOrEqualTo: scrollView.frameLayoutGuide.trailingAnchor, constant: -24),
            contentStack.widthAnchor.constraint(lessThanOrEqualToConstant: BetaTheme.contentWidth),
        ])
    }

    private func addHeader() {
        let row = UIStackView()
        row.axis = .horizontal
        row.alignment = .center
        let brand = UILabel()
        brand.text = "PINPOINT  /  RECORDER SETUP"
        brand.font = .systemFont(ofSize: 12, weight: .semibold)
        brand.textColor = .secondaryLabel
        let signOut = UIButton(type: .system)
        signOut.setTitle("Sign out", for: .normal)
        signOut.addTarget(self, action: #selector(signOutTapped), for: .touchUpInside)
        row.addArrangedSubview(brand)
        row.addArrangedSubview(UIView())
        row.addArrangedSubview(signOut)
        contentStack.addArrangedSubview(row)
        contentStack.setCustomSpacing(26, after: row)

        let title = UILabel()
        title.text = "Connect a recorder"
        title.font = BetaTheme.title()
        title.numberOfLines = 0
        title.adjustsFontForContentSizeCategory = true
        title.accessibilityTraits.insert(.header)
        contentStack.addArrangedSubview(title)

        let subtitle = UILabel()
        subtitle.text = "Keep your recorder powered on and near this Mac."
        subtitle.font = .preferredFont(forTextStyle: .body)
        subtitle.textColor = .secondaryLabel
        subtitle.numberOfLines = 0
        subtitle.adjustsFontForContentSizeCategory = true
        contentStack.addArrangedSubview(subtitle)
        contentStack.setCustomSpacing(26, after: subtitle)
    }

    @discardableResult
    private func addIntro() -> UIView {
        let card = makeCard()
        let symbol = UIImageView(image: UIImage(systemName: "dot.radiowaves.left.and.right"))
        symbol.preferredSymbolConfiguration = .init(pointSize: 32, weight: .medium)
        symbol.tintColor = .label
        symbol.heightAnchor.constraint(equalToConstant: 44).isActive = true
        card.addArrangedSubview(symbol)

        let title = makeLabel("Ready to find NotePin S and Note Pro", style: .title3, weight: .semibold)
        title.accessibilityTraits.insert(.header)
        card.addArrangedSubview(title)
        card.addArrangedSubview(makeLabel(
            "Bluetooth is used to connect and copy recordings. PinPoint does not inspect nearby unsupported devices.",
            style: .body,
            color: .secondaryLabel
        ))
        contentStack.addArrangedSubview(card)
        addButton("Find my recorder", primary: true, action: #selector(scanTapped))

        let note = makeLabel(
            "Already using Plaud Personal? Sync recordings still on the recorder before moving it. Existing Personal recordings and your subscription stay unchanged.",
            style: .footnote,
            color: .secondaryLabel
        )
        contentStack.addArrangedSubview(note)
        return title
    }

    @discardableResult
    private func addProgress(title: String, detail: String) -> UIView {
        let card = makeCard()
        let spinner = UIActivityIndicatorView(style: .large)
        spinner.startAnimating()
        spinner.accessibilityLabel = title
        card.addArrangedSubview(spinner)
        let heading = makeLabel(title, style: .title3, weight: .semibold, alignment: .center)
        heading.accessibilityTraits.insert(.header)
        card.addArrangedSubview(heading)
        card.addArrangedSubview(makeLabel(detail, style: .body, color: .secondaryLabel, alignment: .center))
        contentStack.addArrangedSubview(card)
        return heading
    }

    @discardableResult
    private func addDeviceResults(_ devices: [ScannedPlaudDevice]) -> UIView {
        let heading = makeLabel("Nearby", style: .headline, weight: .semibold)
        heading.accessibilityTraits.insert(.header)
        contentStack.addArrangedSubview(heading)
        devices.forEach { device in
            let card = BetaTheme.card()
            let row = UIStackView()
            row.axis = .horizontal
            row.alignment = .center
            row.spacing = 14
            row.translatesAutoresizingMaskIntoConstraints = false
            card.addSubview(row)
            NSLayoutConstraint.activate([
                row.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18),
                row.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),
                row.topAnchor.constraint(equalTo: card.topAnchor, constant: 16),
                row.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -16),
            ])

            let icon = UIImageView(image: UIImage(systemName: "waveform.circle.fill"))
            icon.preferredSymbolConfiguration = .init(pointSize: 28)
            icon.tintColor = .label
            let labels = UIStackView()
            labels.axis = .vertical
            labels.spacing = 3
            labels.addArrangedSubview(makeLabel(device.model.displayName, style: .headline, weight: .semibold))
            labels.addArrangedSubview(makeLabel("\(device.maskedSerialNumber)  ·  Nearby", style: .subheadline, color: .secondaryLabel))
            let button = BetaTheme.secondaryButton(title: "Connect")
            button.addAction(UIAction { [weak self] _ in self?.onConnect?(device) }, for: .touchUpInside)
            button.accessibilityLabel = "Connect \(device.model.displayName), serial ending \(device.serialNumber.suffix(4))"
            row.addArrangedSubview(icon)
            row.addArrangedSubview(labels)
            row.addArrangedSubview(UIView())
            row.addArrangedSubview(button)
            contentStack.addArrangedSubview(card)
        }
        addButton("Scan again", primary: false, action: #selector(scanTapped))
        return heading
    }

    @discardableResult
    private func addReady(_ device: ConnectedPlaudDevice) -> UIView {
        let heading = addMessage(
            symbol: "checkmark.circle.fill",
            title: "PinPoint is ready",
            detail: "\(device.model.displayName) will reconnect automatically whenever PinPoint is running and this Mac is awake."
        )
        let card = makeCard()
        card.addArrangedSubview(statusRow("Automatic sync", value: "On", symbol: "arrow.triangle.2.circlepath"))
        card.addArrangedSubview(statusRow("Recorder", value: device.maskedSerialNumber, symbol: "waveform"))
        if let battery = device.batteryLevel {
            card.addArrangedSubview(statusRow("Battery", value: "\(battery)%\(device.isCharging ? " · Charging" : "")", symbol: "battery.75"))
        }
        card.addArrangedSubview(statusRow("Plaud Personal", value: "Separate", symbol: "person.2.slash"))
        contentStack.addArrangedSubview(card)
        let button = BetaTheme.primaryButton(title: "Open PinPoint", image: "arrow.right")
        button.addAction(UIAction { [weak self] _ in self?.onContinue?(device) }, for: .touchUpInside)
        contentStack.addArrangedSubview(button)
        return heading
    }

    @discardableResult
    private func addBoundElsewhere(_ device: ScannedPlaudDevice) -> UIView {
        let heading = addMessage(
            symbol: "lock.trianglebadge.exclamationmark",
            title: "This recorder is connected elsewhere",
            detail: "For privacy, Plaud does not reveal which account owns it. PinPoint cannot connect until the current owner releases it."
        )
        let card = makeCard()
        card.addArrangedSubview(makeLabel("Move \(device.model.displayName) to PinPoint", style: .headline, weight: .semibold))
        card.addArrangedSubview(makeLabel(
            "1. Open Plaud Personal on your phone.\n2. Sync recordings still stored on the recorder.\n3. Go to Device and remove this recorder.\n4. Return here and try again.",
            style: .body,
            color: .secondaryLabel
        ))
        card.addArrangedSubview(makeLabel(
            "Your existing Plaud Personal recordings and subscription stay unchanged. New recordings will sync to PinPoint instead.",
            style: .footnote,
            color: .secondaryLabel
        ))
        contentStack.addArrangedSubview(card)
        addButton("I’ve removed it — Try again", primary: true, action: #selector(retryOwnershipTapped))
        addButton("Use another recorder", primary: false, action: #selector(useAnotherTapped))
        return heading
    }

    @discardableResult
    private func addMessage(symbol: String, title: String, detail: String) -> UIView {
        let card = makeCard()
        let icon = UIImageView(image: UIImage(systemName: symbol))
        icon.preferredSymbolConfiguration = .init(pointSize: 36, weight: .medium)
        icon.tintColor = symbol.contains("checkmark") ? .systemGreen : .label
        icon.heightAnchor.constraint(equalToConstant: 48).isActive = true
        icon.accessibilityElementsHidden = true
        card.addArrangedSubview(icon)
        let heading = makeLabel(title, style: .title2, weight: .bold, alignment: .center)
        heading.accessibilityTraits.insert(.header)
        card.addArrangedSubview(heading)
        card.addArrangedSubview(makeLabel(detail, style: .body, color: .secondaryLabel, alignment: .center))
        contentStack.addArrangedSubview(card)
        return heading
    }

    private func addButton(_ title: String, primary: Bool, action: Selector) {
        let button = primary ? BetaTheme.primaryButton(title: title) : BetaTheme.secondaryButton(title: title)
        button.addTarget(self, action: action, for: .touchUpInside)
        contentStack.addArrangedSubview(button)
    }

    private func addDataControls() {
        guard showsReleaseAction || showsLocalDataAction else { return }
        let divider = UIView()
        divider.backgroundColor = .separator
        divider.heightAnchor.constraint(equalToConstant: 1 / UIScreen.main.scale).isActive = true
        contentStack.setCustomSpacing(28, after: contentStack.arrangedSubviews.last!)
        contentStack.addArrangedSubview(divider)
        if showsReleaseAction {
            let release = BetaTheme.secondaryButton(
                title: "Release saved recorder from PinPoint",
                image: "rectangle.portrait.and.arrow.right"
            )
            release.addTarget(self, action: #selector(removeRecorderTapped), for: .touchUpInside)
            contentStack.addArrangedSubview(release)
        }
        if showsLocalDataAction {
            let deleteLocal = UIButton(type: .system)
            deleteLocal.setTitle("Delete local audio and transcripts", for: .normal)
            deleteLocal.setTitleColor(.systemRed, for: .normal)
            deleteLocal.addTarget(self, action: #selector(deleteLocalTapped), for: .touchUpInside)
            deleteLocal.accessibilityHint = "Available after the recorder has been released"
            contentStack.addArrangedSubview(deleteLocal)
        }
    }

    private func makeCard() -> UIStackView {
        let stack = UIStackView()
        stack.axis = .vertical
        stack.spacing = 14
        stack.alignment = .fill
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.backgroundColor = .secondarySystemBackground
        stack.layer.cornerRadius = BetaTheme.cornerRadius
        stack.layer.cornerCurve = .continuous
        return stack
    }

    private func makeLabel(
        _ text: String,
        style: UIFont.TextStyle,
        weight: UIFont.Weight? = nil,
        color: UIColor = .label,
        alignment: NSTextAlignment = .natural
    ) -> UILabel {
        let label = UILabel()
        label.text = text
        label.font = weight.map {
            UIFontMetrics(forTextStyle: style).scaledFont(
                for: .systemFont(ofSize: UIFont.preferredFont(forTextStyle: style).pointSize, weight: $0)
            )
        }
            ?? .preferredFont(forTextStyle: style)
        label.textColor = color
        label.textAlignment = alignment
        label.numberOfLines = 0
        label.adjustsFontForContentSizeCategory = true
        return label
    }

    private func statusRow(_ title: String, value: String, symbol: String) -> UIView {
        let row = UIStackView()
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 12
        let icon = UIImageView(image: UIImage(systemName: symbol))
        icon.tintColor = .secondaryLabel
        icon.widthAnchor.constraint(equalToConstant: 24).isActive = true
        let name = makeLabel(title, style: .body)
        let valueLabel = makeLabel(value, style: .body, weight: .semibold, color: .secondaryLabel, alignment: .right)
        row.addArrangedSubview(icon)
        row.addArrangedSubview(name)
        row.addArrangedSubview(UIView())
        row.addArrangedSubview(valueLabel)
        row.accessibilityLabel = "\(title), \(value)"
        return row
    }

    @objc private func scanTapped() { onScan?() }
    @objc private func retryOwnershipTapped() { onRetryOwnership?() }
    @objc private func useAnotherTapped() { onUseAnotherRecorder?() }
    @objc private func removeRecorderTapped() {
        let alert = UIAlertController(
            title: "Release the saved recorder?",
            message: "Plaud Cloud can release it even if the recorder is offline. If it is nearby, PinPoint also removes its local pairing. Recordings are not erased.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Release recorder", style: .destructive) { [weak self] _ in
            self?.onRemoveRecorder?()
        })
        present(alert, animated: true)
    }
    @objc private func deleteLocalTapped() { onDeleteLocalData?() }
    @objc private func signOutTapped() { onSignOut?() }
    @objc private func settingsTapped() { openPinpointSettings() }

    func showRecorderRemovalError(_ message: String) {
        let alert = UIAlertController(title: "Recorder was not released", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }
}
