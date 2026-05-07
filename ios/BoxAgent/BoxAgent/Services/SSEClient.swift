import Foundation

final class SSEClient: @unchecked Sendable {
    private let url: URL
    private let tunnelToken: String
    private var task: Task<Void, Never>?

    init(url: URL, tunnelToken: String = "") {
        self.url = url
        self.tunnelToken = tunnelToken
    }

    func stream() -> AsyncStream<SSEEvent> {
        AsyncStream { continuation in
            task = Task {
                do {
                    var req = URLRequest(url: url)
                    req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    req.timeoutInterval = 300
                    if !tunnelToken.isEmpty {
                        req.setValue("tunnel \(tunnelToken)", forHTTPHeaderField: "X-Tunnel-Authorization")
                    }
                    let (bytes, _) = try await URLSession.shared.bytes(for: req)
                    AppLog.shared.info("SSE connected: \(url.path)")
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        guard line.hasPrefix("data: ") else { continue }
                        let json = String(line.dropFirst(6))
                        if let event = Self.parse(json) {
                            continuation.yield(event)
                            if event.type == .close { break }
                        }
                    }
                } catch {
                    if !Task.isCancelled {
                        AppLog.shared.error("SSE error: \(error)")
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { [weak self] _ in
                self?.task?.cancel()
            }
        }
    }

    func cancel() {
        task?.cancel()
    }

    private static func parse(_ json: String) -> SSEEvent? {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let typeStr = obj["type"] as? String,
              let type = SSEEventType(rawValue: typeStr) else {
            return nil
        }
        var argsJSON: String?
        if let args = obj["args"] {
            if let argsData = try? JSONSerialization.data(withJSONObject: args) {
                argsJSON = String(data: argsData, encoding: .utf8)
            }
        }
        return SSEEvent(
            type: type,
            messageId: obj["message_id"] as? String,
            role: obj["role"] as? String,
            text: obj["text"] as? String,
            delta: obj["delta"] as? String,
            toolId: obj["tool_id"] as? String,
            toolName: obj["name"] as? String,
            toolArgsJSON: argsJSON,
            toolOk: obj["ok"] as? Bool,
            toolSummary: obj["summary"] as? String,
            toolError: obj["error"] as? String,
            timestamp: obj["ts"] as? Double
        )
    }
}
