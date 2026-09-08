import UIKit
import UniformTypeIdentifiers

/// A self-contained handoff sheet for Partner recordings. It never assumes the
/// destination assistant can retrieve a private PinPoint recording by ID: the
/// bounded meeting context travels in the user-approved deep link or clipboard.
final class BetaAgentHandoffViewController: UIViewController {
    private let recording: BetaRecording
    private let context: BetaAgentRecordingContext
    private let projectStore: BetaProjectSelectionStore

    private let instructionField = UITextField()
    private let projectNameLabel = UILabel()
    private let projectPathLabel = UILabel()
    private let chooseProjectButton = UIButton(type: .system)
    private let codexButton = UIButton(type: .system)
    private let claudeButton = UIButton(type: .system)
    private let copyButton = UIButton(type: .system)
    private let statusLabel = UILabel()
    private let assistantButtons = UIStackView()

    private var selectedProjectPath: String?
    private var pendingDestination: BetaAssistantDestination?
    private var launchInFlight = false

    static func isAvailable(for recording: BetaRecording) -> Bool {
        recording.status == .ready
    }

    init(
        recording: BetaRecording,
        userID: String,
        approvedSummary: String? = nil,
        markedMoments: [Int] = [],
        defaults: UserDefaults = .standard
    ) {
        self.recording = recording
        context = BetaAgentRecordingContext(
            reference: Self.reference(for: recording),
            title: recording.title,
            createdAt: recording.createdAt,
            duration: recording.duration,
            transcript: recording.transcript ?? "",
            approvedSummary: approvedSummary,
            markedMoments: markedMoments
        )
        projectStore = BetaProjectSelectionStore(
            userNamespace: BetaRecordingStore.userDirectoryName(for: userID),
            defaults: defaults
        )
        super.init(nibName: nil, bundle: nil)
        selectedProjectPath = projectStore.lastProject
        modalPresentationStyle = .formSheet
        preferredContentSize = CGSize(width: 760, height: 660)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        configureLayout()
        refreshProjectUI()
        refreshActionState()
    }

    override var canBecomeFirstResponder: Bool { true }

    override var keyCommands: [UIKeyCommand]? {
        [
            UIKeyCommand(input: "\u{1B}", modifierFlags: [], action: #selector(closeTapped)),
            UIKeyCommand(input: "[", modifierFlags: .command, action: #selector(closeTapped)),
        ]
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        becomeFirstResponder()
    }

    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let shouldStack = view.bounds.width < 560
            || traitCollection.preferredContentSizeCategory.isAccessibilityCategory
        assistantButtons.axis = shouldStack ? .vertical : .horizontal
    }

    private func configureLayout() {
        let scrollView = UIScrollView()
        scrollView.alwaysBounceVertical = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)

        let content = UIStackView()
        content.axis = .vertical
        content.spacing = 18
        content.translatesAutoresizingMaskIntoConstraints = false
        scrollView.addSubview(content)

        content.addArrangedSubview(makeHeader())
        content.addArrangedSubview(makeRecordingCard())
        content.addArrangedSubview(makeInstructionCard())
        content.addArrangedSubview(makeProjectCard())
        content.addArrangedSubview(makeActionsCard())
        content.addArrangedSubview(statusLabel)

        statusLabel.font = .preferredFont(forTextStyle: .footnote)
        statusLabel.adjustsFontForContentSizeCategory = true
        statusLabel.textColor = .secondaryLabel
        statusLabel.numberOfLines = 0
        statusLabel.textAlignment = .center
        statusLabel.isHidden = true
        statusLabel.accessibilityTraits = .updatesFrequently

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor, constant: 22),
            content.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor, constant: -24),
            content.widthAnchor.constraint(equalTo: scrollView.frameLayoutGuide.widthAnchor, constant: -48),
        ])
    }

    private func makeHeader() -> UIView {
        let title = UILabel()
        title.text = "Continue with your assistant"
        title.font = BetaTheme.title(28)
        title.adjustsFontForContentSizeCategory = true
        title.numberOfLines = 0
        title.accessibilityTraits = .header

        let close = UIButton(type: .system)
        close.setImage(UIImage(systemName: "xmark"), for: .normal)
        close.tintColor = .label
        close.backgroundColor = .tertiarySystemFill
        close.layer.cornerRadius = 18
        close.accessibilityLabel = "Close"
        close.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)
        NSLayoutConstraint.activate([
            close.widthAnchor.constraint(equalToConstant: 40),
            close.heightAnchor.constraint(equalToConstant: 40),
        ])

        let header = UIStackView(arrangedSubviews: [title, close])
        header.axis = .horizontal
        header.alignment = .top
        header.spacing = 18
        return header
    }

    private func makeRecordingCard() -> UIView {
        let title = label(recording.title, style: .title3, weight: .semibold)
        title.numberOfLines = 0

        let metadata = label(Self.metadata(for: recording), style: .subheadline, color: .secondaryLabel)
        metadata.numberOfLines = 0

        let transcriptState = label(
            (recording.transcript ?? "").isEmpty
                ? "The recording is ready, but Plaud returned no readable transcript text. The assistant brief will contain the recording details and any approved PinPoint summary."
                : "The assistant receives the latest approved summary, your marked moments, a bounded meeting excerpt, and your instruction. Nothing is sent until you open an assistant or copy the brief.",
            style: .footnote,
            color: .secondaryLabel
        )
        transcriptState.numberOfLines = 0

        return card(containing: [title, metadata, transcriptState], spacing: 8)
    }

    private func makeInstructionCard() -> UIView {
        let heading = label("What should the assistant do?", style: .headline, weight: .semibold)
        let hint = label(
            "Optional. Leave this blank to review decisions and suggest next actions.",
            style: .footnote,
            color: .secondaryLabel
        )
        hint.numberOfLines = 0

        instructionField.placeholder = "For example: turn the agreed actions into an implementation plan"
        instructionField.font = .preferredFont(forTextStyle: .body)
        instructionField.adjustsFontForContentSizeCategory = true
        instructionField.backgroundColor = .tertiarySystemBackground
        instructionField.layer.cornerRadius = 12
        instructionField.layer.cornerCurve = .continuous
        instructionField.clearButtonMode = .whileEditing
        instructionField.returnKeyType = .done
        instructionField.delegate = self
        instructionField.accessibilityLabel = "Optional instruction for the assistant"
        instructionField.leftView = UIView(frame: CGRect(x: 0, y: 0, width: 14, height: 1))
        instructionField.leftViewMode = .always
        instructionField.rightView = UIView(frame: CGRect(x: 0, y: 0, width: 14, height: 1))
        instructionField.rightViewMode = .always
        instructionField.heightAnchor.constraint(greaterThanOrEqualToConstant: 50).isActive = true

        return card(containing: [heading, hint, instructionField], spacing: 10)
    }

    private func makeProjectCard() -> UIView {
        let folder = UIImageView(image: UIImage(systemName: "folder.fill"))
        folder.tintColor = .label
        folder.contentMode = .scaleAspectFit
        folder.setContentHuggingPriority(.required, for: .horizontal)
        NSLayoutConstraint.activate([
            folder.widthAnchor.constraint(equalToConstant: 24),
            folder.heightAnchor.constraint(equalToConstant: 24),
        ])

        projectNameLabel.font = .preferredFont(forTextStyle: .headline)
        projectNameLabel.adjustsFontForContentSizeCategory = true
        projectNameLabel.numberOfLines = 0
        projectPathLabel.font = .preferredFont(forTextStyle: .footnote)
        projectPathLabel.adjustsFontForContentSizeCategory = true
        projectPathLabel.textColor = .secondaryLabel
        projectPathLabel.numberOfLines = 0

        let labels = UIStackView(arrangedSubviews: [projectNameLabel, projectPathLabel])
        labels.axis = .vertical
        labels.spacing = 3

        chooseProjectButton.setTitle("Choose…", for: .normal)
        chooseProjectButton.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        chooseProjectButton.showsMenuAsPrimaryAction = true
        chooseProjectButton.setContentHuggingPriority(.required, for: .horizontal)
        chooseProjectButton.accessibilityLabel = "Choose assistant project folder"

        let row = UIStackView(arrangedSubviews: [folder, labels, chooseProjectButton])
        row.axis = .horizontal
        row.alignment = .center
        row.spacing = 12
        return card(containing: [row], spacing: 0)
    }

    private func makeActionsCard() -> UIView {
        configureActionButton(codexButton, title: "Open in Codex", symbol: "arrow.up.right.square")
        configureActionButton(claudeButton, title: "Open in Claude", symbol: "arrow.up.right.square")
        codexButton.addTarget(self, action: #selector(openCodex), for: .touchUpInside)
        claudeButton.addTarget(self, action: #selector(openClaude), for: .touchUpInside)

        copyButton.setTitle("Copy brief", for: .normal)
        copyButton.setImage(UIImage(systemName: "doc.on.doc"), for: .normal)
        copyButton.tintColor = .label
        copyButton.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        copyButton.backgroundColor = .tertiarySystemFill
        copyButton.layer.cornerRadius = 12
        copyButton.layer.cornerCurve = .continuous
        copyButton.addTarget(self, action: #selector(copyBrief), for: .touchUpInside)
        copyButton.heightAnchor.constraint(greaterThanOrEqualToConstant: 48).isActive = true

        assistantButtons.addArrangedSubview(codexButton)
        assistantButtons.addArrangedSubview(claudeButton)
        assistantButtons.axis = .horizontal
        assistantButtons.distribution = .fillEqually
        assistantButtons.spacing = 12

        let note = label(
            "If a Desktop app cannot open, copy the same brief and paste it into a new task.",
            style: .footnote,
            color: .secondaryLabel
        )
        note.numberOfLines = 0

        return card(containing: [assistantButtons, copyButton, note], spacing: 12)
    }

    private func configureActionButton(_ button: UIButton, title: String, symbol: String) {
        button.setTitle(title, for: .normal)
        button.setImage(UIImage(systemName: symbol), for: .normal)
        button.setTitleColor(.systemBackground, for: .normal)
        button.tintColor = .systemBackground
        button.backgroundColor = .label
        button.layer.cornerRadius = 12
        button.layer.cornerCurve = .continuous
        button.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        button.titleLabel?.adjustsFontForContentSizeCategory = true
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 52).isActive = true
    }

    private func card(containing views: [UIView], spacing: CGFloat) -> UIView {
        let card = BetaTheme.card()
        let stack = UIStackView(arrangedSubviews: views)
        stack.axis = .vertical
        stack.spacing = spacing
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -16),
        ])
        return card
    }

    private func label(
        _ text: String,
        style: UIFont.TextStyle,
        weight: UIFont.Weight? = nil,
        color: UIColor = .label
    ) -> UILabel {
        let result = UILabel()
        let base = UIFont.preferredFont(forTextStyle: style)
        result.font = weight.map { UIFont.systemFont(ofSize: base.pointSize, weight: $0) } ?? base
        result.adjustsFontForContentSizeCategory = true
        result.textColor = color
        result.text = text
        return result
    }

    private func projectMenu() -> UIMenu {
        var actions = projectStore.recentProjects.map { path in
            UIAction(
                title: (path as NSString).abbreviatingWithTildeInPath,
                image: UIImage(systemName: "folder"),
                state: path == selectedProjectPath ? .on : .off
            ) { [weak self] _ in
                self?.selectProject(path)
            }
        }
        actions.append(UIAction(
            title: "Choose another folder…",
            image: UIImage(systemName: "folder.badge.plus")
        ) { [weak self] _ in
            self?.presentFolderPicker()
        })
        return UIMenu(children: actions)
    }

    private func selectProject(_ path: String) {
        guard projectStore.remember(projectPath: path) else {
            showStatus("That project folder could not be remembered. Choose another folder.")
            return
        }
        selectedProjectPath = path
        refreshProjectUI()
        if let destination = pendingDestination {
            pendingDestination = nil
            launch(destination)
        }
    }

    private func refreshProjectUI() {
        if let path = selectedProjectPath, !path.isEmpty {
            projectNameLabel.text = (path as NSString).lastPathComponent
            projectNameLabel.textColor = .label
            projectPathLabel.text = (path as NSString).abbreviatingWithTildeInPath
        } else {
            projectNameLabel.text = "Choose a project folder"
            projectNameLabel.textColor = .secondaryLabel
            projectPathLabel.text = "PinPoint remembers your last choice for this signed-in account."
        }
        chooseProjectButton.menu = projectMenu()
    }

    private func refreshActionState() {
        let enabled = Self.isAvailable(for: recording) && !launchInFlight
        codexButton.isEnabled = enabled
        claudeButton.isEnabled = enabled
        copyButton.isEnabled = enabled
    }

    private func presentFolderPicker() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        picker.delegate = self
        picker.allowsMultipleSelection = false
        present(picker, animated: true)
    }

    private func launch(_ destination: BetaAssistantDestination) {
        guard Self.isAvailable(for: recording), !launchInFlight else { return }
        guard let path = selectedProjectPath, !path.isEmpty else {
            pendingDestination = destination
            presentFolderPicker()
            return
        }
        let brief = BetaAgentBriefBuilder.brief(
            for: context,
            instruction: instructionField.text,
            maximumTranscriptCharacters: BetaAgentBriefBuilder.deepLinkTranscriptLimit
        )
        guard let url = BetaAgentHandoff.deepLink(
            destination: destination,
            projectPath: path,
            brief: brief
        ) else {
            offerCopyFallback(for: destination)
            return
        }

        launchInFlight = true
        refreshActionState()
        showStatus("Opening \(destination.displayName)…")
        UIApplication.shared.open(url, options: [:]) { [weak self] opened in
            DispatchQueue.main.async {
                guard let self else { return }
                self.launchInFlight = false
                self.refreshActionState()
                if opened {
                    self.showStatus("Opened \(destination.displayName) in “\((path as NSString).lastPathComponent)”.")
                } else {
                    self.offerCopyFallback(for: destination)
                }
            }
        }
    }

    private func clipboardBrief() -> String {
        BetaAgentBriefBuilder.brief(
            for: context,
            instruction: instructionField.text,
            maximumTranscriptCharacters: BetaAgentBriefBuilder.clipboardTranscriptLimit
        )
    }

    private func copyCurrentBrief() {
        UIPasteboard.general.string = clipboardBrief()
        showStatus("Brief copied. Paste it into a new Codex or Claude task.")
    }

    private func offerCopyFallback(for destination: BetaAssistantDestination) {
        let alert = UIAlertController(
            title: "\(destination.displayName) did not open",
            message: "Copy the prepared brief, open a new \(destination.displayName) task, and paste it there.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Copy brief", style: .default) { [weak self] _ in
            self?.copyCurrentBrief()
        })
        present(alert, animated: true)
    }

    private func showStatus(_ text: String) {
        statusLabel.text = text
        statusLabel.isHidden = false
        UIAccessibility.post(notification: .announcement, argument: text)
    }

    private static func reference(for recording: BetaRecording) -> String {
        let suffix = String(recording.deviceSerialNumber.suffix(4))
        return "device ••••\(suffix), session \(recording.sessionID)"
    }

    private static func metadata(for recording: BetaRecording) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        let seconds = max(0, Int(recording.duration.rounded()))
        let minutes = seconds / 60
        let remainder = seconds % 60
        return "\(formatter.string(from: recording.createdAt)) · \(minutes)m \(remainder)s"
    }

    @objc private func openCodex() { launch(.codex) }
    @objc private func openClaude() { launch(.claude) }
    @objc private func copyBrief() { copyCurrentBrief() }

    @objc private func closeTapped() {
        if let navigationController, navigationController.viewControllers.first !== self {
            navigationController.popViewController(animated: true)
        } else {
            dismiss(animated: true)
        }
    }
}

extension BetaAgentHandoffViewController: UIDocumentPickerDelegate {
    func documentPicker(
        _ controller: UIDocumentPickerViewController,
        didPickDocumentsAt urls: [URL]
    ) {
        guard let path = urls.first?.path else {
            pendingDestination = nil
            return
        }
        selectProject(path)
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        pendingDestination = nil
    }
}

extension BetaAgentHandoffViewController: UITextFieldDelegate {
    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        textField.resignFirstResponder()
        return true
    }
}
