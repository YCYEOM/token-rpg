import AVFoundation
import Cocoa
import WebKit

// 홍보 영상 녹화기. 게임 페이지를 창에 띄우고 setup(녹화 안 함) -> demo(녹화) 순서로 JS 를
// 돌리며 스냅샷을 실제 경과 시간 그대로 H.264 로 쓴다. demo 가 끝나면 녹화를 닫는다.
//
//   swiftc -O -o /tmp/record scripts/promo/record.swift -framework WebKit -framework AVFoundation
//   python3 -c "import token_rpg as t; t.build(out='/tmp/game.html')"
//   PROMO_ONSCREEN=1 /tmp/record file:///tmp/game.html scripts/promo/setup.js scripts/promo/demo.js /tmp/raw.mov
//   ffmpeg -i /tmp/raw.mov -r 30 -c:v libx264 -crf 20 -pix_fmt yuv420p -movflags +faststart -an promo.mp4
//
// file:// 로 여는 게 중요하다 — http 로 열면 put() 이 진짜 저장 파일을 덮어쓴다.
// PROMO_ONSCREEN=1 도 필수 — 화면 밖 창은 WebKit 이 CSS 전환을 아예 안 돌려서 자막이 안 보인다.
let args = CommandLine.arguments
let url = URL(string: args[1])!
let setupJS = try! String(contentsOfFile: args[2], encoding: .utf8)
let demoJS = try! String(contentsOfFile: args[3], encoding: .utf8)
let outURL = URL(fileURLWithPath: args[4])
let W = 540, H = 960, SCALE = 2          // CSS 뷰포트 540x960 -> 영상 1080x1920 (세로)

final class Recorder: NSObject, WKNavigationDelegate {
    let web: WKWebView
    var writer: AVAssetWriter!
    var input: AVAssetWriterInput!
    var adaptor: AVAssetWriterInputPixelBufferAdaptor!
    var start: CFTimeInterval = 0
    var busy = false, recording = false, frames = 0, lastT = -1.0
    var timer: Timer?

    init(web: WKWebView) { self.web = web }

    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {       // 부팅(저장 불러오기)을 기다린다
            w.callAsyncJavaScript(setupJS, arguments: [:], in: nil, in: .page) { r in
                if case .failure(let e) = r { print("setup 오류:", e); exit(1) }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { self.begin() }
            }
        }
    }

    func begin() {
        try? FileManager.default.removeItem(at: outURL)
        writer = try! AVAssetWriter(outputURL: outURL, fileType: .mov)
        input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: W * SCALE, AVVideoHeightKey: H * SCALE,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 14_000_000]])
        input.expectsMediaDataInRealTime = true
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: W * SCALE, kCVPixelBufferHeightKey as String: H * SCALE])
        writer.add(input)
        writer.startWriting()
        writer.startSession(atSourceTime: .zero)
        start = CACurrentMediaTime()
        recording = true
        timer = Timer(timeInterval: 1.0 / 30, repeats: true) { _ in self.grab() }
        RunLoop.main.add(timer!, forMode: .common)
        web.callAsyncJavaScript(demoJS, arguments: [:], in: nil, in: .page) { r in
            switch r {
            case .success(let v): print("demo:", v)
            case .failure(let e): print("demo 오류:", e)
            }
            self.finish()
        }
    }

    func grab() {
        guard recording, !busy else { return }        // 앞 스냅샷이 밀리면 건너뛴다 — 시간은 실제 경과로 찍는다
        busy = true
        let t = CACurrentMediaTime() - start
        let cfg = WKSnapshotConfiguration()
        cfg.snapshotWidth = NSNumber(value: W * SCALE)
        web.takeSnapshot(with: cfg) { img, _ in
            defer { self.busy = false }
            guard self.recording, t > self.lastT, self.input.isReadyForMoreMediaData,
                  let img, let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil),
                  let pool = self.adaptor.pixelBufferPool else { return }
            var pb: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, pool, &pb)
            guard let pb else { return }
            CVPixelBufferLockBaseAddress(pb, [])
            let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb), width: W * SCALE, height: H * SCALE,
                                bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pb),
                                space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
            ctx.interpolationQuality = .high
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W * SCALE, height: H * SCALE))
            CVPixelBufferUnlockBaseAddress(pb, [])
            self.adaptor.append(pb, withPresentationTime: CMTime(seconds: t, preferredTimescale: 600))
            self.lastT = t
            self.frames += 1
        }
    }

    func finish() {
        recording = false
        timer?.invalidate()
        let dur = CACurrentMediaTime() - start
        input.markAsFinished()
        writer.finishWriting {
            print(String(format: "녹화 끝: %.1f초, %d프레임 (평균 %.1f fps)", dur, self.frames, Double(self.frames) / dur))
            exit(0)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled],
                                                     reason: "promo recording")   // App Nap 으로 타이머가 느려지지 않게
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: W, height: H))
web.pageZoom = 1.2                                   // 레이아웃 폭 450px = 메뉴 막대 팝오버와 비슷
let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: W, height: H), styleMask: [.borderless],
                   backing: .buffered, defer: false)
win.contentView = web
// 화면 밖이나 가려진 창은 WebKit 이 타이머·애니메이션을 늦춘다 -> PROMO_ONSCREEN=1 이면 화면 구석에 띄운다
let env = ProcessInfo.processInfo.environment
if env["PROMO_ONSCREEN"] == "1" {
    win.setFrameOrigin(NSPoint(x: 0, y: 0))
    win.level = .floating
    win.ignoresMouseEvents = true
} else {
    win.setFrameOrigin(NSPoint(x: -4000, y: -4000))
}
win.alphaValue = CGFloat(Double(env["PROMO_ALPHA"] ?? "1") ?? 1)
win.orderFront(nil)
let rec = Recorder(web: web)
web.navigationDelegate = rec
web.load(URLRequest(url: url))
withExtendedLifetime(activity) { app.run() }
