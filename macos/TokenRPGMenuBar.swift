import Cocoa
import WebKit

/// token_rpg.SERVE_PORT 와 같아야 한다. 브라우저도 이 주소를 써서 저장 파일 하나를 같이 쓴다.
private let gameURL = URL(string: "http://127.0.0.1:8765/")!

@main
final class TokenRPGApp: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var statusItem: NSStatusItem!
    private var lastStatus: Status?
    private let popover = NSPopover()
    private let game = GameViewController()
    private var server: Process?

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
        popover.delegate = self
        popover.contentSize = NSSize(width: 420, height: 640)
        popover.contentViewController = game

        server = startServer()
        refresh()
        // Stop 훅은 Claude Code 응답 때만 돈다. Codex·Gemini 사용분도 반영되도록 주기적으로 build 한다.
        // ponytail: 5분 폴링 (build+status 약 2초). 즉시 반영이 필요하면 로그 폴더 FSEvents 감시로
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.terminate()
    }

    /// 게임 페이지와 저장 파일을 제공하는 로컬 서버(token-rpg serve). 이미 떠 있으면 그쪽을 쓰고 바로 끝난다.
    private func startServer() -> Process? {
        let process = tokenRPGProcess(["serve"])
        process.standardOutput = FileHandle.nullDevice
        return (try? process.run()) != nil ? process : nil
    }

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(sender)
        } else {
            markSeen()
            game.loadIfChanged()
            NSApp.activate(ignoringOtherApps: true)     // 키 입력(⌘C·⌘V)이 팝오버로 가게
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    /// build 는 game.html 을 다시 쓴다 -> mtime 이 바뀌어 다음에 열 때 페이지가 리로드된다.
    /// 리로드는 페이지 안의 상태(자동 도전 승/패 카운터)를 날리므로, 꼭 필요할 때만 돌린다.
    private func refresh(build: Bool = true) {
        DispatchQueue.global(qos: .utility).async {
            if build { _ = runTokenRPG(["-q", "build"]) }
            let status = try? JSONDecoder().decode(Status.self, from: runTokenRPG(["status"]))
            DispatchQueue.main.async { self.show(status) }
        }
    }

    private func show(_ status: Status?) {
        guard let button = statusItem.button else { return }
        lastStatus = status ?? lastStatus
        game.gamePath = status?.gamePath ?? game.gamePath
        guard let h = status?.hero else {
            button.title = ""
            button.toolTip = "Token RPG — 아직 사용 기록이 없다"
            return
        }
        let defaults = UserDefaults.standard
        // 초월하면 레벨이 1로 돌아간다 — 기준을 낮춰 두지 않으면 ⬆ 가 다시는 안 뜬다
        if defaults.integer(forKey: "seenLevel") == 0 || h.level < defaults.integer(forKey: "seenLevel") {
            defaults.set(h.level, forKey: "seenLevel")
        }
        // 배지: ⬆ 레벨 업(팝오버를 열어 보기 전까지) · ⛏ 원정 가득
        let leveledUp = h.level > defaults.integer(forKey: "seenLevel")
        let expedFull = status?.expedFull == true
        button.title = " Lv.\(h.level) \(Int(h.pct))%" + (leveledUp ? " ⬆" : "") + (expedFull ? " ⛏" : "")
        let total = status?.providers?.reduce(0) { $0 + $1.tokens } ?? 0
        var tip = "오늘 \(format(status?.today ?? 0)) 토큰\n"
            + "\(h.emoji) \(h.title) Lv.\(h.level) (\(String(format: "%.1f", h.pct))%)\n"
            + "다음 레벨까지 \(format(h.toNext)) EXP\n누적 \(format(total)) 토큰"
        if leveledUp { tip += "\n⬆ 레벨 업 — 열어서 새 포인트를 배분해라" }
        if expedFull { tip += "\n⛏ 원정이 가득 찼다 — 수령해야 다시 쌓인다" }
        button.toolTip = tip
    }

    /// 팝오버를 열었으면 레벨 업은 확인한 것으로 본다
    private func markSeen() {
        if let level = lastStatus?.hero?.level { UserDefaults.standard.set(level, forKey: "seenLevel") }
        if lastStatus != nil { show(lastStatus) }
    }

    // 닫자마자 배지만 갱신 — 원정을 수령했으면 ⛏ 가 5분을 기다리지 않고 사라진다.
    // build 는 돌리지 않는다. 돌리면 열 때마다 페이지가 리로드돼 자동 도전이 0회로 돌아간다.
    func popoverDidClose(_ notification: Notification) {
        refresh(build: false)
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
    let today: Int?
    let expedFull: Bool?
    let gamePath: String?
}

/// 팝오버 = 게임 화면. 로컬 서버가 주는 game.html 을 그대로 띄운다.
private final class GameViewController: NSViewController, WKNavigationDelegate, WKUIDelegate {
    var gamePath: String?
    private let web = WKWebView()
    private var loadedAt: Date?

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 640))
        web.underPageBackgroundColor = NSColor(red: 13 / 255, green: 17 / 255, blue: 23 / 255, alpha: 1)
        web.navigationDelegate = self
        web.uiDelegate = self

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
    /// 저장은 페이지가 서버에서 직접 받으므로, 여기서는 game.html 이 새로 만들어졌을 때만 다시 띄운다.
    func loadIfChanged() {
        _ = view
        guard let path = gamePath else { return rebuild(nil) }
        let mtime = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
        guard let mtime, mtime != loadedAt else { return }
        loadedAt = mtime
        web.load(URLRequest(url: gameURL))
    }

    /// target="_blank" 링크. WKWebView 는 이걸 구현하지 않으면 새 창을 못 만들어
    /// 클릭을 조용히 버린다 — 팝오버 안에서 "눌러도 무반응" 으로 보인다.
    /// 팝오버에 창을 띄울 자리가 없으니 기본 브라우저로 넘긴다.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// 팝오버는 게임 한 페이지만 띄운다. 바깥 주소로 나가려 하면 브라우저로 보낸다 —
    /// 여기서 그냥 두면 게임이 그 페이지로 덮여 돌아올 길이 없다.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard navigationAction.navigationType == .linkActivated,
              let url = navigationAction.request.url,
              url.host != "127.0.0.1", url.host != "localhost" else {
            return decisionHandler(.allow)
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    // 서버가 아직 안 떴거나 죽었으면 안내하고, 다음에 열 때 다시 시도한다
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loadedAt = nil
        showMessage("게임 서버에 연결하지 못했다. 잠시 후 다시 열어라.")
    }

    @objc private func rebuild(_ sender: Any?) {
        DispatchQueue.global(qos: .userInitiated).async {
            // 손으로 누른 갱신에서만 새 버전을 다시 확인한다 — Stop 훅이 부르는 build 는 응답마다 돈다
            let out = String(decoding: runTokenRPG(["build", "--check-update"]), as: UTF8.self)
            let first = out.split(separator: "\n").first.map(String.init)
            DispatchQueue.main.async {
                guard let path = first, FileManager.default.fileExists(atPath: path) else {
                    return self.showMessage("아직 사용 기록이 없다. Claude Code·Codex CLI·Gemini CLI 로 작업하면 캐릭터가 생긴다.")
                }
                self.gamePath = path
                self.loadedAt = nil
                self.loadIfChanged()
            }
        }
    }

    private func showMessage(_ text: String) {
        web.loadHTMLString("""
            <body style="background:#0d1117;color:#8b949e;font:13px -apple-system;padding:24px">\(text)</body>
            """, baseURL: nil)
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
private func tokenRPGProcess(_ args: [String]) -> Process {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    if let script = Bundle.main.url(forResource: "token_rpg", withExtension: "py")?.path {
        process.arguments = ["python3", script] + args
    } else {
        process.arguments = ["token-rpg"] + args
    }
    process.standardError = FileHandle.nullDevice
    return process
}

private func runTokenRPG(_ args: [String]) -> Data {
    let process = tokenRPGProcess(args)
    let output = Pipe()
    process.standardOutput = output
    guard (try? process.run()) != nil else { return Data() }
    let data = output.fileHandleForReading.readDataToEndOfFile()   // 기다리기 전에 읽어야 파이프가 막히지 않는다
    process.waitUntilExit()
    return data
}

/// 1,000 이상은 게임 화면과 같이 1.25K · 47.5M 으로 줄인다
private func format(_ value: Int) -> String {
    value.formatted(.number.notation(.compactName).precision(.significantDigits(1...3))
                        .locale(Locale(identifier: "en")))
}
