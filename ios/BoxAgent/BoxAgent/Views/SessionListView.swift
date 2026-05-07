import SwiftUI

struct SessionListView: View {
    let bot: Bot
    let machine: Machine
    @Environment(BotsViewModel.self) private var botsVM
    @State private var sessions: [Session] = []
    @State private var isLoading = true

    var body: some View {
        List {
            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            } else {
                newSessionLink
                if bot.name == "raw" {
                    resumeSessionLink
                }
                ForEach(sessions) { session in
                    NavigationLink {
                        chatView(for: session.chatId)
                    } label: {
                        SessionRow(session: session)
                    }
                }
            }
        }
        .navigationTitle(bot.displayName)
        .task {
            sessions = await botsVM.fetchSessions(bot: bot.name, machine: machine.machineId)
            isLoading = false
        }
        .refreshable {
            sessions = await botsVM.fetchSessions(bot: bot.name, machine: machine.machineId)
        }
    }

    private var newSessionLink: some View {
        NavigationLink {
            chatView(for: "web-\(bot.name)-\(Int(Date().timeIntervalSince1970))")
        } label: {
            HStack {
                Image(systemName: "plus.bubble")
                    .font(.title3)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text("New Session")
                        .font(.body.weight(.medium))
                    Text("Start a new conversation")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.vertical, 8)
        }
    }

    private var resumeSessionLink: some View {
        NavigationLink {
            ClaudeResumeView(bot: bot, machine: machine)
        } label: {
            HStack {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.title3)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Resume Claude Session")
                        .font(.body.weight(.medium))
                    Text("Continue a previous Claude CLI session")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 8)
        }
    }

    private func chatView(for chatId: String) -> some View {
        let api = botsVM.makeAPIClient()!
        let vm = ChatViewModel(bot: bot.name, machine: machine.machineId, chatId: chatId, api: api)
        return ChatView(viewModel: vm, botDisplayName: bot.displayName)
    }
}

struct SessionRow: View {
    let session: Session

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(session.chatId)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                Spacer()
                if session.isMain {
                    Image(systemName: "star.fill")
                        .font(.caption)
                        .foregroundStyle(.yellow)
                }
            }
            if let preview = session.preview, !preview.isEmpty {
                Text(preview)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            HStack(spacing: 8) {
                if let ts = session.lastTs, ts > 0 {
                    Text(Date(timeIntervalSince1970: ts), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                if let count = session.messageCount, count > 0 {
                    Text("\(count) msgs")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}
