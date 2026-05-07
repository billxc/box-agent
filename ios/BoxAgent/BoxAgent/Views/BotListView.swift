import SwiftUI

struct PressableCardStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.96 : 1.0)
            .opacity(configuration.isPressed ? 0.7 : 1.0)
            .animation(.easeInOut(duration: 0.15), value: configuration.isPressed)
    }
}

struct BotListView: View {
    @Environment(BotsViewModel.self) private var botsVM

    var body: some View {
        ScrollView {
            if botsVM.isLoading {
                ProgressView()
                    .padding(.top, 60)
            } else if let error = botsVM.error {
                ContentUnavailableView("Connection Error", systemImage: "wifi.slash", description: Text(error))
            } else {
                LazyVStack(spacing: 16) {
                    ForEach(botsVM.machines) { machine in
                        MachineSection(machine: machine)
                    }
                }
                .padding()
            }
        }
        .navigationTitle("BoxAgent")
        .refreshable {
            await botsVM.loadMachines()
        }
    }
}

struct MachineSection: View {
    let machine: Machine

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle()
                    .fill(machine.online ? .green : .gray)
                    .frame(width: 8, height: 8)
                Text(machine.machineId)
                    .font(.headline)
                Spacer()
                Text(machine.role)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            GlassEffectContainer {
                ForEach(machine.bots) { bot in
                    NavigationLink {
                        SessionListView(bot: bot, machine: machine)
                    } label: {
                        BotCard(bot: bot)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .buttonStyle(PressableCardStyle())
                    .contentShape(Rectangle())
                    .glassEffect(.regular, in: .rect(cornerRadius: 16))
                }
            }
        }
    }
}

struct BotCard: View {
    let bot: Bot

    var body: some View {
        HStack {
            Image(systemName: bot.kind == "workgroup" ? "person.3" : "brain.head.profile")
                .font(.title2)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 2) {
                Text(bot.displayName)
                    .font(.body.weight(.medium))
                Text(bot.backend)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let model = bot.model {
                Text(model.replacingOccurrences(of: "claude-", with: ""))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Image(systemName: "chevron.right")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .contentShape(Rectangle())
    }
}
