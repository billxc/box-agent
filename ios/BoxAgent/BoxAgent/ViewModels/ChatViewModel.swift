import SwiftUI

@MainActor @Observable
final class ChatViewModel {
    let bot: String
    let machine: String
    let chatId: String
    private let api: APIClient

    var messages: [ChatMessage] = []
    var isConnected = false
    var isSending = false
    var isTyping = false
    var historyLoaded = false

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
        let entries = (try? await api.fetchHistory(bot: bot, machine: machine, chatId: chatId)) ?? []
        messages = entries.compactMap { entry in
            guard let role = MessageRole(rawValue: entry.role) else { return nil }
            let ts = entry.ts.map { Date(timeIntervalSince1970: $0) } ?? .now
            switch role {
            case .user, .assistant, .skillOutput:
                return ChatMessage(
                    id: "\(entry.ts ?? 0)-\(entry.role)",
                    role: role, text: entry.text ?? "", isStreaming: false, timestamp: ts
                )
            case .toolCall:
                return ChatMessage(
                    id: entry.toolId ?? UUID().uuidString,
                    role: .toolCall, text: "", isStreaming: false, timestamp: ts,
                    toolId: entry.toolId, toolName: entry.name
                )
            case .toolResult:
                return ChatMessage(
                    id: "result-\(entry.toolId ?? UUID().uuidString)",
                    role: .toolResult, text: "", isStreaming: false, timestamp: ts,
                    toolId: entry.toolId, toolOk: entry.ok,
                    toolSummary: entry.summary, toolError: entry.error
                )
            }
        }
        historyLoaded = true
    }

    func connect() {
        guard let url = api.sseURL(bot: bot, machine: machine, chatId: chatId) else { return }
        sseClient = SSEClient(url: url, tunnelToken: api.tunnelToken)
        isConnected = true
        streamTask = Task { @MainActor [weak self] in
            guard let self, let client = self.sseClient else { return }
            for await event in client.stream() {
                self.handleSSE(event)
            }
            self.isConnected = false
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
            if let idx = messages.indices.last, messages[idx].isStreaming {
                messages[idx].text = event.text ?? (messages[idx].text + (event.delta ?? ""))
            }
        case .streamEnd:
            isTyping = false
            if let idx = messages.indices.last, messages[idx].isStreaming {
                messages[idx].text = event.text ?? messages[idx].text
                messages[idx].isStreaming = false
            }
        case .toolCall:
            messages.append(ChatMessage(
                id: event.toolId ?? nextId(), role: .toolCall,
                text: "", isStreaming: true, timestamp: ts,
                toolId: event.toolId, toolName: event.toolName,
                toolArgsJSON: event.toolArgsJSON
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
                    toolId: event.toolId, toolOk: event.toolOk,
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
