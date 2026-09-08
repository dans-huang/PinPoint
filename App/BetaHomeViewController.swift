import UIKit

final class BetaHomeViewController: UIViewController {
    var onManageRecorder: (() -> Void)?
    var onSignOut: (() -> Void)?
    var onRetryRecording: ((BetaRecording) -> Void)?
    var onRemoveRecorder: (() -> Void)?
    var onOpenRecording: ((BetaRecording) -> Void)?
    var onHandoffRecording: ((BetaRecording) -> Void)?
    var onOpenIntelligenceSettings: (() -> Void)?
    var onManualFastTransfer: (() -> Void)?
    var onResolveFastTransferOffer: ((UUID, Bool) -> Void)?

    private var device: ConnectedPlaudDevice
    private var recordings: [BetaRecording]
    private var expanded = false
    private var summaryReadyIDs = Set<String>()
    private var fastTransferAlert: UIAlertController?
    private var fastTransferOfferID: UUID?
    private var countdownTimer: Timer?
    private var visibleRecordingRowCount = 0

    private let headlineLabel = UILabel()
    private let subtitleLabel = UILabel()
    private let deviceNameLabel = UILabel()
    private let connectionLabel = UILabel()
    private let batteryLabel = UILabel()
    private let storageLabel = UILabel()
    private let automaticTransferButton = UIButton(type: .system)
    private let transcriptStageLabel = UILabel()
    private let momentsStageLabel = UILabel()
    private let recordingsStack = UIStackView()
    private let recordingsScroll = UIScrollView()
    private let expandButton = UIButton(type: .system)
    private let headerStack = UIStackView()
    private let headerActionsStack = UIStackView()
    private let columnsStack = UIStackView()
    private let leftColumn = UIStackView()
    private var leftWidthConstraint: NSLayoutConstraint?
    private var recordingsHeightConstraint: NSLayoutConstraint?

    init(device: ConnectedPlaudDevice, recordings: [BetaRecording]) {
        self.device = device
        self.recordings = recordings
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        configureLayout()
        render(device: device)
        render(recordings: recordings)
    }

    deinit { countdownTimer?.invalidate() }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        updateResponsiveLayout()
    }

    private func configureLayout() {
        let brand = label("PINPOINT  /  PLAUD PARTNER", style: .caption1, weight: .bold, color: .secondaryLabel)
        brand.accessibilityLabel = "PinPoint"
        headlineLabel.font = BetaTheme.title(36)
        headlineLabel.adjustsFontForContentSizeCategory = true
        headlineLabel.numberOfLines = 0
        headlineLabel.text = "Ready for the next conversation."
        headlineLabel.accessibilityTraits = .header
        subtitleLabel.font = .preferredFont(forTextStyle: .subheadline)
        subtitleLabel.textColor = .secondaryLabel
        subtitleLabel.numberOfLines = 0
        subtitleLabel.text = "Connected · automatic transfer is watching quietly"
        let headings = UIStackView(arrangedSubviews: [brand, headlineLabel, subtitleLabel])
        headings.axis = .vertical
        headings.spacing = 5

        let flow = BetaTheme.secondaryButton(title: "Automatic flow", image: "sparkles")
        flow.addTarget(self, action: #selector(flowTapped), for: .touchUpInside)
        let manage = BetaTheme.secondaryButton(title: "Recorder", image: "slider.horizontal.3")
        manage.addTarget(self, action: #selector(manageTapped), for: .touchUpInside)
        let fast = BetaTheme.primaryButton(title: "Fast transfer", image: "wifi")
        fast.addTarget(self, action: #selector(fastTransferTapped), for: .touchUpInside)
        [fast, flow, manage].forEach(headerActionsStack.addArrangedSubview)
        headerActionsStack.axis = .horizontal
        headerActionsStack.spacing = 10
        headerActionsStack.alignment = .center

        headerStack.addArrangedSubview(headings)
        headerStack.addArrangedSubview(headerActionsStack)
        headerStack.axis = .horizontal
        headerStack.alignment = .center
        headerStack.distribution = .equalSpacing
        headerStack.spacing = 30

        [makeDeviceCard(), makeFlowCard(), makeMomentsCard(), makeAccountRow()].forEach(leftColumn.addArrangedSubview)
        leftColumn.axis = .vertical
        leftColumn.spacing = 14
        leftColumn.setContentHuggingPriority(.required, for: .horizontal)
        leftWidthConstraint = leftColumn.widthAnchor.constraint(equalToConstant: 380)
        leftWidthConstraint?.isActive = true

        let recent = makeRecentCard()
        columnsStack.addArrangedSubview(leftColumn)
        columnsStack.addArrangedSubview(recent)
        columnsStack.axis = .horizontal
        columnsStack.alignment = .top
        columnsStack.spacing = 18

        let content = UIStackView(arrangedSubviews: [headerStack, columnsStack])
        content.axis = .vertical
        content.spacing = 22
        content.translatesAutoresizingMaskIntoConstraints = false

        let viewport = UIScrollView()
        viewport.alwaysBounceHorizontal = false
        viewport.alwaysBounceVertical = true
        viewport.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(viewport)
        viewport.addSubview(content)
        NSLayoutConstraint.activate([
            viewport.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            viewport.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            viewport.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            viewport.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: viewport.contentLayoutGuide.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: viewport.contentLayoutGuide.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: viewport.contentLayoutGuide.topAnchor, constant: 22),
            content.bottomAnchor.constraint(equalTo: viewport.contentLayoutGuide.bottomAnchor, constant: -22),
            content.widthAnchor.constraint(equalTo: viewport.frameLayoutGuide.widthAnchor, constant: -48),
            content.heightAnchor.constraint(greaterThanOrEqualTo: viewport.frameLayoutGuide.heightAnchor, constant: -44),
        ])
    }

    private func makeDeviceCard() -> UIView {
        deviceNameLabel.font = .systemFont(ofSize: 25, weight: .bold)
        connectionLabel.font = .preferredFont(forTextStyle: .body)
        connectionLabel.numberOfLines = 0
        batteryLabel.font = .systemFont(ofSize: 42, weight: .bold)
        batteryLabel.textAlignment = .right
        batteryLabel.setContentHuggingPriority(.required, for: .horizontal)
        let identity = UIStackView(arrangedSubviews: [deviceNameLabel, connectionLabel])
        identity.axis = .vertical
        identity.spacing = 5
        let top = UIStackView(arrangedSubviews: [identity, batteryLabel])
        top.axis = .horizontal
        top.alignment = .top
        top.distribution = .equalSpacing
        top.spacing = 14
        let batteryBar = UIProgressView(progressViewStyle: .bar)
        batteryBar.progress = Float(max(0, min(100, device.batteryLevel ?? 0))) / 100
        batteryBar.progressTintColor = .label
        batteryBar.trackTintColor = .tertiarySystemFill
        batteryBar.layer.cornerRadius = 3
        batteryBar.clipsToBounds = true
        batteryBar.heightAnchor.constraint(equalToConstant: 6).isActive = true
        batteryBar.tag = 908
        storageLabel.font = .preferredFont(forTextStyle: .footnote)
        storageLabel.textColor = .secondaryLabel
        storageLabel.numberOfLines = 0
        return card([top, batteryBar, storageLabel], spacing: 15)
    }

    private func makeFlowCard() -> UIView {
        let heading = label("Automatic flow", style: .headline, weight: .bold)
        let info = UIImageView(image: UIImage(systemName: "info.circle"))
        info.tintColor = .secondaryLabel
        let header = UIStackView(arrangedSubviews: [heading, info])
        header.axis = .horizontal
        header.distribution = .equalSpacing
        automaticTransferButton.setTitle("Automatic transfer\nWaiting for the next recording", for: .normal)
        configureStageButton(automaticTransferButton, symbol: "arrow.up.circle")
        automaticTransferButton.addTarget(self, action: #selector(fastTransferTapped), for: .touchUpInside)
        let connection = stage("NotePin connection", detail: "Secure Bluetooth link is live", symbol: "antenna.radiowaves.left.and.right")
        transcriptStageLabel.font = .preferredFont(forTextStyle: .body)
        transcriptStageLabel.numberOfLines = 0
        transcriptStageLabel.text = "Transcript + PinPoint Summary\nLatest ready conversation stays available here"
        let transcriptRow = stageContainer(transcriptStageLabel, symbol: "sparkles")
        return card([header, connection, automaticTransferButton, transcriptRow], spacing: 17)
    }

    private func makeMomentsCard() -> UIView {
        let heading = label("Marked moments", style: .headline, weight: .bold)
        momentsStageLabel.font = .preferredFont(forTextStyle: .subheadline)
        momentsStageLabel.textColor = .secondaryLabel
        momentsStageLabel.numberOfLines = 0
        momentsStageLabel.text = "Button-marked moments are checked safely during the next recording."
        return card([heading, momentsStageLabel], spacing: 8)
    }

    private func makeAccountRow() -> UIView {
        let release = UIButton(type: .system)
        release.setTitle("Release recorder", for: .normal)
        release.addTarget(self, action: #selector(removeTapped), for: .touchUpInside)
        let signOut = UIButton(type: .system)
        signOut.setTitle("Sign out", for: .normal)
        signOut.addTarget(self, action: #selector(signOutTapped), for: .touchUpInside)
        let row = UIStackView(arrangedSubviews: [release, signOut])
        row.axis = .horizontal
        row.distribution = .equalSpacing
        return row
    }

    private func makeRecentCard() -> UIView {
        let title = label("Recent conversations", style: .title2, weight: .bold)
        let hint = label("Open a summary or send any ready conversation to Codex or Claude.", style: .footnote, color: .secondaryLabel)
        let heading = UIStackView(arrangedSubviews: [title, hint])
        heading.axis = .vertical
        heading.spacing = 4

        recordingsStack.axis = .vertical
        recordingsStack.spacing = 1
        recordingsStack.translatesAutoresizingMaskIntoConstraints = false
        recordingsScroll.addSubview(recordingsStack)
        recordingsScroll.showsVerticalScrollIndicator = true
        recordingsScroll.alwaysBounceVertical = false
        recordingsHeightConstraint = recordingsScroll.heightAnchor.constraint(equalToConstant: 110)
        recordingsHeightConstraint?.isActive = true
        NSLayoutConstraint.activate([
            recordingsStack.leadingAnchor.constraint(equalTo: recordingsScroll.contentLayoutGuide.leadingAnchor),
            recordingsStack.trailingAnchor.constraint(equalTo: recordingsScroll.contentLayoutGuide.trailingAnchor),
            recordingsStack.topAnchor.constraint(equalTo: recordingsScroll.contentLayoutGuide.topAnchor),
            recordingsStack.bottomAnchor.constraint(equalTo: recordingsScroll.contentLayoutGuide.bottomAnchor),
            recordingsStack.widthAnchor.constraint(equalTo: recordingsScroll.frameLayoutGuide.widthAnchor),
        ])
        expandButton.setTitle("Show more", for: .normal)
        expandButton.setImage(UIImage(systemName: "chevron.down"), for: .normal)
        expandButton.tintColor = .label
        expandButton.backgroundColor = .tertiarySystemFill
        expandButton.layer.cornerRadius = 11
        expandButton.addTarget(self, action: #selector(expandTapped), for: .touchUpInside)
        expandButton.heightAnchor.constraint(equalToConstant: 40).isActive = true
        return card([heading, recordingsScroll, expandButton], spacing: 12)
    }

    private func updateResponsiveLayout() {
        let availableWidth = max(0, view.bounds.width - 48)
        let stackedColumns = availableWidth < 900
        columnsStack.axis = stackedColumns ? .vertical : .horizontal
        columnsStack.alignment = stackedColumns ? .fill : .top
        leftWidthConstraint?.isActive = !stackedColumns

        headerStack.axis = stackedColumns ? .vertical : .horizontal
        headerStack.alignment = stackedColumns ? .fill : .center
        headerStack.distribution = stackedColumns ? .fill : .equalSpacing
        headerStack.spacing = stackedColumns ? 16 : 30

        let verticalActions = availableWidth < 500
        headerActionsStack.axis = verticalActions ? .vertical : .horizontal
        headerActionsStack.alignment = .fill
        headerActionsStack.distribution = verticalActions ? .fill : .fillEqually
        let estimatedRowHeight: CGFloat = availableWidth < 520 ? 96 : 76
        recordingsHeightConstraint?.constant = visibleRecordingRowCount == 0
            ? 110
            : CGFloat(visibleRecordingRowCount) * estimatedRowHeight
    }

    func render(device: ConnectedPlaudDevice) {
        self.device = device
        guard isViewLoaded else { return }
        deviceNameLabel.text = device.model.displayName
        connectionLabel.text = "Connected — automatic copy is active\nSecure link · \(device.maskedSerialNumber)"
        if let battery = device.batteryLevel {
            batteryLabel.text = "\(battery)%"
            batteryLabel.accessibilityLabel = "Battery \(battery) percent\(device.isCharging ? ", charging" : "")"
            if let progress = view.viewWithTag(908) as? UIProgressView {
                progress.progress = Float(max(0, min(100, battery))) / 100
            }
        } else {
            batteryLabel.text = "—"
        }
        let used = device.storageUsed.map(Self.storage) ?? "—"
        let total = device.storageTotal.map(Self.storage) ?? "—"
        storageLabel.text = "Storage  \(used) of \(total)\n\(device.isCharging ? "Charging" : "Ready")"
    }

    func render(connectionState: BetaDeviceState) {
        guard isViewLoaded else { return }
        switch connectionState {
        case .ready(let device):
            render(device: device)
            subtitleLabel.text = "Connected · automatic transfer is watching quietly"
        case .bluetoothUnavailable:
            subtitleLabel.text = "Bluetooth unavailable · automatic copy is paused"
            connectionLabel.text = "Turn on Bluetooth to reconnect"
        case .accessPaused(let message):
            subtitleLabel.text = "Access check in progress · automatic copy is paused"
            connectionLabel.text = message
        case .failed(let message):
            subtitleLabel.text = "Recorder needs attention"
            connectionLabel.text = message
        default:
            subtitleLabel.text = "Reconnecting · automatic copy resumes when secure"
        }
    }

    func render(recordings: [BetaRecording]) {
        self.recordings = recordings
        guard isViewLoaded else { return }
        if recordings.count <= 8 { expanded = false }
        recordingsStack.arrangedSubviews.forEach {
            recordingsStack.removeArrangedSubview($0)
            $0.removeFromSuperview()
        }
        let visible = expanded ? recordings : Array(recordings.prefix(8))
        if visible.isEmpty {
            recordingsStack.addArrangedSubview(emptyRow())
        } else {
            visible.forEach { recordingsStack.addArrangedSubview(recordingRow($0)) }
        }
        let extra = max(0, recordings.count - 8)
        expandButton.isHidden = extra == 0
        expandButton.setTitle(expanded ? "Show 8 most recent" : "Browse \(extra) more", for: .normal)
        expandButton.setImage(UIImage(systemName: expanded ? "chevron.up" : "chevron.down"), for: .normal)
        recordingsScroll.isScrollEnabled = expanded
        visibleRecordingRowCount = visible.isEmpty ? 0 : min(8, visible.count)
        recordingsHeightConstraint?.constant = visibleRecordingRowCount == 0
            ? 110
            : CGFloat(visibleRecordingRowCount * 76)
        headlineLabel.text = recordings.contains(where: { [.copying, .local, .uploading, .transcribing].contains($0.status) })
            ? "Moving conversations safely."
            : "Ready for the next conversation."
        let latest = recordings.first
        transcriptStageLabel.text = latest?.status == .ready
            ? "Transcript + PinPoint Summary\nLatest conversation is ready to review"
            : "Transcript + PinPoint Summary\nProcessing continues automatically"
    }

    func render(summaryReadyTranscriptionIDs: Set<String>) {
        summaryReadyIDs = summaryReadyTranscriptionIDs
        render(recordings: recordings)
    }

    func renderAutomaticSummaryStatus(_ message: String?) {
        guard isViewLoaded else { return }
        if let message, !message.isEmpty {
            transcriptStageLabel.text = "Transcript + PinPoint Summary\n\(message)"
        } else {
            let latest = recordings.first
            transcriptStageLabel.text = latest?.status == .ready
                ? "Transcript + PinPoint Summary\nLatest conversation is ready to review"
                : "Transcript + PinPoint Summary\nProcessing continues automatically"
        }
    }

    func render(fastTransferState: BetaFastTransferState) {
        guard isViewLoaded else { return }
        switch fastTransferState {
        case .idle:
            automaticTransferButton.setTitle("Automatic transfer\nWaiting for the next recording · tap for Fast Wi-Fi", for: .normal)
        case .checkingForRecordings:
            automaticTransferButton.setTitle("Automatic transfer\nChecking the recorder for unfinished copies…", for: .normal)
        case .requestingLocationPermission(let count):
            automaticTransferButton.setTitle("Fast Wi-Fi transfer\nPreparing access for \(count) recording\(count == 1 ? "" : "s")…", for: .normal)
        case .waitingForDecision(let offer):
            automaticTransferButton.setTitle("Automatic transfer\n\(offer.recordingCount) ready · choose Fast Wi-Fi or BLE", for: .normal)
        case .connecting(let count):
            automaticTransferButton.setTitle("Fast Wi-Fi transfer\nConnecting for \(count) recording\(count == 1 ? "" : "s")…", for: .normal)
        case .transferring(let completed, let total, let speed):
            automaticTransferButton.setTitle("Fast Wi-Fi transfer\n\(completed) of \(total) copied\(speed.map { " · \($0)" } ?? "")", for: .normal)
        case .restoringBluetooth(let completed, let total, let fallback, let message):
            automaticTransferButton.setTitle("Restoring automatic transfer\n\(completed)/\(total) copied · \(fallback) continuing by BLE\(message.map { "\n\($0)" } ?? "")", for: .normal)
        case .unavailable(let message):
            automaticTransferButton.setTitle("Automatic transfer\n\(message)", for: .normal)
        }
    }

    func presentFastTransferOffer(_ offer: BetaFastTransferOffer) {
        dismissFastTransferOffer(id: fastTransferOfferID)
        fastTransferOfferID = offer.id
        let alert = UIAlertController(
            title: offer.recordingCount == 1 ? "New recording ready" : "\(offer.recordingCount) recordings ready",
            message: fastOfferMessage(offer),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Continue with BLE", style: .cancel) { [weak self] _ in
            self?.finishOffer(offer.id, useWiFi: false)
        })
        alert.addAction(UIAlertAction(title: "Use Fast Wi-Fi", style: .default) { [weak self] _ in
            self?.finishOffer(offer.id, useWiFi: true)
        })
        fastTransferAlert = alert
        present(alert, animated: true)
        countdownTimer?.invalidate()
        countdownTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self, weak alert] timer in
            guard let self, self.fastTransferOfferID == offer.id else { timer.invalidate(); return }
            let remaining = max(0, Int(ceil(offer.expiresAt.timeIntervalSinceNow)))
            alert?.message = self.fastOfferMessage(offer, seconds: remaining)
            if remaining == 0 { timer.invalidate() }
        }
    }

    func dismissFastTransferOffer(id: UUID?) {
        guard id == nil || id == fastTransferOfferID else { return }
        countdownTimer?.invalidate()
        countdownTimer = nil
        fastTransferOfferID = nil
        if presentedViewController === fastTransferAlert { fastTransferAlert?.dismiss(animated: true) }
        fastTransferAlert = nil
    }

    func render(markedMomentEvent: BetaMarkedMomentCoordinatorEvent) {
        switch markedMomentEvent {
        case .pending(let count):
            momentsStageLabel.text = "\(count) conversation\(count == 1 ? "" : "s") waiting for a safe mark check."
        case .completed(let record):
            momentsStageLabel.text = record.status == .ready
                ? "\(record.tags.count) button-marked moment\(record.tags.count == 1 ? "" : "s") captured and ready for AI."
                : "Marked-moment check complete."
        case .pausedUntilNextRecording(let count):
            momentsStageLabel.text = "\(count) conversation\(count == 1 ? "" : "s") will be checked during your next recording."
        case .storageWarning(let message):
            momentsStageLabel.text = message
        }
    }

    private func recordingRow(_ recording: BetaRecording) -> UIView {
        let title = label(recording.title, style: .body, weight: .semibold)
        title.numberOfLines = 2
        let detail = label(detailText(recording), style: .footnote, color: .secondaryLabel)
        detail.numberOfLines = 2
        let labels = UIStackView(arrangedSubviews: [title, detail])
        labels.axis = .vertical
        labels.spacing = 3

        let status = UILabel()
        status.text = statusText(recording)
        status.font = .systemFont(ofSize: 12, weight: .semibold)
        status.textColor = recording.status == .ready ? .systemGreen : .secondaryLabel
        status.setContentHuggingPriority(.required, for: .horizontal)

        let assistant = UIButton(type: .system)
        assistant.setImage(UIImage(systemName: "arrow.up.right.square"), for: .normal)
        assistant.tintColor = .label
        assistant.backgroundColor = .tertiarySystemFill
        assistant.layer.cornerRadius = 12
        assistant.accessibilityLabel = "Continue this conversation with an assistant"
        assistant.isEnabled = BetaAgentHandoffViewController.isAvailable(for: recording)
        assistant.alpha = assistant.isEnabled ? 1 : 0.35
        assistant.addAction(UIAction { [weak self] _ in self?.onHandoffRecording?(recording) }, for: .touchUpInside)
        NSLayoutConstraint.activate([
            assistant.widthAnchor.constraint(equalToConstant: 44),
            assistant.heightAnchor.constraint(equalToConstant: 44),
        ])

        let row = UIStackView(arrangedSubviews: [labels, status, assistant])
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 14
        let control = UIControl()
        control.backgroundColor = .secondarySystemBackground
        control.addSubview(row)
        row.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            row.leadingAnchor.constraint(equalTo: control.leadingAnchor, constant: 16),
            row.trailingAnchor.constraint(equalTo: control.trailingAnchor, constant: -14),
            row.topAnchor.constraint(equalTo: control.topAnchor, constant: 11),
            row.bottomAnchor.constraint(equalTo: control.bottomAnchor, constant: -11),
        ])
        if recording.status == .ready {
            control.addAction(UIAction { [weak self] _ in self?.onOpenRecording?(recording) }, for: .touchUpInside)
            control.accessibilityHint = "Opens the summary"
        } else if recording.status == .failed {
            control.addAction(UIAction { [weak self] _ in self?.onRetryRecording?(recording) }, for: .touchUpInside)
            control.accessibilityHint = "Retries this conversation"
        }
        control.isAccessibilityElement = true
        control.accessibilityLabel = "\(recording.title), \(detailText(recording)), \(statusText(recording))"
        control.accessibilityTraits = recording.status == .ready || recording.status == .failed
            ? .button
            : .staticText
        return control
    }

    private func emptyRow() -> UIView {
        let message = label("No conversations yet\nStop a recording and PinPoint will copy it automatically.", style: .body, color: .secondaryLabel)
        message.textAlignment = .center
        message.backgroundColor = .secondarySystemBackground
        message.heightAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        return message
    }

    private func detailText(_ recording: BetaRecording) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        let duration = max(1, Int(recording.duration / 60))
        var text = "\(formatter.string(from: recording.createdAt)) · \(duration) min"
        if recording.status == .ready, let id = recording.transcriptionID, summaryReadyIDs.contains(id) {
            text += " · Summary ready"
        } else if let detail = recording.statusDetail, !detail.isEmpty {
            text += "\n\(detail)"
        }
        return text
    }

    private func statusText(_ recording: BetaRecording) -> String {
        switch recording.status {
        case .copying: return "COPYING"
        case .local, .uploading: return "UPLOADING"
        case .transcribing: return "TRANSCRIBING"
        case .ready: return "READY"
        case .failed: return "RETRY"
        case .needsSupport: return "SUPPORT"
        }
    }

    private func stage(_ title: String, detail: String, symbol: String) -> UIView {
        let text = UILabel()
        text.text = "\(title)\n\(detail)"
        text.font = .preferredFont(forTextStyle: .body)
        text.numberOfLines = 0
        return stageContainer(text, symbol: symbol)
    }

    private func stageContainer(_ text: UIView, symbol: String) -> UIView {
        let icon = UIImageView(image: UIImage(systemName: symbol))
        icon.tintColor = .secondaryLabel
        icon.widthAnchor.constraint(equalToConstant: 24).isActive = true
        let check = UIImageView(image: UIImage(systemName: "checkmark.circle.fill"))
        check.tintColor = .systemGreen
        check.widthAnchor.constraint(equalToConstant: 22).isActive = true
        let row = UIStackView(arrangedSubviews: [icon, text, check])
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 12
        return row
    }

    private func configureStageButton(_ button: UIButton, symbol: String) {
        button.setImage(UIImage(systemName: symbol), for: .normal)
        button.tintColor = .label
        button.setTitleColor(.label, for: .normal)
        button.contentHorizontalAlignment = .leading
        button.titleLabel?.font = .preferredFont(forTextStyle: .body)
        button.titleLabel?.numberOfLines = 0
        button.backgroundColor = .clear
    }

    private func card(_ views: [UIView], spacing: CGFloat = 10) -> UIView {
        let stack = UIStackView(arrangedSubviews: views)
        stack.axis = .vertical
        stack.spacing = spacing
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.backgroundColor = .secondarySystemBackground
        stack.layer.cornerRadius = BetaTheme.cornerRadius
        stack.layer.cornerCurve = .continuous
        stack.clipsToBounds = true
        return stack
    }

    private func label(_ text: String, style: UIFont.TextStyle, weight: UIFont.Weight? = nil, color: UIColor = .label) -> UILabel {
        let value = UILabel()
        value.text = text
        let size = UIFont.preferredFont(forTextStyle: style).pointSize
        value.font = weight.map { UIFontMetrics(forTextStyle: style).scaledFont(for: .systemFont(ofSize: size, weight: $0)) }
            ?? .preferredFont(forTextStyle: style)
        value.textColor = color
        value.numberOfLines = 0
        value.adjustsFontForContentSizeCategory = true
        return value
    }

    private func fastOfferMessage(_ offer: BetaFastTransferOffer, seconds: Int? = nil) -> String {
        let remaining = seconds ?? max(0, Int(ceil(offer.expiresAt.timeIntervalSinceNow)))
        let minutes = max(1, Int(ceil(offer.totalDuration / 60)))
        return "Copy this \(minutes)-minute batch over the recorder's fast channel? PinPoint returns to Bluetooth afterward.\n\nNo response continues safely with BLE in \(remaining)s."
    }

    private func finishOffer(_ id: UUID, useWiFi: Bool) {
        dismissFastTransferOffer(id: id)
        onResolveFastTransferOffer?(id, useWiFi)
    }

    func showRecorderRemovalError(_ message: String) {
        let alert = UIAlertController(title: "Recorder was not released", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }

    @objc private func fastTransferTapped() { onManualFastTransfer?() }
    @objc private func flowTapped() { onOpenIntelligenceSettings?() }
    @objc private func manageTapped() { onManageRecorder?() }
    @objc private func signOutTapped() { onSignOut?() }
    @objc private func expandTapped() { expanded.toggle(); render(recordings: recordings) }
    @objc private func removeTapped() {
        let alert = UIAlertController(
            title: "Release this recorder?",
            message: "This lets it join another Plaud or PinPoint account. Existing local and cloud copies stay available, and files on the recorder are not erased.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Release recorder", style: .destructive) { [weak self] _ in self?.onRemoveRecorder?() })
        present(alert, animated: true)
    }

    private static func storage(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}
