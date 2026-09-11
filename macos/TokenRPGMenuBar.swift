import Cocoa

@main
final class TokenRPGApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()

    // nib 없는 앱은 기본 main(NSApplicationMain)이 delegate 를 만들지 않는다.
    // 직접 붙이지 않으면 프로세스만 뜨고 메뉴 막대 아이콘이 생기지 않는다.
    static func main() {
        let app = NSApplication.shared
        let delegate = TokenRPGApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        guard let button = statusItem.button else { return }
        button.image = NSImage(systemSymbolName: "gamecontroller.fill", accessibilityDescription: "Token RPG")
        button.image?.isTemplate = true
        button.toolTip = "Token RPG"
        button.target = self
        button.action = #selector(togglePopover(_:))

        popover.behavior = .transient
        popover.contentViewController = GamePopoverViewController()
    }

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(sender)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }
}

private struct Hero: Decodable {
    let exp: Int
    let level: Int
    let pct: Double
    let toNext: Int
    let emoji: String
    let title: String
    let hp: Int
    let atk: Int
    let dfn: Double
    let crit: Double
    let spd: Int
}

private struct Provider: Decodable {
    let id: String
    let name: String
    let tokens: Int
}

private struct Status: Decodable {
    let ok: Bool
    let error: String?
    let hero: Hero?
    let providers: [Provider]?
    let projects: Int?
    let days: Int?
    let gamePath: String?
}

private final class GamePopoverViewController: NSViewController {
    private let content = NSStackView()
    private let titleLabel = NSTextField(labelWithString: "Token RPG")
    private let subtitleLabel = NSTextField(labelWithString: "사용량을 불러오는 중…")
    private let progress = NSProgressIndicator()
    private let statsLabel = NSTextField(labelWithString: "")
    private let providerStack = NSStackView()
    private let refreshButton = NSButton(title: "새로고침", target: nil, action: nil)
    private let openButton = NSButton(title: "게임 열기", target: nil, action: nil)
    private let quitButton = NSButton(title: "종료", target: nil, action: nil)

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 370, height: 380))
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(calibratedWhite: 0.075, alpha: 1).cgColor

        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 10
        content.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 18),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -18),
            content.topAnchor.constraint(equalTo: root.topAnchor, constant: 18),
            content.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -16),
        ])

        let header = NSStackView()
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 12
        let icon = NSTextField(labelWithString: "🎮")
        icon.font = .systemFont(ofSize: 36)
        header.addArrangedSubview(icon)
        let names = NSStackView()
        names.orientation = .vertical
        names.spacing = 2
        titleLabel.font = .boldSystemFont(ofSize: 16)
        titleLabel.textColor = .white
        subtitleLabel.font = .systemFont(ofSize: 12)
        subtitleLabel.textColor = .secondaryLabelColor
        names.addArrangedSubview(titleLabel)
        names.addArrangedSubview(subtitleLabel)
        header.addArrangedSubview(names)
        content.addArrangedSubview(header)

        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 100
        progress.translatesAutoresizingMaskIntoConstraints = false
        progress.heightAnchor.constraint(equalToConstant: 8).isActive = true
        content.addArrangedSubview(progress)

        statsLabel.font = .monospacedSystemFont(ofSize: 12, weight: .medium)
        statsLabel.textColor = .systemYellow
        statsLabel.maximumNumberOfLines = 2
        content.addArrangedSubview(statsLabel)

        let divider = NSBox()
        divider.boxType = .separator
        content.addArrangedSubview(divider)

        let providersTitle = NSTextField(labelWithString: "연결된 프로바이더")
        providersTitle.font = .boldSystemFont(ofSize: 12)
        providersTitle.textColor = .secondaryLabelColor
        content.addArrangedSubview(providersTitle)
        providerStack.orientation = .vertical
        providerStack.alignment = .leading
        providerStack.spacing = 4
        content.addArrangedSubview(providerStack)

        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.heightAnchor.constraint(equalToConstant: 4).isActive = true
        content.addArrangedSubview(spacer)

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.distribution = .fillEqually
        buttons.spacing = 8
        refreshButton.bezelStyle = .rounded
        refreshButton.target = self
        refreshButton.action = #selector(refresh)
        openButton.bezelStyle = .rounded
        openButton.target = self
        openButton.action = #selector(openGame)
        quitButton.bezelStyle = .rounded
        quitButton.target = self
        quitButton.action = #selector(quit)
        buttons.addArrangedSubview(refreshButton)
        buttons.addArrangedSubview(openButton)
        buttons.addArrangedSubview(quitButton)
        content.addArrangedSubview(buttons)

        view = root
    }

    override func viewWillAppear() {
        super.viewWillAppear()
        refresh(nil)
    }

    @objc private func refresh(_ sender: Any?) {
        subtitleLabel.stringValue = "사용량을 읽는 중…"
        refreshButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = Self.fetchStatus()
            DispatchQueue.main.async { self?.render(result) }
        }
    }

    private func render(_ result: Result<Status, Error>) {
        refreshButton.isEnabled = true
        while let view = providerStack.arrangedSubviews.first {
            providerStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        switch result {
        case .success(let status) where status.ok && status.hero != nil:
            let hero = status.hero!
            titleLabel.stringValue = "\(hero.emoji)  \(hero.title) · Lv.\(hero.level)"
            subtitleLabel.stringValue = "EXP \(Self.format(hero.exp)) · 다음까지 \(Self.format(hero.toNext))"
            progress.doubleValue = hero.pct
            statsLabel.textColor = .systemYellow
            let defense = String(format: "%.1f", hero.dfn)
            let critical = String(format: "%.1f", hero.crit)
            statsLabel.stringValue = "HP \(hero.hp)  ATK \(hero.atk)  DEF \(defense)\nCRIT \(critical)%  SPD \(hero.spd)"
            for provider in status.providers ?? [] {
                let row = NSTextField(labelWithString: "\(provider.name)                         \(Self.format(provider.tokens))")
                row.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
                row.textColor = .labelColor
                providerStack.addArrangedSubview(row)
            }
            let meta = NSTextField(labelWithString: "\(status.projects ?? 0)개 프로젝트 · \(status.days ?? 0)일 활동")
            meta.font = .systemFont(ofSize: 11)
            meta.textColor = .secondaryLabelColor
            providerStack.addArrangedSubview(meta)
        case .success:
            titleLabel.stringValue = "Token RPG"
            subtitleLabel.stringValue = "아직 사용 기록이 없다. Claude Code 또는 Codex CLI로 작업해라."
            progress.doubleValue = 0
            statsLabel.stringValue = ""
        case .failure(let error):
            titleLabel.stringValue = "Token RPG"
            subtitleLabel.stringValue = "불러오지 못했다"
            progress.doubleValue = 0
            statsLabel.textColor = .systemRed
            statsLabel.stringValue = error.localizedDescription
        }
    }

    @objc private func openGame() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        if let script = Bundle.main.url(forResource: "token_rpg", withExtension: "py")?.path {
            task.arguments = ["python3", script, "open"]
        } else {
            task.arguments = ["token-rpg", "open"]
        }
        try? task.run()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private static func fetchStatus() -> Result<Status, Error> {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        if let script = Bundle.main.url(forResource: "token_rpg", withExtension: "py")?.path {
            process.arguments = ["python3", script, "status"]
        } else {
            process.arguments = ["token-rpg", "status"]
        }
        process.standardOutput = output
        do {
            try process.run()
            process.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            return .success(try JSONDecoder().decode(Status.self, from: data))
        } catch {
            return .failure(error)
        }
    }

    private static func format(_ value: Int) -> String {
        NumberFormatter.localizedString(from: NSNumber(value: value), number: .decimal)
    }
}
