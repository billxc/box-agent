import SwiftUI

@main
struct BoxAgentApp: App {
    @State private var botsVM = BotsViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(botsVM)
        }
    }
}
