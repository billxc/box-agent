import SwiftUI

struct ContinueView: View {
    @Environment(BotsViewModel.self) private var botsVM
    @State private var entries: [RecentEntry] = []

    var body: some View {
        List {
            if entries.isEmpty {
                ContentUnavailableView("No History", systemImage: "clock.arrow.circlepath", description: Text("Sessions you open on this device will appear here"))
            } else {
                ForEach(entries) { entry in
                    NavigationLink {
                        chatView(for: entry)
                    } label: {
                        ContinueRow(entry: entry)
                    }
                }
                .onDelete { indexSet in
                    var all = RecentEntry.load()
                    let idsToRemove = indexSet.map { entries[$0].id }
                    all.removeAll { idsToRemove.contains($0.id) }
                    RecentEntry.save(all)
                    entries = all
                }
            }
        }
        .navigationTitle("Continue")
        .onAppear {
            entries = RecentEntry.load()
        }
    }

    private func chatView(for entry: RecentEntry) -> some View {
        let api = botsVM.makeAPIClient()!
        let vm = ChatViewModel(
            bot: entry.botName,
            machine: entry.machineId,
            chatId: entry.chatId,
            api: api
        )
        return ChatView(
            viewModel: vm,
            botDisplayName: entry.botDisplayName,
            initialRecap: entry.recap ?? "",
            initialSummary: entry.summary ?? "",
            initialCustomTitle: entry.customTitle ?? ""
        )
    }
}

struct ContinueRow: View {
    let entry: RecentEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(displayTitle)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Spacer()
                Text(entry.machineId)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Text(entry.botDisplayName)
                .font(.caption)
                .foregroundStyle(.secondary)

            if !entry.preview.isEmpty {
                Text(entry.preview)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            } else {
                Text(entry.chatId)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            if let recap = entry.recap, !recap.isEmpty {
                Label(recap, systemImage: "pin.fill")
                    .font(.caption)
                    .foregroundStyle(Color.accentColor)
                    .lineLimit(2)
            }

            Text(Date(timeIntervalSince1970: entry.lastAccessed), style: .relative)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .padding(.vertical, 4)
    }

    private var displayTitle: String {
        if let title = entry.customTitle, !title.isEmpty { return title }
        if let summary = entry.summary, !summary.isEmpty { return summary }
        return entry.botDisplayName
    }
}
