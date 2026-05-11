import Foundation

enum MessageRole: String, Codable {
    case user
    case assistant
    case toolCall = "tool_call"
    case toolResult = "tool_result"
    case skillOutput = "skill_output"
}

struct ChatMessage: Identifiable {
    let id: String
    var role: MessageRole
    var text: String
    var isStreaming: Bool
    var timestamp: Date

    var toolId: String?
    var toolName: String?
    var toolArgsJSON: String?
    var parentToolId: String?

    var toolOk: Bool?
    var toolSummary: String?
    var toolError: String?
}

enum SSEEventType: String {
    case message
    case typing
    case streamStart = "stream_start"
    case streamDelta = "stream_delta"
    case streamEnd = "stream_end"
    case toolCall = "tool_call"
    case toolResult = "tool_result"
    case close = "_close"
}

struct SSEEvent: Sendable {
    let type: SSEEventType
    let messageId: String?
    let role: String?
    let text: String?
    let delta: String?
    let toolId: String?
    let toolName: String?
    let toolArgsJSON: String?
    let toolOk: Bool?
    let toolSummary: String?
    let toolError: String?
    let parentToolId: String?
    let timestamp: Double?
}
