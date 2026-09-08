import UIKit
import UserNotifications

final class BetaConversationViewController: UIViewController {
    var onOpenTranscript: ((BetaRecording) -> Void)?
    var onOpenAssistant: ((BetaRecording) -> Void)?

    private let recording: BetaRecording
    private let intelligence: BetaIntelligenceCoordinator
    private let summaryTextView = UITextView()
    private let stateLabel = UILabel()
    private let templateButton = UIButton(type: .system)
    private let improveButton = UIButton(type: .system)
    private let generateButton = UIButton(type: .system)
    private let momentsLabel = UILabel()
    private let headerStack = UIStackView()
    private let summaryHeaderStack = UIStackView()
    private let actionsStack = UIStackView()
    private let viewport = UIScrollView()
    private var selectedTemplateID: String?
    private var templates: [BetaSummaryTemplate] = []
    private var currentSummary: BetaSummarySnapshot?
    private var actionInFlight = false
    private var activeObserver: NSObjectProtocol?
    private var presentedReviewJobID: String?

    init(recording: BetaRecording, intelligence: BetaIntelligenceCoordinator) {
        self.recording = recording
        self.intelligence = intelligence
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .formSheet
        preferredContentSize = CGSize(width: 920, height: 720)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        configureLayout()
        loadTemplates()
        loadSummary()
        resumePendingWordingReview()
        activeObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.presentPendingReviewIfPossible()
        }
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        presentPendingReviewIfPossible()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let compact = view.bounds.width < 680
        actionsStack.axis = compact ? .vertical : .horizontal
        actionsStack.distribution = compact ? .fill : .fillEqually
        summaryHeaderStack.axis = compact ? .vertical : .horizontal
        summaryHeaderStack.alignment = compact ? .fill : .center
        summaryHeaderStack.distribution = compact ? .fill : .equalSpacing
    }

    deinit {
        if let activeObserver { NotificationCenter.default.removeObserver(activeObserver) }
    }

    private func configureLayout() {
        let eyebrow = label("CONVERSATION INTELLIGENCE", style: .caption1, weight: .semibold, color: .secondaryLabel)
        let title = label(recording.title, style: .largeTitle, weight: .bold)
        title.accessibilityTraits = .header

        let metadata = label(Self.metadata(recording), style: .subheadline, color: .secondaryLabel)
        let close = UIButton(type: .system)
        close.setImage(UIImage(systemName: "xmark"), for: .normal)
        close.tintColor = .label
        close.backgroundColor = .tertiarySystemFill
        close.layer.cornerRadius = 20
        close.accessibilityLabel = "Close"
        close.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)
        NSLayoutConstraint.activate([
            close.widthAnchor.constraint(equalToConstant: 42),
            close.heightAnchor.constraint(equalToConstant: 42),
        ])
        let titleStack = UIStackView(arrangedSubviews: [eyebrow, title, metadata])
        titleStack.axis = .vertical
        titleStack.spacing = 6
        headerStack.addArrangedSubview(titleStack)
        headerStack.addArrangedSubview(close)
        headerStack.axis = .horizontal
        headerStack.alignment = .top
        headerStack.spacing = 20

        stateLabel.font = .preferredFont(forTextStyle: .subheadline)
        stateLabel.textColor = .secondaryLabel
        stateLabel.numberOfLines = 0
        stateLabel.adjustsFontForContentSizeCategory = true

        templateButton.setTitle("Adaptive Summary", for: .normal)
        templateButton.setImage(UIImage(systemName: "doc.text"), for: .normal)
        templateButton.tintColor = .label
        templateButton.contentHorizontalAlignment = .leading
        templateButton.showsMenuAsPrimaryAction = true
        templateButton.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        templateButton.accessibilityLabel = "Summary template"

        summaryHeaderStack.addArrangedSubview(label("Summary", style: .title2, weight: .bold))
        summaryHeaderStack.addArrangedSubview(templateButton)
        summaryHeaderStack.axis = .horizontal
        summaryHeaderStack.alignment = .center
        summaryHeaderStack.spacing = 12
        summaryHeaderStack.distribution = .equalSpacing

        summaryTextView.isEditable = false
        summaryTextView.isSelectable = true
        summaryTextView.backgroundColor = .secondarySystemGroupedBackground
        summaryTextView.textColor = .label
        summaryTextView.font = .preferredFont(forTextStyle: .body)
        summaryTextView.adjustsFontForContentSizeCategory = true
        summaryTextView.layer.cornerRadius = BetaTheme.cornerRadius
        summaryTextView.layer.cornerCurve = .continuous
        summaryTextView.textContainerInset = UIEdgeInsets(top: 22, left: 20, bottom: 22, right: 20)
        summaryTextView.text = "Preparing your summary…"
        summaryTextView.accessibilityLabel = "AI summary"

        configureAction(improveButton, title: "Improve wording", symbol: "wand.and.stars", primary: true)
        configureAction(generateButton, title: "Regenerate", symbol: "arrow.clockwise", primary: false)
        let transcript = BetaTheme.secondaryButton(title: "Transcript", image: "text.alignleft")
        let assistant = BetaTheme.secondaryButton(title: "Continue with AI", image: "arrow.up.right.square")
        improveButton.addTarget(self, action: #selector(improveTapped), for: .touchUpInside)
        generateButton.addTarget(self, action: #selector(generateTapped), for: .touchUpInside)
        transcript.addTarget(self, action: #selector(transcriptTapped), for: .touchUpInside)
        assistant.addTarget(self, action: #selector(assistantTapped), for: .touchUpInside)
        [improveButton, generateButton, transcript, assistant].forEach(actionsStack.addArrangedSubview)
        actionsStack.axis = .horizontal
        actionsStack.distribution = .fillEqually
        actionsStack.spacing = 10

        momentsLabel.font = .preferredFont(forTextStyle: .subheadline)
        momentsLabel.textColor = .secondaryLabel
        momentsLabel.numberOfLines = 0
        momentsLabel.text = "Marked moments are checked quietly during the next recording window."
        let momentsCard = card([
            label("Marked moments", style: .headline, weight: .semibold),
            momentsLabel,
        ])

        let summaryCard = card([summaryHeaderStack, stateLabel, summaryTextView, actionsStack], spacing: 14)
        summaryTextView.heightAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true

        let stack = UIStackView(arrangedSubviews: [headerStack, summaryCard, momentsCard])
        stack.axis = .vertical
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        viewport.alwaysBounceVertical = true
        viewport.keyboardDismissMode = .interactive
        viewport.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(viewport)
        viewport.addSubview(stack)
        NSLayoutConstraint.activate([
            viewport.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            viewport.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            viewport.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            viewport.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: viewport.contentLayoutGuide.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: viewport.contentLayoutGuide.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: viewport.contentLayoutGuide.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: viewport.contentLayoutGuide.bottomAnchor, constant: -22),
            stack.widthAnchor.constraint(equalTo: viewport.frameLayoutGuide.widthAnchor, constant: -48),
        ])
        refreshActions()
    }

    func renderMarkedMoments(_ record: BetaRecordingMarkedMoments?) {
        guard let record else {
            momentsLabel.text = "Marked moments are checked quietly during the next recording window."
            return
        }
        switch record.status {
        case .pending:
            momentsLabel.text = "Waiting for the next recording window to check button presses."
        case .none:
            momentsLabel.text = "No button-marked moments in this conversation."
        case .unavailable:
            momentsLabel.text = "The recorder did not return marked moments after several safe attempts. Summary and AI handoff still work."
        case .ready:
            let times = record.tags.prefix(8).map { Self.timecode($0.timestamp) }.joined(separator: "  ·  ")
            momentsLabel.text = "\(record.tags.count) marked moment\(record.tags.count == 1 ? "" : "s")\(times.isEmpty ? "" : "\n" + times)"
        }
    }

    private func loadSummary() {
        stateLabel.text = recording.transcriptionID == nil
            ? "This older local record does not contain the Partner summary reference. Its transcript can still be reviewed or handed to an assistant."
            : "Loading the latest PinPoint Summary…"
        intelligence.loadSummary(for: recording) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let summary?): self.render(summary)
            case .success(nil):
                self.currentSummary = nil
                self.summaryTextView.text = self.recording.transcriptionID == nil
                    ? "No summary is available for this older recording."
                    : "No summary yet. Generate one now, or enable Automatic flow for future conversations."
                self.stateLabel.text = self.recording.transcriptionID == nil
                    ? "Transcript ready · summary reference unavailable"
                    : "Transcript ready · summary not generated"
                self.refreshActions()
            case .failure(let error):
                self.summaryTextView.text = "Your transcript is safe. PinPoint could not load its summary."
                self.stateLabel.text = error.localizedDescription
                self.refreshActions()
            }
        }
    }

    private func loadTemplates() {
        intelligence.client.listTemplates(sessionToken: intelligence.session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self, case .success(let templates) = result else { return }
                self.templates = templates.filter { !$0.isArchived }
                self.rebuildTemplateMenu()
            }
        }
        intelligence.client.getSettings(sessionToken: intelligence.session.sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                guard let self, case .success(let settings) = result else { return }
                if self.selectedTemplateID == nil, self.currentSummary?.templateID == nil {
                    self.selectedTemplateID = settings.defaultTemplateID
                    self.rebuildTemplateMenu()
                }
            }
        }
    }

    private func resumePendingWordingReview() {
        guard intelligence.hasPendingWordingReview(for: recording) else { return }
        actionInFlight = true
        stateLabel.text = "Resuming your background wording review…"
        refreshActions()
        let userID = intelligence.session.userID
        let inbox = BetaPendingWordingReviews.shared
        let notifier = BetaWordingReviewNotifier.shared
        intelligence.resumePendingWordingReview(for: recording) { [weak self, inbox, notifier] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let job?):
                    guard job.status == .proposed, job.proposedSummary != nil else {
                        self?.actionInFlight = false
                        self?.refreshActions()
                        return
                    }
                    inbox.save(job, userID: userID)
                    guard let self else {
                        notifier.notifyReviewReady(jobID: job.id)
                        return
                    }
                    self.actionInFlight = false
                    self.refreshActions()
                    if self.canPresentReviewNow {
                        self.presentReview(job)
                    } else {
                        self.stateLabel.text = "Improved wording is ready for your approval."
                    }
                case .success(nil):
                    self?.actionInFlight = false
                    self?.refreshActions()
                case .failure(let error):
                    guard let self else { return }
                    self.actionInFlight = false
                    self.stateLabel.text = error.localizedDescription
                    self.refreshActions()
                }
            }
        }
    }

    private func rebuildTemplateMenu() {
        let actions = templates.map { template in
            UIAction(
                title: template.name,
                state: template.id == (selectedTemplateID ?? currentSummary?.templateID) ? .on : .off
            ) { [weak self] _ in
                self?.selectedTemplateID = template.id
                self?.templateButton.setTitle(template.name, for: .normal)
                self?.rebuildTemplateMenu()
            }
        }
        templateButton.menu = UIMenu(title: "Use template", children: actions)
        if let id = selectedTemplateID ?? currentSummary?.templateID,
           let selected = templates.first(where: { $0.id == id }) {
            templateButton.setTitle(selected.name, for: .normal)
        }
    }

    private func render(_ summary: BetaSummarySnapshot) {
        currentSummary = summary
        selectedTemplateID = summary.templateID
        summaryTextView.text = summary.text
        if let updatedAt = summary.updatedAt {
            stateLabel.text = "Latest approved summary · version \(summary.version) · \(Self.relativeDate(updatedAt))"
        } else {
            stateLabel.text = "Latest approved summary · version \(summary.version)"
        }
        rebuildTemplateMenu()
        refreshActions()
    }

    @objc private func generateTapped() {
        guard !actionInFlight else { return }
        setBusy("Generating a fresh summary…")
        intelligence.generateSummary(for: recording, templateID: selectedTemplateID) { [weak self] result in
            guard let self else { return }
            self.actionInFlight = false
            switch result {
            case .success(let summary): self.render(summary)
            case .failure(let error): self.stateLabel.text = error.localizedDescription; self.refreshActions()
            }
        }
    }

    @objc private func improveTapped() {
        guard currentSummary != nil, !actionInFlight else { return }
        BetaWordingReviewNotifier.shared.prepareForExplicitAction()
        let userID = intelligence.session.userID
        let inbox = BetaPendingWordingReviews.shared
        let notifier = BetaWordingReviewNotifier.shared
        setBusy("Reviewing names and wording in the background…")
        intelligence.improveSummary(for: recording, templateID: selectedTemplateID) { [weak self, inbox, notifier] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let job):
                    guard job.status == .proposed, job.proposedSummary != nil else {
                        if let self {
                            self.actionInFlight = false
                            self.stateLabel.text = job.errorMessage ?? "The wording review did not return a proposal."
                            self.refreshActions()
                        }
                        return
                    }
                    inbox.save(job, userID: userID)
                    guard let self else {
                        notifier.notifyReviewReady(jobID: job.id)
                        return
                    }
                    self.actionInFlight = false
                    self.refreshActions()
                    if self.canPresentReviewNow {
                        self.presentReview(job)
                    } else {
                        self.stateLabel.text = "Improved wording is ready for your approval."
                        notifier.notifyReviewReady(jobID: job.id)
                    }
                case .failure(let error):
                    guard let self else { return }
                    self.actionInFlight = false
                    self.stateLabel.text = error.localizedDescription
                    self.refreshActions()
                }
            }
        }
    }

    private func presentReview(_ job: BetaSummaryJob) {
        guard job.status == .proposed, job.proposedSummary != nil else {
            stateLabel.text = job.errorMessage ?? "The wording review did not return a proposal."
            return
        }
        guard presentedReviewJobID != job.id, presentedViewController == nil else { return }
        presentedReviewJobID = job.id
        BetaWordingReviewNotifier.shared.clear(jobID: job.id)
        let review = BetaSummaryReviewViewController(job: job)
        review.isModalInPresentation = true
        review.onApprove = { [weak self, weak review] accepted in
            guard let self else { return }
            review?.setBusy(true)
            self.intelligence.approve(job, acceptedVocabulary: accepted) { result in
                switch result {
                case .success(let summary):
                    BetaPendingWordingReviews.shared.remove(job, userID: self.intelligence.session.userID)
                    review?.dismiss(animated: true) {
                        self.presentedReviewJobID = nil
                        self.render(summary)
                    }
                case .failure(let error): review?.show(error)
                }
            }
        }
        review.onDiscard = { [weak self, weak review] in
            guard let self else { return }
            review?.setBusy(true)
            self.intelligence.discard(job) { result in
                switch result {
                case .success:
                    BetaPendingWordingReviews.shared.remove(job, userID: self.intelligence.session.userID)
                    review?.dismiss(animated: true) { self.presentedReviewJobID = nil }
                case .failure(let error): review?.show(error)
                }
            }
        }
        present(review, animated: true)
    }

    private var canPresentReviewNow: Bool {
        UIApplication.shared.applicationState == .active
            && isViewLoaded
            && view.window != nil
            && presentedViewController == nil
    }

    private func presentPendingReviewIfPossible() {
        guard canPresentReviewNow,
              let job = BetaPendingWordingReviews.shared.job(
                userID: intelligence.session.userID,
                transcriptionID: recording.transcriptionID
              ) else { return }
        actionInFlight = false
        refreshActions()
        presentReview(job)
    }

    private func setBusy(_ message: String) {
        actionInFlight = true
        stateLabel.text = message
        refreshActions()
    }

    private func refreshActions() {
        let hasReference = recording.transcriptionID != nil
        improveButton.isEnabled = currentSummary != nil && !actionInFlight
        generateButton.isEnabled = hasReference && !actionInFlight
        templateButton.isEnabled = !actionInFlight && !templates.isEmpty
    }

    private func configureAction(_ button: UIButton, title: String, symbol: String, primary: Bool) {
        button.setTitle(title, for: .normal)
        button.setImage(UIImage(systemName: symbol), for: .normal)
        button.tintColor = primary ? .systemBackground : .label
        button.setTitleColor(primary ? .systemBackground : .label, for: .normal)
        button.backgroundColor = primary ? .label : .tertiarySystemFill
        button.layer.cornerRadius = 13
        button.layer.cornerCurve = .continuous
        button.titleLabel?.font = .preferredFont(forTextStyle: .headline)
        button.titleLabel?.adjustsFontForContentSizeCategory = true
        button.heightAnchor.constraint(greaterThanOrEqualToConstant: 50).isActive = true
    }

    private func card(_ views: [UIView], spacing: CGFloat = 10) -> UIView {
        let stack = UIStackView(arrangedSubviews: views)
        stack.axis = .vertical
        stack.spacing = spacing
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.backgroundColor = .secondarySystemGroupedBackground
        stack.layer.cornerRadius = BetaTheme.cornerRadius
        stack.layer.cornerCurve = .continuous
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

    @objc private func transcriptTapped() { onOpenTranscript?(recording) }
    @objc private func assistantTapped() { onOpenAssistant?(recording) }
    @objc private func closeTapped() { dismiss(animated: true) }

    private static func metadata(_ recording: BetaRecording) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        let minutes = max(1, Int(recording.duration / 60))
        return "\(formatter.string(from: recording.createdAt))  ·  \(minutes) min  ·  Transcript ready"
    }

    private static func relativeDate(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private static func timecode(_ seconds: Int) -> String {
        String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}

final class BetaSummaryReviewViewController: UIViewController {
    var onApprove: (([String]) -> Void)?
    var onDiscard: (() -> Void)?

    private let job: BetaSummaryJob
    private let termsStack = UIStackView()
    private let approveButton = BetaTheme.primaryButton(title: "Approve summary", image: "checkmark")
    private let discardButton = BetaTheme.secondaryButton(title: "Discard")
    private let buttons = UIStackView()
    private var selectedTerms = Set<String>()

    init(job: BetaSummaryJob) {
        self.job = job
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .formSheet
        preferredContentSize = CGSize(width: 780, height: 680)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        let title = UILabel()
        title.text = "Review improved wording"
        title.font = BetaTheme.title(28)
        title.numberOfLines = 0

        let note = UILabel()
        note.text = "Nothing changes until you approve. Selected spellings are also added to your Custom Words."
        note.font = .preferredFont(forTextStyle: .subheadline)
        note.textColor = .secondaryLabel
        note.numberOfLines = 0

        let text = UITextView()
        text.text = job.proposedSummary
        text.font = .preferredFont(forTextStyle: .body)
        text.adjustsFontForContentSizeCategory = true
        text.isEditable = false
        text.backgroundColor = .secondarySystemBackground
        text.layer.cornerRadius = BetaTheme.cornerRadius
        text.textContainerInset = UIEdgeInsets(top: 18, left: 18, bottom: 18, right: 18)

        termsStack.axis = .vertical
        termsStack.spacing = 8
        if job.proposedVocabulary.isEmpty {
            let none = UILabel()
            none.text = "No new custom words suggested."
            none.textColor = .secondaryLabel
            termsStack.addArrangedSubview(none)
        } else {
            let heading = UILabel()
            heading.text = "Add to Custom Words"
            heading.font = .preferredFont(forTextStyle: .headline)
            heading.adjustsFontForContentSizeCategory = true
            termsStack.addArrangedSubview(heading)
            job.proposedVocabulary.forEach { term in
                let button = UIButton(type: .system)
                button.setTitle(term, for: .normal)
                button.setImage(UIImage(systemName: "square"), for: .normal)
                button.tintColor = .label
                button.contentHorizontalAlignment = .leading
                button.titleLabel?.font = .preferredFont(forTextStyle: .body)
                button.titleLabel?.adjustsFontForContentSizeCategory = true
                button.titleLabel?.numberOfLines = 0
                button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
                button.accessibilityHint = "Adds this spelling to Custom Words when the summary is approved"
                button.addAction(UIAction { [weak self, weak button] _ in
                    guard let self else { return }
                    if self.selectedTerms.contains(term) { self.selectedTerms.remove(term) }
                    else { self.selectedTerms.insert(term) }
                    button?.setImage(UIImage(systemName: self.selectedTerms.contains(term) ? "checkmark.square.fill" : "square"), for: .normal)
                }, for: .touchUpInside)
                termsStack.addArrangedSubview(button)
            }
        }

        approveButton.addTarget(self, action: #selector(approveTapped), for: .touchUpInside)
        discardButton.addTarget(self, action: #selector(discardTapped), for: .touchUpInside)
        buttons.addArrangedSubview(discardButton)
        buttons.addArrangedSubview(approveButton)
        buttons.axis = .horizontal
        buttons.distribution = .fillEqually
        buttons.spacing = 10

        let stack = UIStackView(arrangedSubviews: [title, note, text, termsStack])
        stack.axis = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        let viewport = UIScrollView()
        viewport.alwaysBounceVertical = true
        viewport.translatesAutoresizingMaskIntoConstraints = false
        buttons.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(viewport)
        viewport.addSubview(stack)
        view.addSubview(buttons)
        NSLayoutConstraint.activate([
            viewport.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            viewport.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            viewport.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            viewport.bottomAnchor.constraint(equalTo: buttons.topAnchor, constant: -14),
            stack.leadingAnchor.constraint(equalTo: viewport.contentLayoutGuide.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: viewport.contentLayoutGuide.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: viewport.contentLayoutGuide.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: viewport.contentLayoutGuide.bottomAnchor, constant: -12),
            stack.widthAnchor.constraint(equalTo: viewport.frameLayoutGuide.widthAnchor, constant: -48),
            buttons.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 24),
            buttons.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -24),
            buttons.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -22),
            text.heightAnchor.constraint(greaterThanOrEqualToConstant: 260),
        ])
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let compact = view.bounds.width < 520
        buttons.axis = compact ? .vertical : .horizontal
        buttons.distribution = compact ? .fill : .fillEqually
    }

    func setBusy(_ busy: Bool) {
        approveButton.isEnabled = !busy
        discardButton.isEnabled = !busy
        approveButton.setTitle(busy ? "Applying…" : "Approve summary", for: .normal)
    }

    func show(_ error: Error) {
        setBusy(false)
        let alert = UIAlertController(title: "Couldn’t apply changes", message: error.localizedDescription, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }

    @objc private func approveTapped() { onApprove?(Array(selectedTerms).sorted()) }
    @objc private func discardTapped() { onDiscard?() }
}

/// Keeps a finished proposal reachable when its originating sheet is closed.
/// This is intentionally memory-only: proposed summary text is never written
/// into preferences or exposed in a notification.
private final class BetaPendingWordingReviews {
    static let shared = BetaPendingWordingReviews()

    private var jobs: [String: BetaSummaryJob] = [:]

    func save(_ job: BetaSummaryJob, userID: String) {
        jobs[key(userID: userID, transcriptionID: job.transcriptionID)] = job
    }

    func job(userID: String, transcriptionID: String?) -> BetaSummaryJob? {
        guard let transcriptionID else { return nil }
        return jobs[key(userID: userID, transcriptionID: transcriptionID)]
    }

    func remove(_ job: BetaSummaryJob, userID: String) {
        jobs.removeValue(forKey: key(userID: userID, transcriptionID: job.transcriptionID))
    }

    private func key(userID: String, transcriptionID: String) -> String {
        "\(userID)\u{0}\(transcriptionID)"
    }
}

private final class BetaWordingReviewNotifier {
    static let shared = BetaWordingReviewNotifier()

    private let center = UNUserNotificationCenter.current()

    /// Called only from the person's explicit Improve wording action. PinPoint
    /// never asks for notification permission during launch or passive sync.
    func prepareForExplicitAction() {
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func notifyReviewReady(jobID: String) {
        center.getNotificationSettings { [center] settings in
            let schedule = {
                let content = UNMutableNotificationContent()
                content.title = "Review improved wording"
                content.body = "A wording proposal is ready for your approval in PinPoint."
                content.sound = .default
                center.add(UNNotificationRequest(
                    identifier: Self.identifier(jobID),
                    content: content,
                    trigger: nil
                ))
            }
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral:
                schedule()
            case .notDetermined:
                center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    if granted { schedule() }
                }
            case .denied:
                break
            @unknown default:
                break
            }
        }
    }

    func clear(jobID: String) {
        let identifier = Self.identifier(jobID)
        center.removePendingNotificationRequests(withIdentifiers: [identifier])
        center.removeDeliveredNotifications(withIdentifiers: [identifier])
    }

    private static func identifier(_ jobID: String) -> String {
        "pinpoint.wording.\(jobID)"
    }
}
