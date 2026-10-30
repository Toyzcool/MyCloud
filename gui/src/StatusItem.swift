// StatusItem.swift — AppKit menu-bar entry: NSStatusItem + NSPopover hosting
// the SwiftUI MenuBarContent. Replaces MenuBarExtra (a macOS 13+ API) so one
// binary serves macOS 12 gateway machines and macOS 13+ alike — one code path,
// no availability fork (gui-macos12 branch).

import AppKit
import Combine
import SwiftUI

@MainActor
final class StatusItemController: NSObject {
    private let app: AppState
    private let statusItem: NSStatusItem
    private let popover = NSPopover()
    private var cancellable: AnyCancellable?
    private var closeObserver: NSObjectProtocol?
    private var currentSymbol = ""
    /// When the transient popover last auto-closed. Some AppKit builds close a
    /// transient popover on mouseDown at the status button and then deliver the
    /// button's action on mouseUp -- which would instantly REOPEN it, reading
    /// as "clicking the icon never closes the panel". Toggles arriving within
    /// this window after a close are ignored.
    private var lastClose = Date.distantPast

    init(app: AppState) {
        self.app = app
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()

        let pop = popover
        popover.behavior = .transient
        popover.animates = false
        popover.contentViewController = NSHostingController(
            rootView: MenuBarContent(dismiss: { [weak pop] in
                pop?.performClose(nil)
            }).environmentObject(app))

        if let button = statusItem.button {
            button.target = self
            button.action = #selector(togglePopover(_:))
        }

        // Mirror menuBarSymbol() into the item image whenever AppState changes;
        // only touch AppKit when the symbol actually differs. (The Task hop is
        // required: objectWillChange fires BEFORE the new values are assigned.)
        cancellable = app.objectWillChange.sink { [weak self] _ in
            Task { @MainActor in self?.refreshIcon() }
        }
        closeObserver = NotificationCenter.default.addObserver(
            forName: NSPopover.didCloseNotification, object: popover,
            queue: .main) { [weak self] _ in
            Task { @MainActor in self?.lastClose = Date() }
        }
        refreshIcon()
    }

    private func refreshIcon() {
        let name = menuBarSymbol(for: app.status, installed: app.installed,
                                 stale: app.unresponsive || app.syncStalled
                                     || app.agentExited,
                                 progressLive: app.progressLive)
        guard name != currentSymbol else { return }
        currentSymbol = name
        // Fall back to the plain 2019 "icloud" glyph rather than ever setting a
        // nil image: an unknown symbol name on an older macOS must degrade to a
        // generic icon, not a BLANK menu-bar item (the failure mode the 2023
        // symbols caused on 12/13 before the symbol gate existed).
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "MyCloud")
            ?? NSImage(systemSymbolName: "icloud", accessibilityDescription: "MyCloud")
        image?.isTemplate = true  // follow the menu bar's light/dark rendering
        statusItem.button?.image = image
    }

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        // Swallow the toggle when the transient close JUST happened (see
        // lastClose): that toggle is the mouseUp half of the same click.
        guard Date().timeIntervalSince(lastClose) > 0.15 else { return }
        guard let button = statusItem.button else { return }
        // macOS 12's NSPopover does not reliably auto-track NSHostingController
        // sizing; measure the SwiftUI content and pin the size explicitly.
        if let vc = popover.contentViewController {
            vc.view.layoutSubtreeIfNeeded()
            popover.contentSize = vc.view.fittingSize
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }
}

/// Bring the main window to the front, creating one if every WindowGroup
/// window was closed. @Environment(\.openWindow) is macOS 13+, so this works
/// in AppKit terms instead.
///
/// Matching is by capability, not by name: the window's title is whatever
/// navigationTitle is showing (「概览」 etc., never "MyCloud"), and identifier
/// prefixes died with WindowGroup(id:). Any window that CAN become main is our
/// main window -- the status popover and the status-bar window itself cannot.
/// The bundle-URL reopen (which makes SwiftUI create a fresh window, public
/// behavior on macOS 12) stays strictly a zero-window fallback: on a running
/// app it costs a LaunchServices round-trip and, right after iCloud swapped in
/// a rebuilt dist/MyCloud.app, could even launch a SECOND instance.
@MainActor
func openMainWindow() {
    NSApp.activate(ignoringOtherApps: true)
    if let win = NSApp.windows.first(where: { $0.canBecomeMain }) {
        if win.isMiniaturized { win.deminiaturize(nil) }
        win.makeKeyAndOrderFront(nil)
    } else {
        NSWorkspace.shared.open(Bundle.main.bundleURL)
    }
}
