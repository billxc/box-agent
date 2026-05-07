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

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 8) {
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
                    ForEach(viewModel.messages) { msg in
                        switch msg.role {
                        case .user, .assistant:
                            MessageBubble(message: msg)
                        case .toolCall, .toolResult:
                            if showToolCalls {
                                ToolCallView(message: msg)
                            }
                        case .skillOutput:
                            if showToolCalls {
                                SkillOutputCard(text: msg.text)
                            }
                        }
                    }
                    if viewModel.isTyping {
                        TypingIndicator()
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding()
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: viewModel.messages.count) {
                withAnimation {
                    proxy.scrollTo("bottom")
                }
            }
            .onChange(of: viewModel.isTyping) {
                if viewModel.isTyping {
                    withAnimation {
                        proxy.scrollTo("bottom")
                    }
                }
            }
        }
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
            .disabled(inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || viewModel.isSending)
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