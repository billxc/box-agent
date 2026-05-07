import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct ChatView: View {
    @State var viewModel: ChatViewModel
    var botDisplayName: String = ""
    @Environment(BotsViewModel.self) private var botsVM
    @AppStorage("showToolCalls") private var showToolCalls = true
    @State private var inputText = ""
    @FocusState private var inputFocused: Bool
    @State private var showInfo = false

    var body: some View {
        VStack(spacing: 0) {
            messageList
            composeBar
        }
        .navigationTitle(viewModel.chatId)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button {
                    showInfo.toggle()
                } label: {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(viewModel.isConnected ? .green : .red)
                            .frame(width: 6, height: 6)
                        Image(systemName: "info.circle")
                            .font(.caption)
                    }
                }
            }
        }
        .popover(isPresented: $showInfo) {
            SessionInfoView(
                machine: viewModel.machine,
                bot: viewModel.bot,
                chatId: viewModel.chatId,
                isConnected: viewModel.isConnected
            )
        }
        .task {
            await viewModel.loadHistory()
            viewModel.connect()
            RecentEntry.record(
                chatId: viewModel.chatId,
                botName: viewModel.bot,
                botDisplayName: botDisplayName.isEmpty ? viewModel.bot : botDisplayName,
                machineId: viewModel.machine,
                backend: ""
            )
        }
        .onDisappear {
            viewModel.disconnect()
        }
        #if os(iOS)
        .toolbar(.hidden, for: .tabBar)
        #endif
    }

    @State private var isAtBottom = true
    @State private var hasNewMessages = false

    private var messageList: some View {
        ScrollViewReader { proxy in
            ZStack(alignment: .bottomTrailing) {
                scrollArea(proxy: proxy)
                if !isAtBottom {
                    scrollToBottomButton(proxy: proxy)
                }
            }
        }
    }

    private func scrollArea(proxy: ScrollViewProxy) -> some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                loadMoreTrigger
                emptyState
                ForEach(viewModel.messages) { msg in
                    messageRow(msg).id(msg.id)
                }
                typingRow
                bottomAnchor
            }
            .padding()
        }
        .defaultScrollAnchor(.bottom)
        .scrollDismissesKeyboard(.interactively)
        .onChange(of: viewModel.pendingAnchorId) {
            guard let id = viewModel.pendingAnchorId else { return }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(16))
                proxy.scrollTo(id, anchor: .top)
                viewModel.pendingAnchorId = nil
            }
        }
        .onChange(of: viewModel.messages.count) {
            if !isAtBottom && viewModel.pendingAnchorId == nil {
                hasNewMessages = true
            }
        }
        .id("scrollArea-\(showToolCalls)")
    }

    @ViewBuilder
    private var loadMoreTrigger: some View {
        if viewModel.loadState == .loading {
            ProgressView()
                .controlSize(.small)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
        } else if viewModel.loadState == .readyForLoad {
            Color.clear
                .frame(height: 1)
                .onAppear {
                    Task { await viewModel.loadMoreHistory() }
                }
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        if viewModel.messages.isEmpty && !viewModel.isTyping && viewModel.historyLoaded {
            VStack(spacing: 12) {
                Image(systemName: "bubble.left.and.bubble.right")
                    .font(.system(size: 40))
                    .foregroundStyle(.tertiary)
                Text("Start a conversation")
                    .font(.title3.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(botDisplayName.isEmpty ? viewModel.bot : botDisplayName)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 80)
        }
    }

    @ViewBuilder
    private func messageRow(_ msg: ChatMessage) -> some View {
        switch msg.role {
        case .user, .assistant:
            MessageBubble(message: msg)
        case .toolCall, .toolResult:
            if showToolCalls { ToolCallView(message: msg) }
        case .skillOutput:
            if showToolCalls { SkillOutputCard(text: msg.text) }
        }
    }

    @ViewBuilder
    private var typingRow: some View {
        if viewModel.isTyping {
            TypingIndicator()
        }
    }

    private var bottomAnchor: some View {
        Color.clear.frame(height: 1).id("bottom")
            .onAppear { isAtBottom = true; hasNewMessages = false }
            .onDisappear { isAtBottom = false }
    }

    private func scrollToBottomButton(proxy: ScrollViewProxy) -> some View {
        Button {
            withAnimation { proxy.scrollTo("bottom") }
            hasNewMessages = false
        } label: {
            ZStack(alignment: .topTrailing) {
                Image(systemName: "arrow.down.circle.fill")
                    .font(.title)
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(.tint)
                if hasNewMessages {
                    Circle()
                        .fill(.red)
                        .frame(width: 10, height: 10)
                        .offset(x: 2, y: -2)
                }
            }
        }
        .buttonStyle(.plain)
        .padding(.trailing, 20)
        .padding(.bottom, 8)
    }

    private var composeBar: some View {
        HStack(spacing: 12) {
            TextField("Message...", text: $inputText, axis: .vertical)
                .lineLimit(1...5)
                .textFieldStyle(.plain)
                .focused($inputFocused)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .onSubmit {
                    sendCurrentMessage()
                }

            Button {
                sendCurrentMessage()
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
                    .symbolRenderingMode(.hierarchical)
            }
            .disabled(inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .padding(.trailing, 12)
        }
        .padding(.vertical, 6)
        .glassEffect(.regular, in: .capsule)
        .padding(.horizontal)
        .padding(.bottom, 8)
    }

    private func sendCurrentMessage() {
        let text = inputText
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        inputText = ""
        Task { await viewModel.send(text) }
    }
}

struct SessionInfoView: View {
    let machine: String
    let bot: String
    let chatId: String
    let isConnected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle()
                    .fill(isConnected ? .green : .red)
                    .frame(width: 8, height: 8)
                Text(isConnected ? "Connected" : "Offline")
                    .font(.subheadline.weight(.medium))
            }

            InfoRow(label: "Machine", value: machine)
            InfoRow(label: "Bot", value: bot)
            InfoRow(label: "Chat ID", value: chatId)

            Button {
                let text = "machine: \(machine)\nbot: \(bot)\nchat_id: \(chatId)"
                #if os(macOS)
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                #else
                UIPasteboard.general.string = text
                #endif
            } label: {
                Label("Copy Info", systemImage: "doc.on.doc")
                    .font(.subheadline)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
        }
        .padding()
        .frame(minWidth: 250)
    }
}

private struct InfoRow: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.monospaced())
                .textSelection(.enabled)
        }
    }
}

struct TypingIndicator: View {
    var body: some View {
        HStack(spacing: 0) {
            TimelineView(.animation) { timeline in
                let t = timeline.date.timeIntervalSinceReferenceDate
                HStack(spacing: 5) {
                    ForEach(0..<3, id: \.self) { i in
                        Circle()
                            .fill(.secondary)
                            .frame(width: 7, height: 7)
                            .offset(y: sin(t * 4 + Double(i) * 0.8) * 4)
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background {
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(.ultraThinMaterial)
            }
            Spacer(minLength: 60)
        }
    }
}