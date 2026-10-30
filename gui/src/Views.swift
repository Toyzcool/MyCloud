// Views.swift — the real four-pane UI (概览/同步项/问题/设置), MenuBarExtra, and
// the shared presentation helpers. Panels for 同步项/问题/设置 live in
// ViewsPanels.swift. All user-facing copy is Simplified Chinese.
//
// Contract: this layer is a thin shell. It only READS via AppState (state.json
// poll + config-get) and WRITES via the ctl commands AppState wraps. No new
// frameworks beyond SwiftUI/AppKit/Foundation. Every action is disabled — never
// hidden — when the backend is unavailable, so the UI shape stays stable.

import SwiftUI
import AppKit

/// The four sidebar destinations (PLAN 12.3).
enum AppSection: String, CaseIterable, Identifiable, Hashable {
    case overview
    case sources
    case problems
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "概览"
        case .sources: return "同步项"
        case .problems: return "问题"
        case .settings: return "设置"
        }
    }

    var systemImage: String {
        switch self {
        // "gauge" (SF Symbols 2019); the dotted-needle variant is 2023/macOS 14+
        // and renders as a BLANK on 12/13 (supervised review, 2026-08-01).
        case .overview: return "gauge"
        case .sources: return "folder"
        case .problems: return "exclamationmark.triangle"
        case .settings: return "gearshape"
        }
    }
}

// MARK: - Shared presentation helpers

/// Chinese label for a display state: the daemon's top-level state (PLAN 12.3
/// mapping) or one AppState.displayState derives (UNRESPONSIVE, STALLED,
/// EXITED, PAUSED, ATTENTION). Falls back to the raw value so an unknown future
/// state is still shown, never blank.
func stateLabel(_ raw: String, installed: Bool) -> String {
    if !installed { return "未安装" }
    switch raw.uppercased() {
    case "OFFLINE": return "离线"
    case "MOUNTED_IDLE": return "空闲"
    case "SYNCING": return "同步中"
    case "STARTING": return "启动中"
    case "RESTARTING": return "重启中"
    case "DELETIONS_HELD": return "待确认删除"
    case "DEGRADED": return "部分异常"
    case "ATTENTION": return "需要处理"
    case "STALLED": return "同步停滞"
    case "PAUSED": return "已暂停"
    case "STOPPED": return "已停止"
    case "EXITED": return "守护进程已退出"
    case "UNRESPONSIVE": return "守护进程无响应"
    default: return raw.isEmpty ? "未知" : raw
    }
}

/// Accent color for the state badge.
func stateColor(_ raw: String, installed: Bool) -> Color {
    if !installed { return .secondary }
    switch raw.uppercased() {
    case "MOUNTED_IDLE": return .green
    case "SYNCING", "STARTING", "RESTARTING": return .blue
    case "DELETIONS_HELD", "DEGRADED", "ATTENTION", "STALLED", "EXITED", "UNRESPONSIVE":
        return .orange
    case "OFFLINE", "STOPPED", "PAUSED": return .secondary
    default: return .secondary
    }
}

/// SF Symbol for the state badge.
func stateSymbol(_ raw: String, installed: Bool) -> String {
    if !installed { return "icloud.slash" }
    switch raw.uppercased() {
    case "MOUNTED_IDLE": return "checkmark.icloud"
    // arrow.clockwise.icloud (2019): arrow.triangle.2.circlepath.icloud is
    // 2023/macOS 14+ only -- on 12/13 NSImage(systemSymbolName:) returns nil
    // and the badge went blank exactly while a sync was RUNNING.
    case "SYNCING", "STARTING", "RESTARTING": return "arrow.clockwise.icloud"
    case "DELETIONS_HELD", "DEGRADED", "ATTENTION", "STALLED", "EXITED", "UNRESPONSIVE":
        return "exclamationmark.icloud"
    case "OFFLINE", "STOPPED", "PAUSED": return "icloud.slash"
    default: return "icloud"
    }
}

/// Parse an ISO-8601 timestamp (with or without fractional seconds) to a Date.
func parseISO(_ s: String?) -> Date? {
    guard let s = s, !s.isEmpty else { return nil }
    let f1 = ISO8601DateFormatter()
    f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f1.date(from: s) { return d }
    let f2 = ISO8601DateFormatter()
    f2.formatOptions = [.withInternetDateTime]
    return f2.date(from: s)
}

/// A Chinese relative-time phrase for an ISO timestamp ("刚刚 / 5 分钟前 / …").
func relativeTimeCN(_ iso: String?) -> String {
    guard let date = parseISO(iso) else { return "从未" }
    let secs = Date().timeIntervalSince(date)
    if secs < 0 { return "刚刚" }
    if secs < 60 { return "刚刚" }
    let mins = Int(secs / 60)
    if mins < 60 { return "\(mins) 分钟前" }
    let hours = mins / 60
    if hours < 24 { return "\(hours) 小时前" }
    let days = hours / 24
    if days < 30 { return "\(days) 天前" }
    let months = days / 30
    if months < 12 { return "\(months) 个月前" }
    return "\(months / 12) 年前"
}

/// Truncate a long path in the middle so both ends stay visible.
func truncateMiddle(_ s: String, max: Int = 56) -> String {
    if s.count <= max { return s }
    let keep = (max - 1) / 2
    let head = String(s.prefix(keep))
    let tail = String(s.suffix(keep))
    return head + "…" + tail
}

/// Human GB string from a byte count (decimal GB, matching Finder). Adapts the
/// precision so both tiny (0.34) and huge (45 / 221) totals stay readable. The
/// caller appends the "GB" unit, so this returns the bare number.
func gbString(_ bytes: Int) -> String {
    let gb = Double(max(bytes, 0)) / 1_000_000_000
    if gb >= 100 { return String(format: "%.0f", gb) }
    if gb >= 10 { return String(format: "%.1f", gb) }
    return String(format: "%.2f", gb)
}

// MARK: - Journal reader (最近活动)

/// One parsed line of runtime/journal.ndjson. Handles both per-run records
/// (folder/copied/trashed/status) and event records (event=...).
struct JournalEntry: Identifiable {
    let id = UUID()
    var ts: String = ""
    var folder: String = ""
    var copied: Int = 0
    var trashed: Int = 0
    var status: String = ""
    var event: String = ""
    var isFile: Bool = false
}

var journalURL: URL { Bridge.appDir.appendingPathComponent("runtime/journal.ndjson") }

/// mtime of journal.ndjson (nil when absent): re-read only when it changes.
func journalMtime() -> Date? {
    (try? FileManager.default.attributesOfItem(atPath: journalURL.path))?[.modificationDate]
        as? Date
}

/// Read the tail (default 20) of journal.ndjson, newest first. Only the last
/// `maxBytes` are read -- the whole file used to be read on the main thread on
/// every state change (245ms for a 24MB journal, every 2-3s while syncing).
/// Never throws; returns [] when the journal is absent or unreadable.
func readJournalTail(limit: Int = 20, maxBytes: UInt64 = 64 * 1024) -> [JournalEntry] {
    guard let fh = try? FileHandle(forReadingFrom: journalURL) else { return [] }
    defer { try? fh.close() }
    guard let size = try? fh.seekToEnd() else { return [] }
    let start = size > maxBytes ? size - maxBytes : 0
    guard (try? fh.seek(toOffset: start)) != nil,
          let data = try? fh.readToEnd() else { return [] }
    var text = String(decoding: data, as: UTF8.self)
    if start > 0, let nl = text.firstIndex(of: "\n") {
        text = String(text[text.index(after: nl)...])   // drop the partial first line
    }
    let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
    let tail = lines.suffix(limit)
    var out: [JournalEntry] = []
    for line in tail {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else { continue }
        var e = JournalEntry()
        e.ts = dict["ts"] as? String ?? ""
        e.folder = dict["folder"] as? String ?? ""
        e.copied = (dict["copied"] as? Int) ?? Int(dict["copied"] as? Double ?? 0)
        e.trashed = (dict["trashed"] as? Int) ?? Int(dict["trashed"] as? Double ?? 0)
        e.status = dict["status"] as? String ?? ""
        e.event = dict["event"] as? String ?? ""
        e.isFile = dict["is_file"] as? Bool ?? false
        out.append(e)
    }
    return out.reversed()
}

// MARK: - Root

/// What the root alert is showing: an adoption prompt, or messages.
enum RootAlert {
    case adopt(AppState.PendingAdopt)
    case message(String)
}

struct ContentView: View {
    @EnvironmentObject var app: AppState

    // Optional: the non-Optional List selection binding is macOS 13+; the
    // Optional variant goes back to 10.15. nil (sidebar deselected) falls
    // back to 概览. Held in AppState so a banner can jump to 问题.
    private var selection: AppSection? { app.selectedSection }

    /// ONE alert for everything: macOS 12 presents only one of several
    /// `.alert`s chained on a view. The adoption prompt lives here (not on
    /// SettingsView) so the install wizard can show it too.
    private var rootAlert: RootAlert? {
        if let p = app.pendingAdopt { return .adopt(p) }
        let parts = [app.lastError, app.notice].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : .message(parts.joined(separator: "\n\n"))
    }

    private var rootAlertTitle: String {
        if app.pendingAdopt != nil { return "接管现有备份？" }
        return app.lastError != nil ? "操作失败" : "提示"
    }

    var body: some View {
        Group {
            if app.installed {
                splitRoot
            } else {
                OnboardingView()
            }
        }
        // Window sizing: .defaultSize on the WindowGroup is macOS 13+, so the
        // initial size comes from the root view's minimum frame instead.
        .frame(minWidth: 720, minHeight: 520)
        .onAppear { app.start() }
        // Deselecting (⌘-click / clicking sidebar whitespace) leaves detail on
        // 概览 but drops the highlight; snap back so the sidebar always shows
        // where you are.
        .onChange(of: app.selectedSection) { v in
            if v == nil { app.selectedSection = .overview }
        }
        .alert(Text(rootAlertTitle), isPresented: Binding(
            get: { rootAlert != nil },
            set: { if !$0 { app.dismissAlert() } }
        ), presenting: rootAlert) { item in
            // Buttons act on the value captured HERE: the dismissal clears the
            // published state before a Task started by the button could read it.
            switch item {
            case .adopt(let p):
                Button("取消", role: .cancel) {}
                Button("确认接管", role: .destructive) {
                    Task { @MainActor in await app.confirmAdopt(p) }
                }
            case .message:
                Button("好", role: .cancel) {}
            }
        } message: { item in
            switch item {
            case .adopt(let p): Text(p.message)
            case .message(let m): Text(m)
            }
        }
    }

    // NavigationSplitView is macOS 13+; on 12 the two-column NavigationView
    // renders the same sidebar+detail layout (gui-macos12).
    @ViewBuilder
    private var splitRoot: some View {
        if #available(macOS 13.0, *) {
            NavigationSplitView {
                sidebar
            } detail: {
                DetailPane(section: selection ?? .overview)
                    .frame(minWidth: 480, minHeight: 380)
            }
        } else {
            NavigationView {
                sidebar
                DetailPane(section: selection ?? .overview)
                    .frame(minWidth: 480, minHeight: 380)
            }
            .toolbar {
                // Monterey's NavigationView has no built-in sidebar toggle and
                // the sidebar column CAN be drag-collapsed with no way back --
                // this button is the recovery path (13+ has its own chevron).
                ToolbarItem(placement: .navigation) {
                    Button {
                        NSApp.sendAction(
                            #selector(NSSplitViewController.toggleSidebar(_:)),
                            to: nil, from: nil)
                    } label: {
                        Image(systemName: "sidebar.left")
                    }
                    .help("显示/隐藏侧栏")
                }
            }
        }
    }

    private var sidebar: some View {
        List(AppSection.allCases, selection: $app.selectedSection) { section in
            Label(section.title, systemImage: section.systemImage)
                .tag(section)
        }
        .listStyle(.sidebar)
        .navigationTitle("MyCloud")
        .frame(minWidth: 180)
    }

}

/// The detail column as ONE stable view type. On Monterey the second
/// NavigationView column is replaced in place; swapping between four distinct
/// root types there has a history of toolbar duplication/loss glitches. With
/// this wrapper the column's type never changes -- only its content switches.
private struct DetailPane: View {
    @EnvironmentObject var app: AppState
    let section: AppSection

    var body: some View {
        switch section {
        case .overview: OverviewView()
        case .sources: SourcesView()
        case .problems: ProblemsView()
        case .settings: SettingsView()
        }
    }
}

// MARK: - 概览 Overview

struct OverviewView: View {
    @EnvironmentObject var app: AppState
    @State private var journal: [JournalEntry] = []
    @State private var journalStamp: Date? = nil

    /// The badge's state: the raw state folded with what it cannot know itself
    /// (a dead/wedged daemon, a stalled transfer, held deletions, paused vs
    /// exited) -- never a frozen 空闲/同步中.
    private var stateRaw: String { app.displayState }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                badge
                lastSyncRow
                unresponsiveBanner
                stalledBanner
                exitedBanner
                attentionRow
                permissionBanner
                progressSection
                actionButtons
                Divider()
                activitySection
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .navigationTitle("概览")
        .onAppear { reloadJournal(force: true) }
        .onChange(of: app.status?.updatedAt) { _ in reloadJournal(force: false) }
    }

    /// Re-read the journal tail off the main thread, only when it changed.
    private func reloadJournal(force: Bool) {
        let stamp = journalMtime()
        if !force && stamp == journalStamp { return }
        journalStamp = stamp
        Task {
            let entries = await Task.detached(priority: .utility) { readJournalTail() }.value
            journal = entries
        }
    }

    /// One restart button shared by the banners (busy spinner + inline result).
    @ViewBuilder
    private var restartButton: some View {
        Button {
            Task { await app.restartDaemon() }
        } label: {
            if app.fixBusyId == "restart_daemon" {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("重启中…")
                }
            } else {
                Label("重启守护进程", systemImage: "arrow.clockwise.circle")
            }
        }
        .disabled(!app.installed || app.anyBusy)
        if let r = app.fixResults["restart_daemon"] {
            Text(r.detail).font(.caption)
                .foregroundColor(r.ok ? .green : .red)
        }
    }

    private func banner<Content: View>(_ symbol: String, _ title: String,
                                       @ViewBuilder _ content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.title2).foregroundColor(.orange)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.headline).foregroundColor(.orange)
                content()
            }
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.12)))
    }

    /// Minutes/seconds phrase for a banner.
    private func durationCN(_ seconds: Double) -> String {
        let s = Int(max(seconds, 0))
        if s < 120 { return "\(s) 秒" }
        if s < 7200 { return "\(s / 60) 分钟" }
        return "\(s / 3600) 小时"
    }

    /// A transfer that shows no movement for longer than the daemon's own stall
    /// kill while its heartbeat stays fresh: the pass is wedged (typically on a
    /// half-dead SMB session) and the daemon did not recover by itself.
    @ViewBuilder
    private var stalledBanner: some View {
        if app.syncStalled && !app.unresponsive {
            banner("exclamationmark.icloud", "同步停滞") {
                Text("正在备份，但已超过 \(durationCN(LivenessBudget.transferStall)) 没有任何文件传输进展。守护进程仍在运行，可能卡在 NAS 连接上。可尝试重启守护进程；若反复出现，请检查 NAS 与网络。")
                    .font(.callout).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                restartButton
            }
        }
    }

    /// state.json says stopped but launchd still has the agent loaded: the
    /// daemon exited by itself (a crash loop, or it gave up) -- not a pause.
    @ViewBuilder
    private var exitedBanner: some View {
        if app.displayState == "EXITED" {
            banner("exclamationmark.icloud", "守护进程已退出") {
                Text("守护进程并未被暂停，但当前没有在运行"
                     + (app.agentStatus?.lastExit.map { "（上次退出码 \($0)）" } ?? "")
                     + "。launchd 可能正在反复重启它。请点「打开日志文件夹」查看 mycloud.log 与 launchd.err.log，或尝试重启守护进程。")
                    .font(.callout).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    restartButton
                    Button("打开日志文件夹") { app.openLogFolder() }
                }
            }
        }
    }

    /// Held deletions / failed files / frozen deletion propagation while the
    /// badge would otherwise say 空闲: point at 问题.
    @ViewBuilder
    private var attentionRow: some View {
        if app.needsAttention, let s = app.status, !app.unresponsive {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle").foregroundColor(.orange)
                VStack(alignment: .leading, spacing: 4) {
                    Text(attentionText(s)).font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("前往「问题」处理") { app.selectedSection = .problems }
                }
                Spacer()
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
        }
    }

    private func attentionText(_ s: Status) -> String {
        var parts: [String] = []
        if s.deletionsHeld {
            parts.append("\(s.heldCountUncertain ? "至少 " : "")\(s.totalHeldDeletions) 个删除待确认")
        }
        if s.totalFailedFiles > 0 { parts.append("\(s.totalFailedFiles) 个文件多次同步失败") }
        if s.deletionDisabled { parts.append("删除传播已停用") }
        if let e = s.lastError, !e.isEmpty { parts.append("守护进程报告了错误（见上方说明）") }
        return attentionSentence(parts)
    }

    /// TCC refused the daemon's read of the NAS (state.json "guard" detail says
    /// "Operation not permitted"), or an install just finished without the
    /// daemon coming online: Full Disk Access for /usr/bin/python3 is the fix,
    /// and macOS only lets the USER grant it -- so give one-click access to the
    /// pane and to python3 in Finder (drag it into the list), then a restart.
    @ViewBuilder
    private var permissionBanner: some View {
        let denied = (app.status?.guardInfo?.isPermissionDenied ?? false)
            && !["MOUNTED_IDLE", "SYNCING"].contains(app.status?.state.uppercased() ?? "")
        if denied || app.postInstallHint != nil {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "lock.shield")
                    .font(.title2).foregroundColor(.orange)
                VStack(alignment: .leading, spacing: 6) {
                    Text(denied ? "系统拒绝了守护进程读取 NAS（Operation not permitted）"
                                : "还差一步：授予「完全磁盘访问」")
                        .font(.headline).foregroundColor(.orange)
                    Text("后台守护进程以 /usr/bin/python3 运行，需要「完全磁盘访问」才能读取 NAS 与本机文件。操作：点第一个按钮打开设置面板并解锁；点第二个按钮，把访达里已选中的 python3 拖进列表并勾选；最后重启守护进程。")
                        .font(.callout).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 10) {
                        Button("打开「完全磁盘访问」设置") { app.openFullDiskAccessSettings() }
                        Button("在访达中显示 python3") { app.revealPython3InFinder() }
                        Button {
                            Task { await app.restartDaemon() }
                        } label: {
                            if app.fixBusyId == "restart_daemon" {
                                HStack(spacing: 6) { ProgressView().controlSize(.small); Text("重启中…") }
                            } else {
                                Text("重启守护进程")
                            }
                        }
                        .disabled(app.anyBusy)
                    }
                }
            }
            .padding(12)
            .background(Color.orange.opacity(0.08))
            .cornerRadius(8)
        }
    }

    // Raised when state.json has gone stale (file mtime > 30s during a claimed
    // sync, or past the idle budget): the daemon is not heartbeating. Offers a
    // one-click 重启守护进程 (ctl fix restart_daemon) with busy + inline result.
    @ViewBuilder
    private var unresponsiveBanner: some View {
        if app.unresponsive {
            banner("exclamationmark.icloud", "守护进程无响应") {
                Text(unresponsiveText)
                    .font(.callout).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                restartButton
            }
        }
    }

    private var unresponsiveText: String {
        if app.daemonGone {
            return "记录的守护进程已不在运行（被系统终止或崩溃），同步已停止。可尝试重启守护进程；若反复出现，请查看日志。"
        }
        let age = app.stateFileAgeSeconds.map { durationCN($0) } ?? "很久"
        return "已有 \(age) 没有收到守护进程的状态更新，同步可能已停止。可尝试重启守护进程。"
    }

    private var badge: some View {
        HStack(spacing: 12) {
            Image(systemName: stateSymbol(stateRaw, installed: app.installed))
                .font(.system(size: 40))
                .foregroundColor(stateColor(stateRaw, installed: app.installed))
            VStack(alignment: .leading, spacing: 2) {
                Text(stateLabel(stateRaw, installed: app.installed))
                    .font(.largeTitle).bold()
                    .foregroundColor(stateColor(stateRaw, installed: app.installed))
                if let e = app.status?.lastError, !e.isEmpty {
                    Text(e).font(.footnote).foregroundColor(.secondary)
                        .lineLimit(2)
                }
                // Why it is offline: the failing guard layer and its mechanism
                // (a mount failure used to be a bare 「离线」 with no reason).
                if let g = app.status?.guardInfo, !g.detail.isEmpty, !g.isPermissionDenied,
                   !["MOUNTED_IDLE", "SYNCING"].contains(app.status?.state.uppercased() ?? "") {
                    Text(g.layer == "mount" ? "无法挂载 NAS：\(g.detail)"
                                            : "挂载检查失败（\(g.layer)）：\(g.detail)")
                        .font(.footnote).foregroundColor(.orange)
                        .lineLimit(3)
                }
            }
        }
    }

    private var lastSyncRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "clock.arrow.circlepath").foregroundColor(.secondary)
            Text("上次成功同步：").foregroundColor(.secondary)
            Text(relativeTimeCN(app.status?.lastSyncOkAt))
        }
        .font(.callout)
    }

    @ViewBuilder
    private var progressSection: some View {
        // Only render the progress bar for a LIVE pass; a stale "active" block
        // left by a killed daemon must not show a frozen bar (PLAN 12.4).
        if app.progressLive, let p = app.status?.progress {
            VStack(alignment: .leading, spacing: 6) {
                progressBody(p)
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
        }
    }

    /// Phase-driven progress content (PLAN v2). Each branch owns its own caption
    /// so unknown/legacy phases still render a sensible bar.
    @ViewBuilder
    private func progressBody(_ p: Progress) -> some View {
        switch p.phase {
        case "planning":
            // Building the change set: no counts yet → indeterminate + minute.
            ProgressView { Text("正在计算变更…（第 \(scanMinute(p)) 分钟）") }
            folderCaption(p)
        case "downloading":
            downloadingBar(p)
        case "verifying":
            ProgressView { Text("正在深度校验备份…") }
            folderCaption(p)
        case "transferring":
            transferringBar(p)
        default:
            // Legacy "scanning" keeps its old copy; empty/unknown falls back to
            // the transferring bar so pre-v2 backends still show progress.
            if p.isScanning {
                ProgressView { Text("扫描文件清单中（第 \(scanMinute(p)) 分钟）") }
                folderCaption(p)
            } else {
                transferringBar(p)
            }
        }
    }

    /// downloading: bar driven by BYTES when available (see transferringBar for
    /// why), falling back to the file count, then to indeterminate.
    @ViewBuilder
    private func downloadingBar(_ p: Progress) -> some View {
        if let frac = p.byteFraction {
            ProgressView(value: frac) {
                Text("正在从 iCloud 下载云端文件 \(pctString(frac))\(bytesPair(p))\(filesSuffix(p))")
            }
        } else if let total = p.totalFiles, total > 0 {
            ProgressView(value: Double(min(p.filesDone, total)), total: Double(total)) {
                Text("正在从 iCloud 下载云端文件 \(p.filesDone)/\(total)")
            }
        } else {
            ProgressView {
                Text("正在从 iCloud 下载云端文件（已下载 \(p.filesDone) 个）")
            }
        }
        activityCaption(p)
        folderCaption(p)
    }

    /// transferring: the bar tracks transferred BYTES, not file count. Across
    /// 315k files spanning several orders of magnitude in size, a count-based
    /// percentage misrepresents the work left (90% of files can be 30% of the
    /// bytes). File count stays as a secondary annotation.
    @ViewBuilder
    private func transferringBar(_ p: Progress) -> some View {
        if let frac = p.byteFraction {
            ProgressView(value: frac) {
                Text("备份中 \(pctString(frac))\(bytesPair(p))\(filesSuffix(p))")
            }
        } else if let total = p.totalFiles, total > 0 {
            ProgressView(value: Double(min(p.filesDone, total)), total: Double(total)) {
                Text("备份中 \(p.filesDone)/\(total) 文件")
            }
        } else {
            ProgressView {
                Text("备份中（已传输 \(p.filesDone) 个文件）")
            }
        }
        activityCaption(p)
        if !p.current.isEmpty { currentCaption(p) } else { folderCaption(p) }
    }

    /// "42%" from a 0...1 fraction.
    private func pctString(_ frac: Double) -> String {
        String(format: "%.0f%%", frac * 100)
    }

    /// "（3/12 万个文件）"-style secondary count, or "" when no total is known.
    private func filesSuffix(_ p: Progress) -> String {
        if let total = p.totalFiles, total > 0 {
            return "，\(p.filesDone)/\(total) 个文件"
        }
        return p.filesDone > 0 ? "，已传 \(p.filesDone) 个文件" : ""
    }

    /// The "is it stuck?" line. The byte/file counters advance in JUMPS (rsync
    /// block-buffers itemize output; a batch is up to 500 files), so a frozen
    /// number is normal during a healthy slow transfer. A wall-clock "last
    /// transfer N ago" plus the coarse batch counter always move, which is what
    /// actually answers the question. Silent past 3 minutes → the caption says
    /// so plainly rather than implying everything is fine.
    @ViewBuilder
    private func activityCaption(_ p: Progress) -> some View {
        if let stamp = parseISO(p.lastTransferAt) {
            let age = max(Date().timeIntervalSince(stamp), 0)
            HStack(spacing: 6) {
                Text("最近传输：\(agoString(age))")
                if let bd = p.batchesDone, let bt = p.batchesTotal, bt > 0 {
                    Text("· 第 \(min(bd + 1, bt))/\(bt) 批")
                }
            }
            .font(.caption)
            .foregroundColor(age > 180 ? .orange : .secondary)
        }
    }

    /// Chinese relative age for the activity caption.
    private func agoString(_ seconds: TimeInterval) -> String {
        let s = Int(seconds)
        if s < 60 { return "\(s) 秒前" }
        if s < 3600 { return "\(s / 60) 分钟前" }
        return "\(s / 3600) 小时前"
    }

    /// "（X/Y GB）" when both byte counters are present, else empty.
    private func bytesPair(_ p: Progress) -> String {
        guard p.hasBytes, let bd = p.bytesDone, let bt = p.bytesTotal else { return "" }
        return "（\(gbString(bd))/\(gbString(bt)) GB）"
    }

    @ViewBuilder
    private func folderCaption(_ p: Progress) -> some View {
        if !p.folder.isEmpty {
            Text("同步项：\(p.folder)").font(.caption).foregroundColor(.secondary)
        }
    }

    @ViewBuilder
    private func currentCaption(_ p: Progress) -> some View {
        Text(truncateMiddle(p.current))
            .font(.caption).foregroundColor(.secondary)
            .lineLimit(1).truncationMode(.middle)
    }

    /// 1-based elapsed minute since the pass started (第 1 分钟 covers 0–59s).
    /// Guards clock skew so it never shows a zero/negative minute.
    private func scanMinute(_ p: Progress) -> Int {
        guard let start = parseISO(p.startedAt) else { return 1 }
        let elapsed = Date().timeIntervalSince(start)
        if elapsed < 0 { return 1 }
        return Int(elapsed / 60) + 1
    }

    private var actionButtons: some View {
        HStack(spacing: 12) {
            Button {
                Task { await app.syncNow() }
            } label: { Label("立即同步", systemImage: "arrow.clockwise") }
            Button {
                Task { await app.pause() }
            } label: { Label("暂停", systemImage: "pause.circle") }
            Button {
                Task { await app.resume() }
            } label: { Label("恢复", systemImage: "play.circle") }
            if app.isRunning("pause") || app.isRunning("resume") {
                ProgressView().controlSize(.small)
            }
        }
        .disabled(!app.installed || app.anyBusy)
    }

    @ViewBuilder
    private var activitySection: some View {
        Text("最近活动").font(.headline)
        if journal.isEmpty {
            Text("暂无同步记录。").font(.callout).foregroundColor(.secondary)
        } else {
            VStack(spacing: 0) {
                ForEach(journal) { e in
                    activityRow(e)
                    Divider()
                }
            }
        }
    }

    @ViewBuilder
    private func activityRow(_ e: JournalEntry) -> some View {
        HStack(spacing: 10) {
            if !e.event.isEmpty {
                Image(systemName: "info.circle").foregroundColor(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(eventLabel(e.event)).font(.callout)
                    Text(relativeTimeCN(e.ts)).font(.caption).foregroundColor(.secondary)
                }
                Spacer()
            } else {
                Image(systemName: e.isFile ? "doc" : "folder")
                    .foregroundColor(.secondary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(e.folder.isEmpty ? "（未命名）" : e.folder).font(.callout)
                    Text(relativeTimeCN(e.ts)).font(.caption).foregroundColor(.secondary)
                }
                Spacer()
                Text("复制 \(e.copied) · 归档 \(e.trashed)")
                    .font(.caption).foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 6)
    }

    private func eventLabel(_ ev: String) -> String {
        switch ev {
        case "dest_changed": return "目标目录已变更"
        case "capability_fingerprint_changed": return "能力指纹变化，删除传播已停用"
        default: return ev
        }
    }
}

// MARK: - Onboarding (未安装守护进程)

/// The install wizard: everything install.sh did, from the GUI (2026-09-12).
/// Three steps on one page -- environment, NAS connection, backup sources --
/// then 安装并启动. The password goes to ctl on stdin only. Full Disk Access
/// (the one step macOS forbids automating) is guided afterwards from 概览.
struct OnboardingView: View {
    @EnvironmentObject var app: AppState
    @State private var host = "192.168.5.1"
    @State private var share = "SHARE"
    @State private var anonymous = false
    @State private var username = "admin"
    @State private var password = ""
    @State private var folders: [String] = OnboardingView.defaultFolders()

    /// iCloud Drive root when it exists (the gateway case), else 文稿 + 桌面.
    static func defaultFolders() -> [String] {
        let home = NSHomeDirectory()
        let icloud = home + "/Library/Mobile Documents/com~apple~CloudDocs"
        if FileManager.default.fileExists(atPath: icloud) { return [icloud] }
        return [home + "/Documents", home + "/Desktop"]
    }

    private var trimmed: (host: String, share: String, user: String) {
        (host.trimmingCharacters(in: .whitespaces),
         share.trimmingCharacters(in: .whitespaces),
         username.trimmingCharacters(in: .whitespaces))
    }

    private var canInstall: Bool {
        let t = trimmed
        guard !t.host.isEmpty, !t.share.isEmpty, !folders.isEmpty else { return false }
        guard app.cltInstalled != false, app.hasBundledBackend else { return false }
        return anonymous || (!t.user.isEmpty && !password.isEmpty)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                stepEnvironment
                stepConnection
                stepFolders
                stepInstall
            }
            .padding(28)
            .frame(maxWidth: 640, alignment: .leading)
        }
        .onAppear { Task { @MainActor in await app.checkEnvironment() } }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "icloud").font(.system(size: 40)).foregroundColor(.accentColor)
            VStack(alignment: .leading, spacing: 4) {
                Text("欢迎使用 MyCloud").font(.title2).bold()
                Text("把这台 Mac 上的文件持续备份到局域网 NAS。按下面三步完成安装，全程无需终端。")
                    .foregroundColor(.secondary)
            }
        }
    }

    private var stepEnvironment: some View {
        GroupBox(label: Text("1 · 环境")) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    switch app.cltInstalled {
                    case nil:
                        ProgressView().controlSize(.small)
                        Text("正在检查命令行工具…").foregroundColor(.secondary)
                    case .some(true):
                        Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
                        Text("命令行工具已安装（python3 与 rsync 可用）")
                    case .some(false):
                        Image(systemName: "xmark.circle.fill").foregroundColor(.red)
                        Text("缺少命令行工具（Command Line Tools）")
                        Button("安装命令行工具…") {
                            Task { @MainActor in
                                await app.installCommandLineTools()
                            }
                        }
                        Button("重新检查") { Task { @MainActor in await app.checkEnvironment() } }
                    }
                }
                if app.cltInstalled == false {
                    Text("系统会弹出安装对话框；装完后点「重新检查」。")
                        .font(.caption).foregroundColor(.secondary)
                }
                if !app.hasBundledBackend {
                    Label("此应用包内没有后端文件，请使用 build_app.sh 生成的 MyCloud.app。",
                          systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundColor(.orange)
                }
            }
            .padding(6)
        }
    }

    private func field<F: View>(_ label: String, @ViewBuilder _ content: () -> F) -> some View {
        HStack {
            Text(label).frame(width: 92, alignment: .trailing)
            content()
                .textFieldStyle(.roundedBorder)
                .disabled(app.anyBusy)
        }
    }

    private var stepConnection: some View {
        GroupBox(label: Text("2 · 云盘连接")) {
            VStack(alignment: .leading, spacing: 8) {
                field("服务器地址") { TextField("例如 192.168.5.1", text: $host) }
                field("共享名") { TextField("例如 SHARE", text: $share) }
                Picker("连接模式", selection: $anonymous) {
                    Text("注册用户（用户名 + 密码）").tag(false)
                    Text("匿名（guest，无需密码）").tag(true)
                }
                .frame(maxWidth: 380)
                if !anonymous {
                    field("用户名") { TextField("例如 admin", text: $username) }
                    field("密码") { SecureField("必填", text: $password) }
                }
                Text(anonymous
                     ? "匿名模式使用 SMB guest 会话，NAS 需允许该共享的匿名访问。"
                     : "密码只经本机内部管道传给后台安装程序，保存在登录钥匙串与 nsmb.conf（权限 600）；不会出现在命令行参数或日志中。安装前会先用它实测连接。")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(6)
        }
    }

    private var stepFolders: some View {
        GroupBox(label: Text("3 · 备份内容")) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(folders, id: \.self) { path in
                    HStack {
                        Image(systemName: "folder").foregroundColor(.accentColor)
                        Text(truncateMiddle(path)).lineLimit(1)
                        Spacer()
                        Button {
                            folders.removeAll { $0 == path }
                        } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.plain)
                        .disabled(app.anyBusy)
                    }
                }
                if folders.isEmpty {
                    Text("至少添加一个要备份的文件夹。").font(.caption).foregroundColor(.orange)
                }
                Button {
                    addFolder()
                } label: { Label("添加文件夹…", systemImage: "folder.badge.plus") }
                .disabled(app.anyBusy)
                Text("默认建议 iCloud 云盘根目录。作为常开网关机使用时，请在 Apple ID → iCloud 中关闭「优化 Mac 储存空间」，新文件才会自动下载并备份。")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(6)
        }
    }

    private var stepInstall: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Button {
                    let t = trimmed
                    let secret = password
                    Task { @MainActor in
                        await app.install(host: t.host, share: t.share, anonymous: anonymous,
                                          username: t.user, password: secret, folders: folders)
                        password = ""
                    }
                } label: {
                    if app.isRunning("install") {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("正在验证连接并启动守护进程（通常 1–2 分钟）…")
                        }
                    } else {
                        Label("安装并启动", systemImage: "play.circle.fill")
                    }
                }
                .disabled(!canInstall || app.anyBusy)
                .keyboardShortcut(.defaultAction)
                Spacer()
                Button("打开 README") { openProjectDoc("README.md") }
            }
            Text("安装内容：后台守护进程（登录时自启）、每日健康看门狗、配置文件。所有文件位于 ~/Library/Application Support/MyCloud，可在设置页随时卸载。")
                .font(.caption).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func addFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.prompt = "添加"
        panel.message = "选择要备份的文件夹"
        guard panel.runModal() == .OK else { return }
        for url in panel.urls where !folders.contains(url.path) {
            folders.append(url.path)
        }
    }
}

// MARK: - Menu bar quick panel

struct MenuBarContent: View {
    @EnvironmentObject var app: AppState
    /// Set by StatusItemController so button taps close the NSPopover — the
    /// auto-closing behavior of the MenuBarExtra menu this panel replaced.
    /// (@Environment(\.openWindow) was macOS 13+; window opening now goes
    /// through openMainWindow() in StatusItem.swift.)
    var dismiss: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("状态：\(stateLabel(app.displayState, installed: app.installed))")
                .font(.callout)
            // Progress line keeps a constant-height placeholder when idle:
            // macOS 12's NSPopover does not reliably track NSHostingController
            // height changes, so the panel must not grow/shrink while open.
            Text(app.progressLive
                    ? menuProgressLine(app.status?.progress ?? Progress())
                    : " ")
                .font(.caption)
                .foregroundColor(.secondary)
            // Last failure, fixed height for the same reason: an action started
            // from this panel closes it, so its failure is shown here next time.
            Text(app.lastError.map { "上次操作失败：\($0)" } ?? " ")
                .font(.caption)
                .foregroundColor(.red)
                .lineLimit(2)
                .frame(height: 30, alignment: .topLeading)
            Divider()
            Button("立即同步") {
                dismiss?()
                Task { await app.syncNow() }
            }
            .disabled(!app.installed || app.anyBusy)
            Button("打开 MyCloud") {
                dismiss?()
                openMainWindow()
            }
            Divider()
            Button("退出") { NSApplication.shared.terminate(nil) }
        }
        .padding(14)
        .frame(width: 240, alignment: .leading)
    }
}

/// A compact one-line sync summary for the menu bar dropdown (phase-aware).
func menuProgressLine(_ p: Progress) -> String {
    if p.isPlanning { return "正在计算变更…" }
    if p.isScanning { return "正在扫描文件清单…" }   // legacy phase
    if p.isVerifying { return "正在深度校验备份…" }
    if p.isDownloading {
        if let t = p.totalFiles, t > 0 { return "正在下载云端文件 \(p.filesDone)/\(t)" }
        return "正在下载云端文件（已下载 \(p.filesDone)）"
    }
    // transferring or empty/unknown phase.
    if let total = p.totalFiles, total > 0 {
        return "备份中 \(p.folder)（\(p.filesDone)/\(total)）"
    }
    return "备份中 \(p.folder)（已传输 \(p.filesDone)）"
}

/// systemImage for the menu-bar icon, driven by current status.
/// `stale` (daemon unresponsive, a stalled transfer, an exited daemon) and
/// `progressLive` (a sync genuinely moving) come from AppState so a killed
/// daemon never leaves a spinning/idle icon. States are WHITELISTED: an
/// unknown one (STARTING/RESTARTING used to be) must never show a green check.
/// The attention row's sentence; never a bare "。".
func attentionSentence(_ parts: [String]) -> String {
    parts.isEmpty ? "有需要处理的问题。" : parts.joined(separator: "；") + "。"
}

func menuBarSymbol(for status: Status?, installed: Bool,
                   stale: Bool = false, progressLive: Bool = false) -> String {
    guard installed, let s = status else { return "icloud.slash" }
    if stale { return "exclamationmark.icloud" }
    let state = s.state.uppercased()
    switch state {
    case "OFFLINE", "STOPPED", "PAUSED": return "icloud.slash"
    // 2019 symbol; the 2023 triangle-circlepath variant blanked the menu
    // bar item on macOS 12/13 for the whole duration of every sync.
    case "STARTING", "RESTARTING": return "arrow.clockwise.icloud"
    case "MOUNTED_IDLE", "SYNCING", "DELETIONS_HELD", "DEGRADED": break
    default: return "icloud"
    }
    if progressLive || state == "SYNCING" {
        return "arrow.clockwise.icloud"
    }
    if s.deletionsHeld || s.totalFailedFiles > 0
        || s.watcherDegraded || s.deletionDisabled
        || !s.orphanedFolders.isEmpty || !(s.lastError ?? "").isEmpty {
        return "exclamationmark.icloud"
    }
    return "checkmark.icloud"
}
