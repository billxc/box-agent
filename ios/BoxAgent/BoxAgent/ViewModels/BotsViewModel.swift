import SwiftUI

@MainActor @Observable
final class BotsViewModel {
    var machines: [Machine] = []
    var isLoading = false
    var error: String?

    var serverURL: String {
        get { UserDefaults.standard.string(forKey: "serverURL") ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: "serverURL") }
    }

    var token: String {
        get { UserDefaults.standard.string(forKey: "token") ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: "token") }
    }

    var isConfigured: Bool {
        !serverURL.isEmpty && !token.isEmpty
    }

    private var api: APIClient? {
        guard isConfigured else { return nil }
        return APIClient(baseURL: serverURL, token: token)
    }

    func loadMachines() async {
        guard let api else { return }
        isLoading = true
        error = nil
        do {
            machines = try await api.fetchMachines()
        } catch {
            self.error = String(describing: error)
        }
        isLoading = false
    }

    func fetchSessions(bot: String, machine: String) async -> [Session] {
        guard let api else { return [] }
        return (try? await api.fetchSessions(bot: bot, machine: machine)) ?? []
    }

    func fetchRecentSessions() async -> [RecentSession] {
        guard let api else { return [] }
        var results: [RecentSession] = []
        for machine in machines {
            for bot in machine.bots {
                let sessions = (try? await api.fetchSessions(bot: bot.name, machine: machine.machineId)) ?? []
                for session in sessions {
                    results.append(RecentSession(
                        session: session,
                        botName: bot.name,
                        botDisplayName: bot.displayName,
                        machineId: machine.machineId,
                        backend: bot.backend
                    ))
                }
            }
        }
        return results.sorted { ($0.session.lastTs ?? 0) > ($1.session.lastTs ?? 0) }
    }

    func makeAPIClient() -> APIClient? { api }
}
