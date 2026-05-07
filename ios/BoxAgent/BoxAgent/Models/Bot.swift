import Foundation

struct Machine: Identifiable, Codable {
    var id: String { machineId }
    let machineId: String
    let online: Bool
    let role: String
    let isSelf: Bool
    let hostIndex: Int?
    let bots: [Bot]
    let lastSeen: Double?

    enum CodingKeys: String, CodingKey {
        case machineId = "machine_id"
        case online, role
        case isSelf = "self"
        case hostIndex = "host_index"
        case bots
        case lastSeen = "last_seen"
    }
}

struct Bot: Identifiable, Codable {
    var id: String { name }
    let name: String
    let displayName: String
    let backend: String
    let model: String?
    let kind: String

    enum CodingKeys: String, CodingKey {
        case name
        case displayName = "display_name"
        case backend, model, kind
    }
}

struct Session: Identifiable, Codable {
    var id: String { chatId }
    let chatId: String
    let sessionId: String?
    let platform: String
    let isMain: Bool
    let preview: String?
    let lastTs: Double?
    let backend: String?
    let model: String?

    enum CodingKeys: String, CodingKey {
        case chatId = "chat_id"
        case sessionId = "session_id"
        case platform
        case isMain = "is_main"
        case preview
        case lastTs = "last_ts"
        case backend, model
    }
}

struct MachinesResponse: Codable {
    let machines: [Machine]
}

struct SessionsResponse: Codable {
    let ok: Bool
    let sessions: [Session]
}

struct RecentSession: Identifiable {
    var id: String { "\(machineId)-\(botName)-\(session.chatId)" }
    let session: Session
    let botName: String
    let botDisplayName: String
    let machineId: String
    let backend: String
}

struct HistoryResponse: Codable {
    let ok: Bool
    let history: [HistoryEntry]
}

struct HistoryEntry: Codable {
    let role: String
    let text: String?
    let ts: Double?
    let toolId: String?
    let name: String?
    let ok: Bool?
    let summary: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case role, text, ts
        case toolId = "tool_id"
        case name, ok, summary, error
    }
}

struct VersionResponse: Codable {
    let ok: Bool
    let machineId: String?
    let version: String?
    let versionString: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case machineId = "machine_id"
        case version
        case versionString = "version_string"
    }
}

struct ClaudeProject: Identifiable, Codable {
    var id: String { encoded }
    let encoded: String
    let label: String
    let cwd: String?
    let sessionCount: Int?
    let lastTs: Double?

    enum CodingKeys: String, CodingKey {
        case encoded, label, cwd
        case sessionCount = "session_count"
        case lastTs = "last_ts"
    }
}

struct ClaudeSession: Identifiable, Codable {
    var id: String { sessionId }
    let sessionId: String
    let firstUser: String?
    let messageCount: Int?
    let lastTs: Double?

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case firstUser = "first_user"
        case messageCount = "message_count"
        case lastTs = "last_ts"
    }
}

struct ClaudeProjectsResponse: Codable {
    let ok: Bool
    let projects: [ClaudeProject]
}

struct ClaudeSessionsResponse: Codable {
    let ok: Bool
    let sessions: [ClaudeSession]
}

struct ClaudeResumeResponse: Codable {
    let ok: Bool
    let chatId: String?
    let sessionId: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case chatId = "chat_id"
        case sessionId = "session_id"
    }
}
