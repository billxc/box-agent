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
    var summary: String?
    var customTitle: String?
    var recap: String?

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

    static func record(
        chatId: String,
        botName: String,
        botDisplayName: String,
        machineId: String,
        backend: String,
        preview: String = "",
        summary: String? = nil,
        customTitle: String? = nil,
        recap: String? = nil
    ) {
        var entries = load()
        let now = Date().timeIntervalSince1970
        if let idx = entries.firstIndex(where: { $0.chatId == chatId && $0.botName == botName && $0.machineId == machineId }) {
            var existing = entries[idx]
            existing.lastAccessed = now
            if !preview.isEmpty { existing = RecentEntry(chatId: chatId, botName: botName, botDisplayName: botDisplayName, machineId: machineId, backend: backend, preview: preview, lastAccessed: now, summary: summary ?? existing.summary, customTitle: customTitle ?? existing.customTitle, recap: recap ?? existing.recap) }
            else {
                if summary != nil { existing.summary = summary }
                if customTitle != nil { existing.customTitle = customTitle }
                if recap != nil { existing.recap = recap }
            }
            entries[idx] = existing
        } else {
            entries.insert(RecentEntry(chatId: chatId, botName: botName, botDisplayName: botDisplayName, machineId: machineId, backend: backend, preview: preview, lastAccessed: now, summary: summary, customTitle: customTitle, recap: recap), at: 0)
        }
        save(entries.sorted { $0.lastAccessed > $1.lastAccessed })
    }
}
