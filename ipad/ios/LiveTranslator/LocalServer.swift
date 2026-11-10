import Foundation
import Network

/// Tiny HTTP server on 127.0.0.1 that serves the web app bundled inside the app (Resources/site).
/// The page is then a "secure context" (localhost), so the microphone, workers, WebAssembly and
/// storage all behave exactly as in Safari. Only this device can reach it (loopback only).
final class LocalServer {
    static let port: UInt16 = 47123                // fixed: the page's saved data belongs to this address
    private let root: URL
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "local-server", qos: .userInitiated)

    init(root: URL) {
        self.root = root.standardizedFileURL
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(Self.port)/")! }

    func start(ready: @escaping (Error?) -> Void) {
        do {
            let params = NWParameters.tcp
            params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: Self.port)!)
            params.allowLocalEndpointReuse = true
            let l = try NWListener(using: params)
            l.newConnectionHandler = { [weak self] c in self?.handle(c) }
            var reported = false
            l.stateUpdateHandler = { state in
                switch state {
                case .ready where !reported:
                    reported = true
                    DispatchQueue.main.async { ready(nil) }
                case .failed(let e) where !reported:
                    reported = true
                    DispatchQueue.main.async { ready(e) }
                default: break
                }
            }
            l.start(queue: queue)
            listener = l
        } catch {
            ready(error)
        }
    }

    // MARK: - requests

    private func handle(_ c: NWConnection) {
        c.start(queue: queue)
        receiveHead(c, buffer: Data())
    }

    private func receiveHead(_ c: NWConnection, buffer: Data) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self else { return }
            var buf = buffer
            if let data { buf.append(data) }
            if let end = buf.range(of: Data("\r\n\r\n".utf8)) {
                let head = String(decoding: buf[..<end.lowerBound], as: UTF8.self)
                self.respond(c, head: head)
            } else if error != nil || done || buf.count > 65536 {
                c.cancel()
            } else {
                self.receiveHead(c, buffer: buf)
            }
        }
    }

    private func respond(_ c: NWConnection, head: String) {
        let parts = head.split(separator: "\r\n").first?.split(separator: " ") ?? []
        guard parts.count >= 2 else { return send(c, status: "400 Bad Request") }
        let method = String(parts[0])
        var path = String(parts[1])
        if let q = path.firstIndex(where: { $0 == "?" || $0 == "#" }) { path = String(path[..<q]) }
        path = path.removingPercentEncoding ?? path
        if path.hasSuffix("/") { path += "index.html" }
        var file = root.appendingPathComponent(String(path.drop(while: { $0 == "/" }))).standardizedFileURL
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: file.path, isDirectory: &isDir), isDir.boolValue {
            file = file.appendingPathComponent("index.html")
        }
        guard file.path.hasPrefix(root.path), method == "GET" || method == "HEAD",
              let attrs = try? FileManager.default.attributesOfItem(atPath: file.path),
              let size = (attrs[.size] as? NSNumber)?.intValue,
              let handle = try? FileHandle(forReadingFrom: file) else {
            return send(c, status: "404 Not Found")
        }
        let header = "HTTP/1.1 200 OK\r\nContent-Type: \(Self.type(of: file.pathExtension))\r\n" +
            "Content-Length: \(size)\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"
        c.send(content: Data(header.utf8), completion: .contentProcessed { [weak self] _ in
            if method == "HEAD" { try? handle.close(); c.cancel(); return }
            self?.stream(c, handle)
        })
    }

    /// Big files (speech models) go out 1 MB at a time.
    private func stream(_ c: NWConnection, _ handle: FileHandle) {
        let chunk = (try? handle.read(upToCount: 1 << 20)) ?? Data()
        if chunk.isEmpty {
            try? handle.close()
            c.send(content: nil, isComplete: true, completion: .contentProcessed { _ in c.cancel() })
            return
        }
        c.send(content: chunk, completion: .contentProcessed { [weak self] error in
            if error != nil { try? handle.close(); c.cancel(); return }
            self?.stream(c, handle)
        })
    }

    private func send(_ c: NWConnection, status: String) {
        let body = Data(status.utf8)
        let head = "HTTP/1.1 \(status)\r\nContent-Type: text/plain\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        c.send(content: Data(head.utf8) + body, isComplete: true, completion: .contentProcessed { _ in c.cancel() })
    }

    static func type(of ext: String) -> String {
        switch ext.lowercased() {
        case "html": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json"
        case "webmanifest": return "application/manifest+json"
        case "wasm": return "application/wasm"
        case "png": return "image/png"
        case "ttf": return "font/ttf"
        case "txt": return "text/plain; charset=utf-8"
        default: return "application/octet-stream"
        }
    }
}
