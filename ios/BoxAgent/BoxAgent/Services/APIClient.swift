import Foundation

private let log = AppLog.shared

actor APIClient {
    let baseURL: String
    let token: String

    init(baseURL: String, token: String) {
        self.baseURL = baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        self.token = token
    }

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: "\(baseURL)\(path)") else {
            throw APIError.invalidURL
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if !token.isEmpty {
            req.setValue("tunnel \(token)", forHTTPHeaderField: "X-Tunnel-Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        log.info("\(method) \(path)")
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.httpError(0)
        }
        if !(200..<300 ~= http.statusCode) {
            let body = String(data: data, encoding: .utf8) ?? "(binary)"
            log.error("\(method) \(path) → \(http.statusCode): \(body)")
            throw APIError.httpError(http.statusCode)
        }
        return data
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data, context: String) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            let body = String(data: data, encoding: .utf8) ?? "(binary)"
            log.error("Decode \(context) failed: \(error)\nBody: \(body)")
            throw error
        }
    }

    func fetchMachines() async throws -> [Machine] {
        let data = try await request("/api/machines")
        return try decode(MachinesResponse.self, from: data, context: "machines").machines
    }

    func fetchSessions(bot: String, machine: String) async throws -> [Session] {
        let data = try await request("/api/sessions?bot=\(bot)&machine=\(machine)")
        return try decode(SessionsResponse.self, from: data, context: "sessions").sessions
    }

    func fetchHistory(bot: String, machine: String, chatId: String, limit: Int = 0, offset: Int = 0) async throws -> (entries: [HistoryEntry], total: Int) {
        let encoded = chatId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? chatId
        var path = "/api/history?bot=\(bot)&machine=\(machine)&chat_id=\(encoded)"
        if limit > 0 {
            path += "&limit=\(limit)&offset=\(offset)"
        }
        let data = try await request(path)
        let resp = try decode(HistoryResponse.self, from: data, context: "history")
        return (resp.history, resp.total ?? resp.history.count)
    }

    func sendMessage(bot: String, machine: String, chatId: String, text: String) async throws {
        let body: [String: Any] = ["bot": bot, "machine": machine, "chat_id": chatId, "text": text]
        _ = try await request("/api/send", method: "POST", body: body)
    }

    func fetchVersion() async throws -> VersionResponse {
        let data = try await request("/api/version")
        return try decode(VersionResponse.self, from: data, context: "version")
    }

    func fetchClaudeProjects(machine: String) async throws -> [ClaudeProject] {
        let data = try await request("/api/claude/projects?machine=\(machine)")
        return try decode(ClaudeProjectsResponse.self, from: data, context: "claude/projects").projects
    }

    func fetchClaudeSessions(machine: String, project: String) async throws -> [ClaudeSession] {
        let encoded = project.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? project
        let data = try await request("/api/claude/sessions?machine=\(machine)&project=\(encoded)")
        return try decode(ClaudeSessionsResponse.self, from: data, context: "claude/sessions").sessions
    }

    func resumeClaudeSession(bot: String, machine: String, sessionId: String, project: String, backend: String = "") async throws -> ClaudeResumeResponse {
        var body: [String: Any] = ["bot": bot, "machine": machine, "session_id": sessionId, "project": project]
        if !backend.isEmpty {
            body["backend"] = backend
        }
        let data = try await request("/api/claude/resume", method: "POST", body: body)
        return try decode(ClaudeResumeResponse.self, from: data, context: "claude/resume")
    }

    nonisolated func sseURL(bot: String, machine: String, chatId: String) -> URL? {
        let encoded = chatId.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? chatId
        return URL(string: "\(baseURL)/api/stream?bot=\(bot)&machine=\(machine)&chat_id=\(encoded)")
    }

    nonisolated var tunnelToken: String { token }
}

enum APIError: LocalizedError {
    case invalidURL
    case httpError(Int)

    var errorDescription: String? {
        switch self {
        case .invalidURL: "Invalid server URL"
        case .httpError(let code): "Server returned \(code)"
        }
    }
}
