import SwiftUI

struct RecentsView: View {
    @Environment(BotsViewModel.self) private var botsVM
    @State private var recents: [RecentSession] = []
    @State private var isLoading = true

    var body: some View {
        List {
            if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            } else if recents.isEmpty {
                ContentUnavailableView("No Sessions", systemImage: "clock", description: Text("Start a conversation from the Agents tab"))
            } else {
                ForEach(recents.prefix(20)) { recent in
                    NavigationLink {
                        chatView(for: recent)
                    } label: {
                        ServerRecentRow(recent: recent)
                    }
                }
            }
        }
        .navigationTitle("Recents")
        .task {
            recents = await botsVM.fetchRecentSessions()
            isLoading = false
        }
        .refreshable {
            recents = await botsVM.fetchRecentSessions()
        }
    }

    private func chatView(for recent: RecentSession) -> some View {
        let api = botsVM.makeAPIClient()!
        let vm = ChatViewModel(
            bot: recent.botName,
            machine: recent.machineId,
            chatId: recent.session.chatId,
            api: api
        )
        return ChatView(viewModel: vm, botDisplayName: recent.botDisplayName)
    }
}

struct ServerRecentRow: View {
    let recent: RecentSession

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(recent.botDisplayName)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Text(recent.machineId)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let preview = recent.session.preview, !preview.isEmpty {
                Text(preview)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            HStack(spacing: 8) {
                Label(recent.session.platform, systemImage: platformIcon(recent.session.platform))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)

                if let ts = recent.session.lastTs {
                    Text(Date(timeIntervalSince1970: ts), style: .relative)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                if recent.session.isMain {
                    Image(systemName: "star.fill")
                        .font(.caption2)
                        .foregroundStyle(.yellow)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func platformIcon(_ platform: String) -> String {
        switch platform {
        case "web": return "globe"
        case "telegram": return "paperplane"
        case "discord": return "bubble.left.and.bubble.right"
        case "claude": return "brain.head.profile"
        default: return "questionmark.circle"
        }
    }
}
