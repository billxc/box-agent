import SwiftUI
import DequeModule

enum HistoryLoadState {
    case readyForLoad
    case loading
    case allLoaded
}

@MainActor @Observable
final class ChatViewModel {
    let bot: String
    let machine: String
    let chatId: String
    private let api: APIClient

    var messages: Deque<ChatMessage> = []
    var isConnected = false
    var isSending = false
    var isTyping = false
    var historyLoaded = false
    var loadState: HistoryLoadState = .readyForLoad
    var pendingAnchorId: String?
    private var totalMessages = 0
    private var loadedOffset = 0
    private let pageSize = 50

    private var sseClient: SSEClient?
    private var streamTask: Task<Void, Never>?
    private var msgCounter = 0

    init(bot: String, machine: String, chatId: String, api: APIClient) {
        self.bot = bot
        self.machine = machine
        self.chatId = chatId
        self.api = api
    }

    func loadHistory() async {
        do {
            let (entries, total) = try await api.fetchHistory(bot: bot, machine: machine, chatId: chatId, limit: pageSize, offset: 0)
            totalMessages = total
            loadedOffset = entries.count
            loadState = loadedOffset < totalMessages ? .readyForLoad : .allLoaded
            messages = Self.mapEntries(entries)
        } catch {
            AppLog.shared.error("loadHistory: \(error)")
        }
        historyLoaded = true
    }

    func loadMoreHistory() async {
        guard loadState == .readyForLoad else {
            AppLog.shared.info("loadMore skipped: state=\(loadState)")
            return
        }
        loadState = .loading
        let anchorId = messages.first?.id
        AppLog.shared.info("loadMore: offset=\(loadedOffset) total=\(totalMessages)")
        defer {
            // Safety net: if anything goes wrong without explicit handling, reset state
            if loadState == .loading {
                loadState = .readyForLoad
            }
        }
        do {
            let (entries, _) = try await withTimeout(seconds: 30) {
                try await self.api.fetchHistory(bot: self.bot, machine: self.machine, chatId: self.chatId, limit: self.pageSize, offset: self.loadedOffset)
            }
            AppLog.shared.info("loadMore: got \(entries.count) entries")
            let older = Self.mapEntries(entries)
            messages.prepend(contentsOf: older)
            loadedOffset += entries.count
            loadState = loadedOffset < totalMessages ? .readyForLoad : .allLoaded
            pendingAnchorId = anchorId
        } catch {
            AppLog.shared.error("loadMoreHistory: \(error)")
            loadState = .readyForLoad
        }
    }

    private static func mapEntries(_ entries: [HistoryEntry]) -> Deque<ChatMessage> {
        var out: Deque<ChatMessage> = []
        out.reserveCapacity(entries.count)
        for (idx, entry) in entries.enumerated() {
            guard let role = MessageRole(rawValue: entry.role) else { continue }
            let ts = entry.ts.map { Date(timeIntervalSince1970: $0) } ?? .now
            switch role {
            case .user, .assistant, .skillOutput:
                out.append(ChatMessage(
                    id: "\(entry.ts ?? 0)-\(entry.role)-\(idx)",
                    role: role, text: entry.text ?? "", isStreaming: false, timestamp: ts
                ))
            case .toolCall:
                out.append(ChatMessage(
                    id: entry.toolId ?? "tc-\(idx)-\(UUID().uuidString)",
                    role: .toolCall, text: "", isStreaming: false, timestamp: ts,
                    toolId: entry.toolId, toolName: entry.name
                ))
            case .toolResult:
                out.append(ChatMessage(
                    id: "result-\(entry.toolId ?? "tr-\(idx)-\(UUID().uuidString)")",
                    role: .toolResult, text: "", isStreaming: false, timestamp: ts,
                    toolId: entry.toolId, toolOk: entry.ok,
                    toolSummary: entry.summary, toolError: entry.error
                ))
            }
        }
        return out
    }

    func connect() {
        guard sseClient == nil else { return }
        guard let url = api.sseURL(bot: bot, machine: machine, chatId: chatId) else { return }
        sseClient = SSEClient(url: url, tunnelToken: api.tunnelToken)
        isConnected = true
        streamTask = Task { @MainActor [weak self] in
            guard let self, let client = self.sseClient else { return }
            for await event in client.stream() {
                self.handleSSE(event)
            }
            self.isConnected = false
            self.sseClient = nil
        }
    }

    func disconnect() {
        sseClient?.cancel()
        sseClient = nil
        streamTask?.cancel()
        streamTask = nil
        isConnected = false
    }

    func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        isSending = true
        do {
            try await api.sendMessage(bot: bot, machine: machine, chatId: chatId, text: trimmed)
        } catch {
            // message send failed
        }
        isSending = false
    }

    private func handleSSE(_ event: SSEEvent) {
        let ts = event.timestamp.map { Date(timeIntervalSince1970: $0) } ?? .now
        switch event.type {
        case .message:
            isTyping = false
            guard let role = event.role.flatMap({ MessageRole(rawValue: $0) }) else { return }
            if role == .user, let last = messages.last, last.role == .user, last.text == (event.text ?? "") {
                return
            }
            messages.append(ChatMessage(
                id: event.messageId ?? nextId(), role: role,
                text: event.text ?? "", isStreaming: false, timestamp: ts
            ))
        case .typing:
            isTyping = true
        case .streamStart:
            isTyping = false
            messages.append(ChatMessage(
                id: event.messageId ?? nextId(), role: .assistant,
                text: "", isStreaming: true, timestamp: ts
            ))
        case .streamDelta:
            // Locate by message_id — relying on "last streaming message" breaks
            // once a toolCall (also isStreaming=true) lands between deltas.
            let targetId = event.messageId
            if let id = targetId, let idx = messages.lastIndex(where: { $0.id == id && $0.role == .assistant }) {
                messages[idx].text = event.text ?? (messages[idx].text + (event.delta ?? ""))
            } else if let idx = messages.lastIndex(where: { $0.role == .assistant && $0.isStreaming }) {
                messages[idx].text = event.text ?? (messages[idx].text + (event.delta ?? ""))
            }
        case .streamEnd:
            isTyping = false
            let targetId = event.messageId
            if let id = targetId, let idx = messages.lastIndex(where: { $0.id == id && $0.role == .assistant }) {
                messages[idx].text = event.text ?? messages[idx].text
                messages[idx].isStreaming = false
            } else if let idx = messages.lastIndex(where: { $0.role == .assistant && $0.isStreaming }) {
                messages[idx].text = event.text ?? messages[idx].text
                messages[idx].isStreaming = false
            }
        case .toolCall:
            messages.append(ChatMessage(
                id: event.toolId ?? nextId(), role: .toolCall,
                text: "", isStreaming: true, timestamp: ts,
                toolId: event.toolId, toolName: event.toolName,
                toolArgsJSON: event.toolArgsJSON,
                parentToolId: event.parentToolId
            ))
        case .toolResult:
            if let idx = messages.lastIndex(where: { $0.toolId == event.toolId && $0.role == .toolCall }) {
                messages[idx].toolOk = event.toolOk
                messages[idx].toolSummary = event.toolSummary
                messages[idx].toolError = event.toolError
                messages[idx].isStreaming = false
            } else {
                messages.append(ChatMessage(
                    id: "result-\(event.toolId ?? nextId())", role: .toolResult,
                    text: "", isStreaming: false, timestamp: ts,
                    toolId: event.toolId,
                    parentToolId: event.parentToolId,
                    toolOk: event.toolOk,
                    toolSummary: event.toolSummary, toolError: event.toolError
                ))
            }
        case .close:
            disconnect()
        }
    }

    private func nextId() -> String {
        msgCounter += 1
        return "local-\(msgCounter)"
    }
}

struct TimeoutError: Error {}

func withTimeout<T: Sendable>(seconds: Int, operation: @Sendable @escaping () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask {
            try await operation()
        }
        group.addTask {
            try await Task.sleep(for: .seconds(seconds))
            throw TimeoutError()
        }
        let result = try await group.next()!
        group.cancelAll()
        return result
    }
}
