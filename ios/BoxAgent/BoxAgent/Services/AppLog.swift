import Foundation
import os

final class AppLog: @unchecked Sendable {
    static let shared = AppLog()

    private var entries: [LogEntry] = []
    private let lock = NSLock()
    private let maxEntries = 200

    struct LogEntry: Identifiable {
        let id = UUID()
        let timestamp: Date
        let level: String
        let message: String
    }

    func log(_ level: String, _ message: String) {
        let entry = LogEntry(timestamp: .now, level: level, message: message)
        lock.lock()
        entries.append(entry)
        if entries.count > maxEntries {
            entries.removeFirst(entries.count - maxEntries)
        }
        lock.unlock()
        Logger(subsystem: "com.boxagent.app", category: "App").log("\(level): \(message)")
    }

    func info(_ message: String) { log("INFO", message) }
    func error(_ message: String) { log("ERROR", message) }

    func getEntries() -> [LogEntry] {
        lock.lock()
        defer { lock.unlock() }
        return entries
    }

    func clear() {
        lock.lock()
        entries.removeAll()
        lock.unlock()
    }

    func exportText() -> String {
        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm:ss.SSS"
        return getEntries().map { "\(fmt.string(from: $0.timestamp)) [\($0.level)] \($0.message)" }.joined(separator: "\n")
    }
}
