import SwiftUI

struct ToolCallView: View {
    let message: ChatMessage

    var body: some View {
        if message.role == .toolResult {
            toolResultCard
        } else if message.toolOk != nil {
            completedToolCallCard
        } else {
            pendingToolCallCard
        }
    }

    private var pendingToolCallCard: some View {
        HStack(spacing: 8) {
            Image(systemName: "wrench.and.screwdriver")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(message.toolName ?? "tool")
                    .font(.caption.weight(.semibold).monospaced())
                if let args = message.toolArgsJSON {
                    Text(args.prefix(80))
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if message.isStreaming {
                ProgressView()
                    .controlSize(.mini)
            }
        }
        .padding(10)
        .glassEffect(.regular, in: .rect(cornerRadius: 12))
        .tint(.orange)
        .padding(.horizontal, 20)
    }

    private var completedToolCallCard: some View {
        let ok = message.toolOk ?? true
        return HStack(spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(ok ? .green : .red)
            VStack(alignment: .leading, spacing: 2) {
                Text(message.toolName ?? "tool")
                    .font(.caption.weight(.semibold).monospaced())
                if let summary = message.toolSummary ?? message.toolError {
                    Text(summary.prefix(120))
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
            }
            Spacer()
        }
        .padding(10)
        .glassEffect(.regular, in: .rect(cornerRadius: 12))
        .tint(ok ? .green : .red)
        .padding(.horizontal, 20)
    }

    private var toolResultCard: some View {
        HStack(spacing: 8) {
            Image(systemName: (message.toolOk ?? true) ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle((message.toolOk ?? true) ? .green : .red)
            if let summary = message.toolSummary ?? message.toolError {
                Text(summary.prefix(120))
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
            Spacer()
        }
        .padding(10)
        .glassEffect(.regular, in: .rect(cornerRadius: 12))
        .tint((message.toolOk ?? true) ? .green : .red)
        .padding(.horizontal, 20)
    }
}

struct SkillOutputCard: View {
    let text: String
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "text.book.closed")
                        .foregroundStyle(.purple)
                    Text("Skill Output")
                        .font(.caption.weight(.semibold))
                    Spacer()
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.plain)

            if expanded {
                Text(text)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(20)
                    .padding(.top, 6)
            }
        }
        .padding(10)
        .glassEffect(.regular, in: .rect(cornerRadius: 12))
        .tint(.purple)
        .padding(.horizontal, 20)
    }
}
