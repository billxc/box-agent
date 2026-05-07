import SwiftUI
import MarkdownUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct MessageBubble: View {
    let message: ChatMessage
    private var isUser: Bool { message.role == .user }
    @State private var showSelectSheet = false

    private var hasTable: Bool {
        message.text.contains("\n|") && message.text.contains("---")
    }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 60) }

            VStack(alignment: .leading, spacing: 4) {
                if !isUser && hasTable {
                    Markdown(message.text)
                        .textSelection(.enabled)
                        .markdownTheme(.gitHub)
                } else if isUser {
                    Text(message.text)
                        .textSelection(.enabled)
                        .font(.body)
                } else {
                    RichMarkdownView(text: message.text)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background {
                if isUser {
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(.tint.opacity(0.15))
                } else {
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(.ultraThinMaterial)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture(count: 2) {
                showSelectSheet = true
            }
            .sheet(isPresented: $showSelectSheet) {
                SelectableTextView(text: message.text)
            }

            if !isUser { Spacer(minLength: 60) }
        }
    }
}

struct RichMarkdownView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(parseBlocks().enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let level, let content):
                    headingView(level: level, content: content)
                case .codeBlock(let code):
                    codeBlockView(code: code)
                case .text(let content):
                    inlineTextView(content: content)
                }
            }
        }
        .textSelection(.enabled)
    }

    private func headingView(level: Int, content: String) -> some View {
        Text(content)
            .font(level == 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
            .padding(.top, 4)
    }

    private func codeBlockView(code: String) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(code)
                .font(.caption.monospaced())
                .textSelection(.enabled)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(.quaternary))
    }

    @ViewBuilder
    private func inlineTextView(content: String) -> some View {
        if let attributed = try? AttributedString(
            markdown: content,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            Text(attributed)
                .font(.body)
        } else {
            Text(content)
                .font(.body)
        }
    }

    private enum Block {
        case heading(Int, String)
        case codeBlock(String)
        case text(String)
    }

    private func parseBlocks() -> [Block] {
        var blocks: [Block] = []
        var lines = text.components(separatedBy: "\n")
        var i = 0
        var textBuf: [String] = []

        func flushText() {
            let joined = textBuf.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty {
                blocks.append(.text(joined))
            }
            textBuf.removeAll()
        }

        while i < lines.count {
            let line = lines[i]

            if line.hasPrefix("```") {
                flushText()
                var codeLines: [String] = []
                i += 1
                while i < lines.count && !lines[i].hasPrefix("```") {
                    codeLines.append(lines[i])
                    i += 1
                }
                i += 1 // skip closing ```
                blocks.append(.codeBlock(codeLines.joined(separator: "\n")))
                continue
            }

            if let heading = parseHeading(line) {
                flushText()
                blocks.append(.heading(heading.0, heading.1))
                i += 1
                continue
            }

            textBuf.append(line)
            i += 1
        }
        flushText()
        return blocks
    }

    private func parseHeading(_ line: String) -> (Int, String)? {
        if line.hasPrefix("### ") { return (3, String(line.dropFirst(4))) }
        if line.hasPrefix("## ") { return (2, String(line.dropFirst(3))) }
        if line.hasPrefix("# ") { return (1, String(line.dropFirst(2))) }
        return nil
    }
}

struct SelectableTextView: View {
    let text: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            SelectableTextContent(text: text)
                .padding()
                .navigationTitle("Select Text")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                }
        }
    }
}

#if os(iOS)
struct SelectableTextContent: UIViewRepresentable {
    let text: String

    func makeUIView(context: Context) -> UITextView {
        let tv = UITextView()
        tv.isEditable = false
        tv.isSelectable = true
        tv.font = .preferredFont(forTextStyle: .body)
        tv.textColor = .label
        tv.backgroundColor = .clear
        tv.dataDetectorTypes = .link
        tv.text = text
        return tv
    }

    func updateUIView(_ uiView: UITextView, context: Context) {
        uiView.text = text
    }
}
#else
struct SelectableTextContent: NSViewRepresentable {
    let text: String

    func makeNSView(context: Context) -> NSScrollView {
        let scrollView = NSTextView.scrollableTextView()
        let tv = scrollView.documentView as! NSTextView
        tv.isEditable = false
        tv.isSelectable = true
        tv.font = .systemFont(ofSize: NSFont.systemFontSize)
        tv.string = text
        tv.backgroundColor = .clear
        return scrollView
    }

    func updateNSView(_ nsView: NSScrollView, context: Context) {
        let tv = nsView.documentView as! NSTextView
        tv.string = text
    }
}
#endif
