import SwiftUI

struct ClaudeResumeView: View {
    let bot: Bot
    let machine: Machine
    @Environment(BotsViewModel.self) private var botsVM
    @State private var projects: [ClaudeProject] = []
    @State private var isLoading = true
    @State private var error: String?

    var body: some View {
        List {
            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            } else if let error {
                ContentUnavailableView("Error", systemImage: "exclamationmark.triangle", description: Text(error))
            } else if projects.isEmpty {
                ContentUnavailableView("No Projects", systemImage: "folder", description: Text("No Claude projects found"))
            } else {
                ForEach(projects) { project in
                    NavigationLink {
                        ClaudeSessionPickerView(bot: bot, machine: machine, project: project)
                    } label: {
                        HStack {
                            Image(systemName: "folder")
                                .foregroundStyle(.tint)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(project.label)
                                    .font(.body)
                                if let count = project.sessionCount {
                                    Text("\(count) sessions")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Resume Session")
        .task {
            guard let api = botsVM.makeAPIClient() else { return }
            do {
                projects = try await api.fetchClaudeProjects(machine: machine.machineId)
            } catch {
                self.error = error.localizedDescription
            }
            isLoading = false
        }
    }
}

struct ClaudeSessionPickerView: View {
    let bot: Bot
    let machine: Machine
    let project: ClaudeProject
    @Environment(BotsViewModel.self) private var botsVM
    @State private var sessions: [ClaudeSession] = []
    @State private var isLoading = true
    @State private var resumedChatId: String?
    @State private var error: String?

    var body: some View {
        List {
            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            } else if sessions.isEmpty {
                ContentUnavailableView("No Sessions", systemImage: "bubble.left", description: Text("No sessions in this project"))
            } else {
                ForEach(sessions) { session in
                    Button {
                        Task { await resumeSession(session) }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                if let firstUser = session.firstUser, !firstUser.isEmpty {
                                    Text(firstUser)
                                        .font(.subheadline)
                                        .lineLimit(2)
                                } else {
                                    Text(session.sessionId.prefix(12) + "…")
                                        .font(.subheadline.monospaced())
                                }
                                if let ts = session.lastTs {
                                    Text(Date(timeIntervalSince1970: ts), style: .relative)
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            Spacer()
                            if let count = session.messageCount {
                                Text("\(count) msgs")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .navigationTitle(project.label)
        .navigationDestination(item: $resumedChatId) { chatId in
            let api = botsVM.makeAPIClient()!
            let vm = ChatViewModel(bot: bot.name, machine: machine.machineId, chatId: chatId, api: api)
            ChatView(viewModel: vm, botDisplayName: bot.displayName)
        }
        .task {
            guard let api = botsVM.makeAPIClient() else { return }
            do {
                sessions = try await api.fetchClaudeSessions(machine: machine.machineId, project: project.encoded)
            } catch {
                self.error = error.localizedDescription
            }
            isLoading = false
        }
    }

    private func resumeSession(_ session: ClaudeSession) async {
        guard let api = botsVM.makeAPIClient() else { return }
        do {
            let resp = try await api.resumeClaudeSession(
                bot: "raw", machine: machine.machineId,
                sessionId: session.sessionId, project: project.encoded,
                backend: bot.backend
            )
            if resp.ok, let chatId = resp.chatId {
                resumedChatId = chatId
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}
