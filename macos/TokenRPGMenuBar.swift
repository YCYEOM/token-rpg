import Cocoa
import WebKit

@main
final class TokenRPGApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private let game = GameViewController()

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
        NSApp.mainMenu = editMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        guard let button = statusItem.button else { return }
        button.image = NSImage(systemSymbolName: "gamecontroller.fill", accessibilityDescription: "Token RPG")
        button.image?.isTemplate = true
        button.imagePosition = .imageLeading
        button.toolTip = "Token RPG"
        button.target = self
        button.action = #selector(togglePopover(_:))

        popover.behavior = .transient
        popover.contentSize = NSSize(width: 420, height: 640)
        popover.contentViewController = game

        refresh()
        // Stop 훅은 Claude Code 응답 때만 돈다. Codex·Gemini 사용분도 반영되도록 주기적으로 build 한다.
        // ponytail: 5분 폴링 (build+status 약 2초). 즉시 반영이 필요하면 로그 폴더 FSEvents 감시로
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(sender)
        } else {
            game.loadIfChanged()
            NSApp.activate(ignoringOtherApps: true)     // 키 입력(⌘C·⌘V)이 팝오버로 가게
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    private func refresh() {
        DispatchQueue.global(qos: .utility).async {
            _ = runTokenRPG(["-q", "build"])
            let status = try? JSONDecoder().decode(Status.self, from: runTokenRPG(["status"]))
            DispatchQueue.main.async { self.show(status) }
        }
    }

    private func show(_ status: Status?) {
        guard let button = statusItem.button else { return }
        game.gamePath = status?.gamePath ?? game.gamePath
        guard let h = status?.hero else {
            button.title = ""
            button.toolTip = "Token RPG — 아직 사용 기록이 없다"
            return
        }
        button.title = " Lv.\(h.level) \(Int(h.pct))%"
        let total = status?.providers?.reduce(0) { $0 + $1.tokens } ?? 0
        button.toolTip = "\(h.emoji) \(h.title) Lv.\(h.level) (\(String(format: "%.1f", h.pct))%)\n"
            + "다음 레벨까지 \(format(h.toNext)) EXP\n누적 \(format(total)) 토큰"
    }

    // 메뉴 막대 앱은 메인 메뉴가 없어서 팝오버 입력칸에서 ⌘C·⌘V·⌘A 가 동작하지 않는다
    private func editMenu() -> NSMenu {
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let item = NSMenuItem()
        item.submenu = edit
        let main = NSMenu()
        main.addItem(item)
        return main
    }
}

private struct Hero: Decodable {
    let level: Int
    let pct: Double
    let toNext: Int
    let emoji: String
    let title: String
}

private struct Provider: Decodable {
    let tokens: Int
}

private struct Status: Decodable {
    let hero: Hero?
    let providers: [Provider]?
    let gamePath: String?
}

/// 팝오버 = 게임 화면. build 가 만든 game.html 을 그대로 띄운다.
private final class GameViewController: NSViewController {
    var gamePath: String?
    private let web = WKWebView()
    private var loadedAt: Date?

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 640))
        web.underPageBackgroundColor = NSColor(red: 13 / 255, green: 17 / 255, blue: 23 / 255, alpha: 1)

        let bar = NSStackView()
        bar.edgeInsets = NSEdgeInsets(top: 6, left: 10, bottom: 6, right: 10)
        bar.setViews([button("↻ 갱신", #selector(rebuild)), button("브라우저로 열기", #selector(openInBrowser))],
                     in: .leading)
        bar.setViews([button("종료", #selector(quit))], in: .trailing)

        for v in [web, bar] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(v)
        }
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: root.topAnchor),
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.bottomAnchor.constraint(equalTo: bar.topAnchor),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        view = root
    }

    /// 팝오버를 열 때만 부른다. 보는 도중에 페이지를 바꾸면 전투·스크롤이 날아간다.
    func loadIfChanged() {
        _ = view
        guard let path = gamePath else { return rebuild(nil) }
        let mtime = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
        guard let mtime, mtime != loadedAt,
              let html = try? String(contentsOfFile: path, encoding: .utf8) else { return }
        loadedAt = mtime
        // 고정된 https 출처로 띄워야 localStorage(게임 저장)가 앱을 다시 켜도 남는다
        web.loadHTMLString(html, baseURL: URL(string: "https://token-rpg.local/"))
    }

    @objc private func rebuild(_ sender: Any?) {
        DispatchQueue.global(qos: .userInitiated).async {
            let out = String(decoding: runTokenRPG(["build"]), as: UTF8.self)
            let first = out.split(separator: "\n").first.map(String.init)
            DispatchQueue.main.async {
                guard let path = first, FileManager.default.fileExists(atPath: path) else {
                    self.web.loadHTMLString("""
                        <body style="background:#0d1117;color:#8b949e;font:13px -apple-system;padding:24px">
                        아직 사용 기록이 없다. Claude Code·Codex CLI·Gemini CLI 로 작업하면 캐릭터가 생긴다.</body>
                        """, baseURL: nil)
                    return
                }
                self.gamePath = path
                self.loadedAt = nil
                self.loadIfChanged()
            }
        }
    }

    @objc private func openInBrowser() {
        DispatchQueue.global(qos: .userInitiated).async { _ = runTokenRPG(["open"]) }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        return b
    }
}

/// 앱에 넣어 둔 token_rpg.py 를 우선 쓰고, 없으면 PATH 의 token-rpg 를 쓴다.
private func runTokenRPG(_ args: [String]) -> Data {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    if let script = Bundle.main.url(forResource: "token_rpg", withExtension: "py")?.path {
        process.arguments = ["python3", script] + args
    } else {
        process.arguments = ["token-rpg"] + args
    }
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    guard (try? process.run()) != nil else { return Data() }
    let data = output.fileHandleForReading.readDataToEndOfFile()   // 기다리기 전에 읽어야 파이프가 막히지 않는다
    process.waitUntilExit()
    return data
}

private func format(_ value: Int) -> String {
    NumberFormatter.localizedString(from: NSNumber(value: value), number: .decimal)
}
