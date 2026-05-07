import Foundation

struct RecentEntry: Codable, Identifiable {
    var id: String { "\(machineId)-\(botName)-\(chatId)" }
    let chatId: String
    let botName: String
    let botDisplayName: String
    let machineId: String
    let backend: String
    let preview: String
    var lastAccessed: Double

    static func load() -> [RecentEntry] {
        guard let data = UserDefaults.standard.data(forKey: "recentSessions"),
              let entries = try? JSONDecoder().decode([RecentEntry].self, from: data) else {
            return []
        }
        return entries.sorted { $0.lastAccessed > $1.lastAccessed }
    }

    static func save(_ entries: [RecentEntry]) {
        let trimmed = Array(entries.prefix(50))
        if let data = try? JSONEncoder().encode(trimmed) {
            UserDefaults.standard.set(data, forKey: "recentSessions")
        }
    }

    static func record(chatId: String, botName: String, botDisplayName: String, machineId: String, backend: String, preview: String = "") {
        var entries = load()
        if let idx = entries.firstIndex(where: { $0.chatId == chatId && $0.botName == botName && $0.machineId == machineId }) {
            entries[idx].lastAccessed = Date().timeIntervalSince1970
            if !preview.isEmpty { entries[idx] = RecentEntry(chatId: chatId, botName: botName, botDisplayName: botDisplayName, machineId: machineId, backend: backend, preview: preview, lastAccessed: Date().timeIntervalSince1970) }
        } else {
            entries.insert(RecentEntry(chatId: chatId, botName: botName, botDisplayName: botDisplayName, machineId: machineId, backend: backend, preview: preview, lastAccessed: Date().timeIntervalSince1970), at: 0)
        }
        save(entries.sorted { $0.lastAccessed > $1.lastAccessed })
    }
}
