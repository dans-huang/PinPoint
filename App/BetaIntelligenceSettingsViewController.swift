import UIKit

final class BetaIntelligenceSettingsViewController: UITableViewController {
    var onSettingsChanged: (() -> Void)?

    private let client: BetaIntelligenceProviding
    private let sessionTokenProvider: () -> String
    private var sessionToken: String { sessionTokenProvider() }
    private var settings = BetaIntelligenceSettings(
        automaticSummaryEnabled: true,
        defaultTemplateID: "tpl_builtin_adaptive"
    )
    private var templates: [BetaSummaryTemplate] = []
    private var vocabulary: [BetaVocabularyTerm] = []
    private var loadingCount = 0
    private var firstLoadError: Error?
    private let automaticSwitch = UISwitch()

    init(client: BetaIntelligenceProviding, sessionTokenProvider: @escaping () -> String) {
        self.client = client
        self.sessionTokenProvider = sessionTokenProvider
        super.init(style: .insetGrouped)
        title = "Automatic flow"
        modalPresentationStyle = .formSheet
        preferredContentSize = CGSize(width: 720, height: 720)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        tableView.backgroundColor = .systemGroupedBackground
        tableView.register(UITableViewCell.self, forCellReuseIdentifier: "Cell")
        navigationItem.leftBarButtonItem = UIBarButtonItem(
            barButtonSystemItem: .close,
            target: self,
            action: #selector(closeTapped)
        )
        let addWord = UIAction(title: "Custom word", image: UIImage(systemName: "text.badge.plus")) { [weak self] _ in
            self?.promptForWord()
        }
        let addTemplate = UIAction(title: "Summary template", image: UIImage(systemName: "doc.badge.plus")) { [weak self] _ in
            self?.promptForTemplate()
        }
        let add = UIBarButtonItem(
            image: UIImage(systemName: "plus"),
            menu: UIMenu(children: [addWord, addTemplate])
        )
        add.accessibilityLabel = "Add custom word or template"
        navigationItem.rightBarButtonItem = add
        automaticSwitch.addTarget(self, action: #selector(automaticChanged), for: .valueChanged)
        refresh()
    }

    override func numberOfSections(in tableView: UITableView) -> Int { 3 }

    override func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
        switch section {
        case 0: return 1
        case 1: return max(templates.filter { !$0.isArchived }.count, 1)
        default: return max(vocabulary.count, 1)
        }
    }

    override func tableView(_ tableView: UITableView, titleForHeaderInSection section: Int) -> String? {
        switch section {
        case 0: return "After every transcript"
        case 1: return "Default summary template"
        default: return "Custom words"
        }
    }

    override func tableView(_ tableView: UITableView, titleForFooterInSection section: Int) -> String? {
        switch section {
        case 0:
            return "When enabled, PinPoint generates a private summary as soon as Plaud finishes the transcript."
        case 1:
            return "The selected template applies to future conversations. You can regenerate an individual summary from its detail page."
        default:
            return "Correct spellings are supplied to future summaries. Wording review can suggest new terms for your approval."
        }
    }

    override func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = tableView.dequeueReusableCell(withIdentifier: "Cell", for: indexPath)
        var content = cell.defaultContentConfiguration()
        cell.accessoryView = nil
        cell.accessoryType = .none
        cell.selectionStyle = .default
        switch indexPath.section {
        case 0:
            content.text = "Generate summaries automatically"
            content.secondaryText = "Uses your selected template"
            automaticSwitch.isOn = settings.automaticSummaryEnabled
            cell.accessoryView = automaticSwitch
            cell.selectionStyle = .none
        case 1:
            let visible = templates.filter { !$0.isArchived }
            if visible.isEmpty {
                content.text = loadingCount > 0 ? "Loading templates…" : "Templates unavailable"
                content.textProperties.color = .secondaryLabel
                cell.selectionStyle = .none
            } else {
                let template = visible[indexPath.row]
                content.text = template.name
                let isDefault = template.id == settings.defaultTemplateID
                content.secondaryText = template.isBuiltIn
                    ? (isDefault ? "PinPoint built-in · Default" : "PinPoint built-in")
                    : (isDefault ? "Your template · Default" : "Your template")
                if template.isBuiltIn {
                    cell.accessoryType = isDefault ? .checkmark : .none
                } else {
                    cell.accessoryView = managementButton(
                        label: "Manage \(template.name)",
                        actions: [
                            UIAction(title: "Edit", image: UIImage(systemName: "pencil")) { [weak self] _ in
                                self?.presentTemplateEditor(template: template)
                            },
                            UIAction(
                                title: "Archive",
                                image: UIImage(systemName: "archivebox"),
                                attributes: .destructive
                            ) { [weak self] _ in
                                self?.confirmArchive(template)
                            },
                        ]
                    )
                }
            }
        default:
            if vocabulary.isEmpty {
                content.text = loadingCount > 0 ? "Loading custom words…" : "No custom words yet"
                content.secondaryText = loadingCount > 0 ? nil : "Tap + to add a correct spelling"
                content.textProperties.color = .secondaryLabel
                cell.selectionStyle = .none
            } else {
                let term = vocabulary[indexPath.row]
                content.text = term.term
                content.secondaryText = nil
                cell.selectionStyle = .none
                cell.accessoryView = managementButton(
                    label: "Manage \(term.term)",
                    actions: [
                        UIAction(
                            title: "Delete",
                            image: UIImage(systemName: "trash"),
                            attributes: .destructive
                        ) { [weak self] _ in
                            self?.confirmDelete(term)
                        },
                    ]
                )
            }
        }
        content.textProperties.numberOfLines = 0
        content.secondaryTextProperties.numberOfLines = 0
        cell.contentConfiguration = content
        return cell
    }

    override func tableView(_ tableView: UITableView, didSelectRowAt indexPath: IndexPath) {
        tableView.deselectRow(at: indexPath, animated: true)
        guard indexPath.section == 1 else { return }
        let visible = templates.filter { !$0.isArchived }
        guard visible.indices.contains(indexPath.row) else { return }
        settings.defaultTemplateID = visible[indexPath.row].id
        saveSettings()
    }

    override func tableView(
        _ tableView: UITableView,
        trailingSwipeActionsConfigurationForRowAt indexPath: IndexPath
    ) -> UISwipeActionsConfiguration? {
        guard indexPath.section == 2, vocabulary.indices.contains(indexPath.row) else { return nil }
        let term = vocabulary[indexPath.row]
        let delete = UIContextualAction(style: .destructive, title: "Delete") { [weak self] _, _, done in
            self?.client.removeVocabulary(id: term.id, sessionToken: self?.sessionToken ?? "") { result in
                DispatchQueue.main.async {
                    if case .success = result {
                        self?.vocabulary.removeAll { $0.id == term.id }
                        self?.tableView.reloadSections(IndexSet(integer: 2), with: .automatic)
                        self?.onSettingsChanged?()
                        done(true)
                    } else {
                        done(false)
                        if case .failure(let error) = result { self?.show(error) }
                    }
                }
            }
        }
        return UISwipeActionsConfiguration(actions: [delete])
    }

    private func refresh() {
        loadingCount = 3
        firstLoadError = nil
        client.getSettings(sessionToken: sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                if case .success(let value) = result { self?.settings = value }
                self?.finishLoad(result)
            }
        }
        client.listTemplates(sessionToken: sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                if case .success(let value) = result { self?.templates = value }
                self?.finishLoad(result)
            }
        }
        client.listVocabulary(sessionToken: sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                if case .success(let value) = result { self?.vocabulary = value }
                self?.finishLoad(result)
            }
        }
    }

    private func finishLoad<T>(_ result: Result<T, Error>) {
        if case .failure(let error) = result, firstLoadError == nil {
            firstLoadError = error
        }
        loadingCount = max(0, loadingCount - 1)
        tableView.reloadData()
        if loadingCount == 0, let error = firstLoadError { show(error) }
    }

    @objc private func automaticChanged() {
        settings.automaticSummaryEnabled = automaticSwitch.isOn
        saveSettings()
    }

    private func saveSettings() {
        let pending = settings
        client.updateSettings(pending, sessionToken: sessionToken) { [weak self] result in
            DispatchQueue.main.async {
                switch result {
                case .success(let saved):
                    self?.settings = saved
                    self?.tableView.reloadData()
                    self?.onSettingsChanged?()
                case .failure(let error):
                    self?.show(error)
                    self?.refresh()
                }
            }
        }
    }

    private func promptForWord() {
        let alert = UIAlertController(
            title: "Add a custom word",
            message: "Use the exact spelling you want PinPoint to preserve.",
            preferredStyle: .alert
        )
        alert.addTextField { field in
            field.placeholder = "Product, person, or technical term"
            field.autocapitalizationType = .words
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Add", style: .default) { [weak self, weak alert] _ in
            guard let self, let term = alert?.textFields?.first?.text else { return }
            self.client.addVocabulary(term: term, sessionToken: self.sessionToken) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success(let value):
                        self.vocabulary.append(value)
                        self.tableView.reloadSections(IndexSet(integer: 2), with: .automatic)
                        self.onSettingsChanged?()
                    case .failure(let error): self.show(error)
                    }
                }
            }
        })
        present(alert, animated: true)
    }

    private func promptForTemplate() {
        presentTemplateEditor(template: nil)
    }

    private func presentTemplateEditor(template: BetaSummaryTemplate?) {
        guard template?.isBuiltIn != true else { return }
        let editor = BetaTemplateEditorViewController(template: template)
        editor.onSave = { [weak self, weak editor] name, prompt in
            guard let self else { return }
            editor?.setBusy(true)
            let completion: (Result<BetaSummaryTemplate, Error>) -> Void = { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success(let value):
                        if let index = self.templates.firstIndex(where: { $0.id == value.id }) {
                            self.templates[index] = value
                        } else {
                            self.templates.append(value)
                            self.settings.defaultTemplateID = value.id
                        }
                        editor?.dismiss(animated: true)
                        if template == nil {
                            self.saveSettings()
                        } else {
                            self.tableView.reloadSections(IndexSet(integer: 1), with: .automatic)
                            self.onSettingsChanged?()
                        }
                    case .failure(let error):
                        editor?.setBusy(false)
                        editor?.show(error)
                    }
                }
            }
            if let template {
                self.client.updateTemplate(
                    id: template.id,
                    name: name,
                    prompt: prompt,
                    sessionToken: self.sessionToken,
                    completion: completion
                )
            } else {
                self.client.createTemplate(
                    name: name,
                    prompt: prompt,
                    sessionToken: self.sessionToken,
                    completion: completion
                )
            }
        }
        present(editor, animated: true)
    }

    private func confirmArchive(_ template: BetaSummaryTemplate) {
        guard !template.isBuiltIn else { return }
        let alert = UIAlertController(
            title: "Archive \(template.name)?",
            message: template.id == settings.defaultTemplateID
                ? "Future conversations will return to Adaptive Summary. Existing summaries do not change."
                : "The template leaves your picker. Existing summaries do not change.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Archive", style: .destructive) { [weak self] _ in
            guard let self else { return }
            self.client.archiveTemplate(id: template.id, sessionToken: self.sessionToken) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success:
                        self.refresh()
                        self.onSettingsChanged?()
                    case .failure(let error): self.show(error)
                    }
                }
            }
        })
        present(alert, animated: true)
    }

    private func confirmDelete(_ term: BetaVocabularyTerm) {
        let alert = UIAlertController(
            title: "Delete \(term.term)?",
            message: "Future summaries will no longer receive this spelling.",
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Delete", style: .destructive) { [weak self] _ in
            guard let self else { return }
            self.client.removeVocabulary(id: term.id, sessionToken: self.sessionToken) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success:
                        self.vocabulary.removeAll { $0.id == term.id }
                        self.tableView.reloadSections(IndexSet(integer: 2), with: .automatic)
                        self.onSettingsChanged?()
                    case .failure(let error): self.show(error)
                    }
                }
            }
        })
        present(alert, animated: true)
    }

    private func managementButton(label: String, actions: [UIAction]) -> UIButton {
        let button = UIButton(type: .system)
        button.setImage(UIImage(systemName: "ellipsis.circle"), for: .normal)
        button.tintColor = .secondaryLabel
        button.showsMenuAsPrimaryAction = true
        button.menu = UIMenu(children: actions)
        button.accessibilityLabel = label
        button.frame.size = CGSize(width: 44, height: 44)
        return button
    }

    private func show(_ error: Error) {
        guard presentedViewController == nil else { return }
        let alert = UIAlertController(title: "Couldn’t update PinPoint", message: error.localizedDescription, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }

    @objc private func closeTapped() { dismiss(animated: true) }
}

final class BetaTemplateEditorViewController: UIViewController, UITextFieldDelegate, UITextViewDelegate {
    var onSave: ((String, String) -> Void)?

    private let template: BetaSummaryTemplate?
    private let nameField = UITextField()
    private let promptTextView = UITextView()
    private let countLabel = UILabel()
    private let saveButton = BetaTheme.primaryButton(title: "Create template", image: "checkmark")
    private let cancelButton = BetaTheme.secondaryButton(title: "Cancel")
    private let actionsStack = UIStackView()
    private var busy = false

    private static let maximumNameLength = 80
    private static let maximumPromptLength = 4_000

    init(template: BetaSummaryTemplate?) {
        self.template = template
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .formSheet
        preferredContentSize = CGSize(width: 680, height: 640)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        configureLayout()
        renderValidation()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if template == nil { nameField.becomeFirstResponder() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let compact = view.bounds.width < 500
        actionsStack.axis = compact ? .vertical : .horizontal
        actionsStack.distribution = compact ? .fill : .fillEqually
    }

    private func configureLayout() {
        let eyebrow = makeLabel(
            template == nil ? "NEW SUMMARY TEMPLATE" : "EDIT SUMMARY TEMPLATE",
            style: .caption1,
            weight: .bold,
            color: .secondaryLabel
        )
        let title = makeLabel(
            template == nil ? "Create a template" : "Refine your template",
            style: .largeTitle,
            weight: .bold
        )
        title.accessibilityTraits = .header
        let note = makeLabel(
            "Describe the structure, priorities, and tone PinPoint should use for future conversation summaries.",
            style: .subheadline,
            color: .secondaryLabel
        )

        let nameLabel = makeLabel("Template name", style: .headline, weight: .semibold)
        nameField.borderStyle = .none
        nameField.backgroundColor = .secondarySystemGroupedBackground
        nameField.layer.cornerRadius = 13
        nameField.layer.cornerCurve = .continuous
        nameField.font = .preferredFont(forTextStyle: .body)
        nameField.adjustsFontForContentSizeCategory = true
        nameField.placeholder = "For example, Product review"
        nameField.text = template?.name
        nameField.clearButtonMode = .whileEditing
        nameField.returnKeyType = .next
        nameField.delegate = self
        nameField.accessibilityLabel = "Template name"
        nameField.addTarget(self, action: #selector(nameChanged), for: .editingChanged)
        let namePadding = UIView(frame: CGRect(x: 0, y: 0, width: 16, height: 1))
        nameField.leftView = namePadding
        nameField.leftViewMode = .always
        nameField.rightView = UIView(frame: CGRect(x: 0, y: 0, width: 12, height: 1))
        nameField.rightViewMode = .unlessEditing
        nameField.heightAnchor.constraint(greaterThanOrEqualToConstant: 50).isActive = true

        let promptLabel = makeLabel("Summary instructions", style: .headline, weight: .semibold)
        promptTextView.backgroundColor = .secondarySystemGroupedBackground
        promptTextView.layer.cornerRadius = 13
        promptTextView.layer.cornerCurve = .continuous
        promptTextView.font = .preferredFont(forTextStyle: .body)
        promptTextView.adjustsFontForContentSizeCategory = true
        promptTextView.textColor = .label
        promptTextView.textContainerInset = UIEdgeInsets(top: 15, left: 12, bottom: 15, right: 12)
        promptTextView.text = template?.prompt
        promptTextView.delegate = self
        promptTextView.keyboardDismissMode = .interactive
        promptTextView.accessibilityLabel = "Summary instructions"
        promptTextView.heightAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true

        countLabel.font = .preferredFont(forTextStyle: .footnote)
        countLabel.textColor = .secondaryLabel
        countLabel.textAlignment = .right
        countLabel.adjustsFontForContentSizeCategory = true

        let fields = UIStackView(arrangedSubviews: [
            eyebrow,
            title,
            note,
            nameLabel,
            nameField,
            promptLabel,
            promptTextView,
            countLabel,
        ])
        fields.axis = .vertical
        fields.spacing = 12
        fields.setCustomSpacing(6, after: eyebrow)
        fields.setCustomSpacing(7, after: title)
        fields.setCustomSpacing(22, after: note)
        fields.setCustomSpacing(8, after: nameLabel)
        fields.setCustomSpacing(18, after: nameField)
        fields.setCustomSpacing(8, after: promptLabel)
        fields.translatesAutoresizingMaskIntoConstraints = false

        saveButton.setTitle(template == nil ? "Create template" : "Save changes", for: .normal)
        saveButton.addTarget(self, action: #selector(saveTapped), for: .touchUpInside)
        cancelButton.addTarget(self, action: #selector(cancelTapped), for: .touchUpInside)
        actionsStack.addArrangedSubview(cancelButton)
        actionsStack.addArrangedSubview(saveButton)
        actionsStack.axis = .horizontal
        actionsStack.distribution = .fillEqually
        actionsStack.spacing = 10
        actionsStack.translatesAutoresizingMaskIntoConstraints = false

        let viewport = UIScrollView()
        viewport.alwaysBounceVertical = true
        viewport.keyboardDismissMode = .interactive
        viewport.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(viewport)
        viewport.addSubview(fields)
        view.addSubview(actionsStack)
        NSLayoutConstraint.activate([
            viewport.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            viewport.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            viewport.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            viewport.bottomAnchor.constraint(equalTo: actionsStack.topAnchor, constant: -14),
            fields.leadingAnchor.constraint(equalTo: viewport.contentLayoutGuide.leadingAnchor, constant: 24),
            fields.trailingAnchor.constraint(equalTo: viewport.contentLayoutGuide.trailingAnchor, constant: -24),
            fields.topAnchor.constraint(equalTo: viewport.contentLayoutGuide.topAnchor, constant: 22),
            fields.bottomAnchor.constraint(equalTo: viewport.contentLayoutGuide.bottomAnchor, constant: -12),
            fields.widthAnchor.constraint(equalTo: viewport.frameLayoutGuide.widthAnchor, constant: -48),
            actionsStack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 24),
            actionsStack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -24),
            actionsStack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -22),
        ])
    }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        promptTextView.becomeFirstResponder()
        return false
    }

    func textField(
        _ textField: UITextField,
        shouldChangeCharactersIn range: NSRange,
        replacementString string: String
    ) -> Bool {
        guard let current = textField.text,
              let swiftRange = Range(range, in: current) else { return false }
        return current.replacingCharacters(in: swiftRange, with: string).count <= Self.maximumNameLength
    }

    func textView(
        _ textView: UITextView,
        shouldChangeTextIn range: NSRange,
        replacementText text: String
    ) -> Bool {
        guard let current = textView.text,
              let swiftRange = Range(range, in: current) else { return false }
        return current.replacingCharacters(in: swiftRange, with: text).count <= Self.maximumPromptLength
    }

    func textViewDidChange(_ textView: UITextView) { renderValidation() }

    @objc private func nameChanged() { renderValidation() }

    @objc private func saveTapped() {
        guard !busy else { return }
        let name = nameField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let prompt = promptTextView.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !prompt.isEmpty else { return }
        onSave?(name, prompt)
    }

    @objc private func cancelTapped() {
        guard !busy else { return }
        dismiss(animated: true)
    }

    func setBusy(_ busy: Bool) {
        self.busy = busy
        nameField.isEnabled = !busy
        promptTextView.isEditable = !busy
        cancelButton.isEnabled = !busy
        isModalInPresentation = busy
        saveButton.setTitle(
            busy ? "Saving…" : (template == nil ? "Create template" : "Save changes"),
            for: .normal
        )
        renderValidation()
    }

    func show(_ error: Error) {
        setBusy(false)
        let alert = UIAlertController(
            title: "Couldn’t save template",
            message: error.localizedDescription,
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }

    private func renderValidation() {
        let name = nameField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let prompt = promptTextView.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        countLabel.text = "\(promptTextView.text?.count ?? 0) / \(Self.maximumPromptLength)"
        saveButton.isEnabled = !busy && !name.isEmpty && !prompt.isEmpty
    }

    private func makeLabel(
        _ text: String,
        style: UIFont.TextStyle,
        weight: UIFont.Weight? = nil,
        color: UIColor = .label
    ) -> UILabel {
        let label = UILabel()
        label.text = text
        let size = UIFont.preferredFont(forTextStyle: style).pointSize
        label.font = weight.map {
            UIFontMetrics(forTextStyle: style).scaledFont(for: .systemFont(ofSize: size, weight: $0))
        } ?? .preferredFont(forTextStyle: style)
        label.textColor = color
        label.numberOfLines = 0
        label.adjustsFontForContentSizeCategory = true
        return label
    }
}
