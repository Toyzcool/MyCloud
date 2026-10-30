// MyCloudApp.swift — @main entry. Wires the main window (four-pane ContentView)
// and the menu-bar status item to a single shared AppState.
//
// macOS 12 compatibility (gui-macos12): MenuBarExtra, WindowGroup(id:),
// .defaultSize and @Environment(\.openWindow) are all macOS 13+ SwiftUI. The
// menu-bar entry is therefore an AppKit NSStatusItem hosting MenuBarContent in
// an NSPopover (StatusItem.swift) -- one code path that behaves identically on
// macOS 12 through 26. Window sizing lives on ContentView's frame.

import AppKit
import SwiftUI

/// Owns the single AppState and the status-bar item. An NSApplicationDelegate
/// (rather than @StateObject in the App) so the SAME instance backs both the
/// window scene and the AppKit status item without an availability fork.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let app = AppState()
    private var statusItem: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = StatusItemController(app: app)
        // Start polling here, not only in ContentView.onAppear: the status-bar
        // icon must track the daemon even if no window ever appears. start()
        // is idempotent, so the onAppear call is a harmless refresh.
        app.start()
    }
}

@main
struct MyCloudApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup("MyCloud") {
            ContentView()
                .environmentObject(delegate.app)
        }
    }
}
