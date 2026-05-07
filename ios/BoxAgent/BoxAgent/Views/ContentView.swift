import SwiftUI

struct ContentView: View {
    @Environment(BotsViewModel.self) private var botsVM

    var body: some View {
        if botsVM.isConfigured {
            MainTabView()
        } else {
            NavigationStack {
                SettingsView(showAsDismissable: false)
            }
        }
    }
}

struct MainTabView: View {
    @Environment(BotsViewModel.self) private var botsVM

    var body: some View {
        TabView {
            Tab("Continue", systemImage: "play.circle") {
                NavigationStack {
                    ContinueView()
                }
            }
            Tab("Agents", systemImage: "cpu") {
                NavigationStack {
                    BotListView()
                }
            }
            Tab("Recents", systemImage: "clock") {
                NavigationStack {
                    RecentsView()
                }
            }
            Tab("Settings", systemImage: "gearshape") {
                NavigationStack {
                    SettingsView(showAsDismissable: false)
                }
            }
        }
        #if os(iOS)
        .tabBarMinimizeBehavior(.onScrollDown)
        #else
        .tabViewStyle(.sidebarAdaptable)
        #endif
        .task {
            await botsVM.loadMachines()
        }
    }
}
