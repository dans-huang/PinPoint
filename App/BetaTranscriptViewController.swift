import UIKit

final class BetaTranscriptViewController: UIViewController {
    private let conversationTitle: String
    private let transcript: String

    init(title: String, transcript: String) {
        conversationTitle = title
        self.transcript = transcript
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let eyebrow = UILabel()
        eyebrow.text = "TRANSCRIPT"
        eyebrow.font = .systemFont(ofSize: 12, weight: .semibold)
        eyebrow.textColor = .secondaryLabel

        let titleLabel = UILabel()
        titleLabel.text = conversationTitle
        titleLabel.font = BetaTheme.title(28)
        titleLabel.numberOfLines = 0
        titleLabel.adjustsFontForContentSizeCategory = true
        titleLabel.accessibilityTraits = .header

        let closeButton = BetaTheme.secondaryButton(title: "Done")
        closeButton.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)

        let header = UIStackView(arrangedSubviews: [titleLabel, closeButton])
        header.axis = .horizontal
        header.alignment = .top
        header.spacing = 18

        let textView = UITextView()
        textView.text = transcript
        textView.font = .preferredFont(forTextStyle: .body)
        textView.adjustsFontForContentSizeCategory = true
        textView.textColor = .label
        textView.backgroundColor = .secondarySystemBackground
        textView.layer.cornerRadius = BetaTheme.cornerRadius
        textView.layer.cornerCurve = .continuous
        textView.textContainerInset = UIEdgeInsets(top: 22, left: 20, bottom: 22, right: 20)
        textView.isEditable = false
        textView.isSelectable = true
        textView.accessibilityLabel = "Conversation transcript"

        let stack = UIStackView(arrangedSubviews: [eyebrow, header, textView])
        stack.axis = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 28),
            stack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -28),
            textView.heightAnchor.constraint(greaterThanOrEqualToConstant: 320),
            closeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 88),
        ])
        preferredContentSize = CGSize(width: 760, height: 680)
    }

    @objc private func closeTapped() {
        dismiss(animated: true)
    }
}
