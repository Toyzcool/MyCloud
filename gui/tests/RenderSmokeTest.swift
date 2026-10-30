// RenderSmokeTest.swift — runtime render smoke for every GUI surface.
//
// WHY: the GUI has no Xcode project and no unit tests; the only automated gate
// used to be "it compiles". That let a compiles-clean/crashes-at-runtime bug
// through (gui-macos12: a find&replace turned groupedFormCompat() into a call
// to itself -- infinite recursion on macOS 13+). This runner actually RENDERS
// each view off-screen with fixture data, so any body-evaluation crash fails
// the self-test with a named case.
//
// Compiled by gui/selftest.sh together with gui/src/*.swift MINUS
// MyCloudApp.swift (whose @main would collide with this one), for the HOST
// target. It therefore exercises the macOS-13+ branches on the dev machine;
// the macOS-12-only branches are compile-gated (dual-target build) and listed
// for hardware validation in gui/DEVLOG.md.

import AppKit
import SwiftUI

// MARK: fixtures

@MainActor
private func isoNow(offset: TimeInterval = 0) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: Date(timeIntervalSinceNow: offset))
}

@MainActor
private func fixtureApp(state: String,
                        syncing: Bool = false,
                        staleSeconds: TimeInterval = 0,
                        problems: Bool = false,
                        anonymous: Bool = false,
                        denied: Bool = false,
                        withConfig: Bool = true) -> AppState {
    let a = AppState()
    var st = Status()
    st.state = state
    if denied {
        var g = GuardInfo()
        g.layer = "sentinel"
        g.detail = "cat rc=1: cat: .../.mycloud_sentinel: Operation not permitted"
        st.guardInfo = g
    }
    st.updatedAt = isoNow(offset: -staleSeconds)
    st.lastSyncOkAt = isoNow(offset: -7200)

    if syncing {
        var p = Progress()
        p.active = true
        p.phase = "transferring"
        p.folder = "com~apple~CloudDocs"
        p.filesDone = 12_345
        p.totalFiles = 266_956
        p.bytesDone = 30_000_000_000
        p.bytesTotal = 68_800_000_000
        p.current = "Documents/Growth/big-file.bin"
        p.startedAt = isoNow(offset: -600)
        p.lastTransferAt = isoNow(offset: -3)
        p.batchesDone = 12
        p.batchesTotal = 534
        st.progress = p
    }

    var f = FolderStatus()
    f.state = syncing ? "SYNCING" : "upToDate"
    f.lastOk = isoNow(offset: -3600)
    f.filesCopied = 266_895
    if problems {
        f.failedFiles = (1...61).map { "Documents/venvs/broken-link-\($0)" }
        f.heldDeletions = ["Documents/held/a.txt", "Documents/held/b.txt"]
        var sk = IcloudSkipped()
        sk.count = 42
        sk.bytes = 123_456_789
        sk.samples = ["Documents/cloud-only-1.pdf", "Documents/cloud-only-2.mov"]
        f.icloudSkipped = sk
    }
    st.folders = ["com~apple~CloudDocs": f]
    a.status = st

    if withConfig {
        a.installed = true      // exercise the installed-only settings rows
        var cfg = Config()
        cfg.host = "192.168.5.1"
        cfg.share = "SHARE"
        cfg.username = "admin"
        if anonymous {
            cfg.authMode = "anonymous"
            cfg.username = "guest"
        }
        cfg.destRel = "MyCloud"
        var s1 = Source(); s1.path = "/Users/t/Library/Mobile Documents/com~apple~CloudDocs"
        s1.name = "com~apple~CloudDocs"; s1.isFile = false
        var s2 = Source(); s2.path = "/Users/t/Notes.txt"; s2.name = "Notes.txt"; s2.isFile = true
        cfg.sources = [s1, s2]
        a.config = cfg
    } else {
        a.config = nil
    }
    return a
}

// MARK: render harness

@MainActor
private var failures: [String] = []

/// Render `view` into an off-screen bitmap. A crash inside any body aborts the
/// process; the "RUN:" marker printed beforehand names the culprit case in the
/// selftest output.
@MainActor
private func render<V: View>(_ name: String, _ view: V, attachWindow: Bool = false) {
    print("RUN: \(name)")
    autoreleasepool {
        let host = NSHostingView(rootView: AnyView(view))
        host.frame = NSRect(x: 0, y: 0, width: 760, height: 560)
        var window: NSWindow? = nil
        if attachWindow {
            // Attach (but never order front) so window-dependent plumbing runs.
            let w = NSWindow(contentRect: host.frame, styleMask: [.titled],
                             backing: .buffered, defer: true)
            w.contentView = host
            window = w
        }
        host.layoutSubtreeIfNeeded()
        if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
            host.cacheDisplay(in: host.bounds, to: rep)
        }
        // Let deferred Tasks / onAppear side effects fire before the next case.
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        window?.contentView = nil
    }
    print("OK:  \(name)")
}

// MARK: entry

@main
@MainActor
struct RenderSmokeTest {
    static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)

        // Standalone shims.
        render("LabeledRow", LabeledRow("标签") { Text("值") }.padding())

        // Menu bar panel: idle and live-progress variants.
        render("MenuBarContent idle",
               MenuBarContent().environmentObject(fixtureApp(state: "MOUNTED_IDLE")))
        render("MenuBarContent syncing",
               MenuBarContent(dismiss: {})
                   .environmentObject(fixtureApp(state: "SYNCING", syncing: true)))

        // 概览 panel: idle / syncing / stale-unresponsive banner.
        render("OverviewView idle",
               OverviewView().environmentObject(fixtureApp(state: "MOUNTED_IDLE")))
        render("OverviewView syncing",
               OverviewView().environmentObject(fixtureApp(state: "SYNCING", syncing: true)))
        render("OverviewView stale",
               OverviewView().environmentObject(
                   fixtureApp(state: "SYNCING", syncing: true, staleSeconds: 600)))
        render("OverviewView permission-denied banner",
               OverviewView().environmentObject(
                   fixtureApp(state: "OFFLINE", denied: true)))

        // 同步项 panel: loading and populated.
        render("SourcesView config-nil",
               SourcesView().environmentObject(
                   fixtureApp(state: "MOUNTED_IDLE", withConfig: false)))
        render("SourcesView populated",
               SourcesView().environmentObject(fixtureApp(state: "MOUNTED_IDLE")))

        // 问题 panel with failed/held/orphan/iCloud content.
        render("ProblemsView populated",
               ProblemsView().environmentObject(
                   fixtureApp(state: "MOUNTED_IDLE", problems: true)))

        // 设置 panel: registered-user and anonymous connection modes.
        render("SettingsView",
               SettingsView().environmentObject(fixtureApp(state: "MOUNTED_IDLE")))
        render("SettingsView anonymous",
               SettingsView().environmentObject(
                   fixtureApp(state: "MOUNTED_IDLE", anonymous: true)))

        // Onboarding (uninstalled) path.
        render("OnboardingView",
               OnboardingView().environmentObject(fixtureApp(state: "OFFLINE")))

        // Root navigation chrome against the machine's REAL state (exercises
        // NavigationSplitView on 13+, onAppear -> app.start() polling).
        render("ContentView real-state",
               ContentView().environmentObject(AppState()), attachWindow: true)

        // Status-bar controller lifecycle (init creates a real NSStatusItem).
        print("RUN: StatusItemController lifecycle")
        var ctl: StatusItemController? = StatusItemController(
            app: fixtureApp(state: "SYNCING", syncing: true))
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.1))
        ctl = nil
        _ = ctl  // silence "written to but never read"
        print("OK:  StatusItemController lifecycle")

        if failures.isEmpty {
            print("RENDER-SMOKE: ALL OK")
            exit(0)
        } else {
            print("RENDER-SMOKE: FAILURES: \(failures)")
            exit(1)
        }
    }
}
