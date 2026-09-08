import UIKit

final class BetaRestartViewController: UIViewController {
    private let titleText: String
    private let detailText: String
    private let symbolName: String
    private let noteText: String?

    init(
        titleText: String = "Signed out safely",
        detailText: String = "Press ⌘Q, then reopen PinPoint before signing in again. Plaud requires a cold start when the recorder owner changes.",
        symbolName: String = "checkmark.circle.fill",
        noteText: String? = "Your copied recordings remain on this Mac. Signing out does not erase the recorder or Plaud Cloud."
    ) {
        self.titleText = titleText
        self.detailText = detailText
        self.symbolName = symbolName
        self.noteText = noteText
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let symbol = UIImageView(image: UIImage(systemName: symbolName))
        symbol.preferredSymbolConfiguration = .init(pointSize: 42, weight: .medium)
        symbol.tintColor = .label
        symbol.contentMode = .center
        symbol.accessibilityElementsHidden = true

        let title = UILabel()
        title.text = titleText
        title.font = BetaTheme.title()
        title.numberOfLines = 0
        title.adjustsFontForContentSizeCategory = true
        title.textAlignment = .center
        title.accessibilityTraits.insert(.header)

        let detail = UILabel()
        detail.text = detailText
        detail.font = .preferredFont(forTextStyle: .body)
        detail.textColor = .secondaryLabel
        detail.numberOfLines = 0
        detail.adjustsFontForContentSizeCategory = true
        detail.textAlignment = .center

        let note = UILabel()
        note.text = noteText
        note.font = .preferredFont(forTextStyle: .footnote)
        note.textColor = .secondaryLabel
        note.numberOfLines = 0
        note.adjustsFontForContentSizeCategory = true
        note.textAlignment = .center
        note.isHidden = noteText == nil

        let stack = UIStackView(arrangedSubviews: [symbol, title, detail, note])
        stack.axis = .vertical
        stack.alignment = .fill
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 32, left: 32, bottom: 32, right: 32)
        stack.backgroundColor = .secondarySystemBackground
        stack.layer.cornerRadius = BetaTheme.cornerRadius
        stack.layer.cornerCurve = .continuous
        view.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.safeAreaLayoutGuide.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.safeAreaLayoutGuide.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -24),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: BetaTheme.contentWidth),
        ])

        UIAccessibility.post(notification: .screenChanged, argument: title)
    }
}
