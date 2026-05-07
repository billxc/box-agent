import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct LogView: View {
    @State private var entries: [AppLog.LogEntry] = []
    @State private var filter = ""

    private var filtered: [AppLog.LogEntry] {
        if filter.isEmpty { return entries }
        return entries.filter { $0.message.localizedCaseInsensitiveContains(filter) || $0.level.localizedCaseInsensitiveContains(filter) }
    }

    var body: some View {
        List {
            ForEach(filtered.reversed()) { entry in
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(entry.level)
                            .font(.caption2.weight(.bold).monospaced())
                            .foregroundStyle(entry.level == "ERROR" ? .red : .secondary)
                        Spacer()
                        Text(entry.timestamp, format: .dateTime.hour().minute().second())
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    Text(entry.message)
                        .font(.caption.monospaced())
                        .foregroundStyle(.primary)
                        .lineLimit(5)
                }
                .padding(.vertical, 2)
            }
        }
        .searchable(text: $filter, prompt: "Filter logs")
        .navigationTitle("Logs")
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Menu {
                    Button {
                        let text = AppLog.shared.exportText()
                        #if os(macOS)
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(text, forType: .string)
                        #else
                        UIPasteboard.general.string = text
                        #endif
                    } label: {
                        Label("Copy All", systemImage: "doc.on.doc")
                    }
                    Button(role: .destructive) {
                        AppLog.shared.clear()
                        entries = []
                    } label: {
                        Label("Clear", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
            }
        }
        .onAppear {
            entries = AppLog.shared.getEntries()
        }
    }
}
