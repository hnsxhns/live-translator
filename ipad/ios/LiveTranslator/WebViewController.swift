import UIKit
import WebKit

/// Shows the bundled Live Translator web app full screen and gives it what Safari would:
/// microphone, alert/confirm/prompt dialogs, the share sheet (Backup, Journal PDF, certificates),
/// mail / WhatsApp links, and keeping the screen on while listening.
final class WebViewController: UIViewController, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    private var server: LocalServer!
    private var webView: WKWebView!
    private let status = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.17, green: 0.17, blue: 0.17, alpha: 1)

        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? ""
        let build = info["CFBundleVersion"] as? String ?? ""
        let selftest = ProcessInfo.processInfo.arguments.contains("-selftest")

        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.websiteDataStore = .default()                       // keeps login, library, settings
        let ucc = config.userContentController
        ucc.add(self, name: "lt")
        // tells the page it runs inside the app (no web updates / service worker; share through iOS)
        ucc.addUserScript(WKUserScript(source: "window.LT_NATIVE = {version: '\(version)', build: '\(build)'};",
                                       injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if selftest {                                               // CI: report when the speech models are loaded
            ucc.addUserScript(WKUserScript(source: Self.selftestJS, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        }

        webView = WKWebView(frame: view.bounds, configuration: config)
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = view.backgroundColor
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        view.addSubview(webView)

        status.textColor = .lightGray
        status.font = .systemFont(ofSize: 15)
        status.numberOfLines = 0
        status.textAlignment = .center
        status.frame = view.bounds.insetBy(dx: 40, dy: 40)
        status.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(status)

        let site = Bundle.main.resourceURL!.appendingPathComponent("site")
        server = LocalServer(root: site)
        server.start { [weak self] error in
            guard let self else { return }
            if let error {
                self.status.text = "Không khởi động được app: \(error.localizedDescription)"
                return
            }
            self.status.removeFromSuperview()
            self.webView.load(URLRequest(url: self.server.baseURL))
        }
    }

    // MARK: - microphone

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" ? .grant : .prompt)   // iOS still asks the user once
    }

    // MARK: - alert / confirm / prompt

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Huỷ", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        a.addTextField { $0.text = defaultText }
        a.addAction(UIAlertAction(title: "Huỷ", style: .cancel) { _ in completionHandler(nil) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(a.textFields?.first?.text ?? "") })
        present(a, animated: true)
    }

    // MARK: - links (mailto:, whatsapp:, https pages) open outside the app

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        let internal = (url.host == "127.0.0.1") || ["about", "blob", "data"].contains(url.scheme ?? "")
        if internal { return decisionHandler(.allow) }
        UIApplication.shared.open(url)
        decisionHandler(.cancel)
    }

    // MARK: - messages from the page

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let m = message.body as? [String: Any], let type = m["type"] as? String else { return }
        switch type {
        case "keepAwake":
            UIApplication.shared.isIdleTimerDisabled = (m["on"] as? Bool) ?? false
        case "share":
            share(name: m["name"] as? String ?? "file", base64: m["b64"] as? String ?? "", text: m["text"] as? String)
        case "log":
            NSLog("LT: %@", (m["msg"] as? String) ?? "")
        default:
            break
        }
    }

    /// Share sheet with the file: Save to Files, AirDrop, Mail, WhatsApp...
    private func share(name: String, base64: String, text: String?) {
        guard let data = Data(base64Encoded: base64) else { return }
        let safe = name.replacingOccurrences(of: "/", with: "_")
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent(safe)
        guard (try? data.write(to: file)) != nil else { return }
        var items: [Any] = [file]
        if let text, !text.isEmpty { items.append(text) }
        let vc = UIActivityViewController(activityItems: items, applicationActivities: nil)
        if let pop = vc.popoverPresentationController {            // iPad: anchor the sheet
            pop.sourceView = view
            pop.sourceRect = CGRect(x: view.bounds.midX, y: view.bounds.midY, width: 1, height: 1)
            pop.permittedArrowDirections = []
        }
        present(vc, animated: true)
    }

    private static let selftestJS = """
    (function () {
      var t0 = Date.now();
      var timer = setInterval(function () {
        var app = window.app, s = document.getElementById('loginStatus');
        var p = document.querySelector('.progress span');
        var info = {ready: !!(app && app.ready), whisper: app && app.recognizer && app.recognizer.whisperMode,
                    voices: !!(app && app.recognizer && app.recognizer.spk), vosk: !!(app && app.recognizer && app.recognizer.vosk),
                    login: s && s.textContent, progress: p && p.textContent, url: location.href, secs: Math.round((Date.now() - t0) / 1000)};
        window.webkit.messageHandlers.lt.postMessage({type: 'log', msg: 'selftest ' + JSON.stringify(info)});
        if (info.ready || info.secs > 150) clearInterval(timer);
      }, 5000);
    })();
    """
}
