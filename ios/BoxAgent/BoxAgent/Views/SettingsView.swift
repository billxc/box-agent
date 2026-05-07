import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

struct SettingsView: View {
    @Environment(BotsViewModel.self) private var botsVM
    @Environment(\.dismiss) private var dismiss
    var showAsDismissable: Bool
    @State private var serverURL = ""
    @State private var token = ""
    @State private var showToken = false
    @State private var versionInfo: String?
    @State private var isTesting = false
    @State private var testError: String?
    @AppStorage("showToolCalls") private var showToolCalls = true

    var body: some View {
        Form {
            Section("Server") {
                TextField("your-tunnel", text: $serverURL)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                    #if !os(macOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif

                HStack {
                    Group {
                        if showToken {
                            TextField("Tunnel Token", text: $token)
                        } else {
                            SecureField("Tunnel Token", text: $token)
                        }
                    }
                    .autocorrectionDisabled()
                    #if !os(macOS)
                    .textInputAutocapitalization(.never)
                    #endif

                    Button {
                        showToken.toggle()
                    } label: {
                        Image(systemName: showToken ? "eye.slash" : "eye")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)

                    Button {
                        #if os(macOS)
                        if let clip = NSPasteboard.general.string(forType: .string), !clip.isEmpty {
                            token = clip.trimmingCharacters(in: .whitespacesAndNewlines)
                        }
                        #else
                        if let clip = UIPasteboard.general.string, !clip.isEmpty {
                            token = clip.trimmingCharacters(in: .whitespacesAndNewlines)
                        }
                        #endif
                    } label: {
                        Image(systemName: "doc.on.clipboard")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }

            Section("Display") {
                Toggle("Show Tool Calls", isOn: $showToolCalls)
            }

            Section("Debug") {
                NavigationLink {
                    LogView()
                } label: {
                    Label("View Logs", systemImage: "doc.text.magnifyingglass")
                }
            }

            Section {
                Button {
                    Task { await testConnection() }
                } label: {
                    HStack {
                        Text("Test Connection")
                        Spacer()
                        if isTesting {
                            ProgressView()
                                .controlSize(.small)
                        }
                    }
                }
                .disabled(serverURL.isEmpty || token.isEmpty || isTesting)

                Button("Save Server Settings") {
                    botsVM.serverURL = serverURL
                    botsVM.token = token
                    Task { await botsVM.loadMachines() }
                }
                .disabled(serverURL.isEmpty || token.isEmpty)

                if let info = versionInfo {
                    Label(info, systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
                if let err = testError {
                    Label(err, systemImage: "xmark.circle.fill")
                        .foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Settings")
        .onAppear {
            serverURL = botsVM.serverURL
            token = botsVM.token
        }
    }

    private func testConnection() async {
        isTesting = true
        versionInfo = nil
        testError = nil
        let api = APIClient(baseURL: serverURL, token: token)
        do {
            let v = try await api.fetchVersion()
            versionInfo = v.versionString ?? v.version ?? "Connected"
        } catch {
            testError = error.localizedDescription
        }
        isTesting = false
    }
}
