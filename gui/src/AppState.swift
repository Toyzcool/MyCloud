// AppState.swift — the single observable the whole UI binds to.
//
// Reads are cheap: a 1s Timer polls runtime/state.json directly (no subprocess).
// Writes go through the ctl contract (Bridge.runCtl); config is refreshed after
// every mutation so the UI reflects exactly what the backend committed.
// Failures land in `lastError`, informational receipts in `notice` -- two
// channels, so a success can never clear (or pass for) a failure.

import AppKit
import Foundation
import SwiftUI

/// Outcome of a one-click ctl `fix` shown inline next to the control that ran it
/// (green when ok, red otherwise). Kept UI-facing and separate from FixResult so
/// the view layer never touches the raw JSON envelope.
struct FixOutcome {
    var ok: Bool
    var detail: String
}

/// Liveness budgets, matched to how often the daemon really writes state.json.
enum LivenessBudget {
    /// A live pass re-stamps its progress every ~3s.
    static let activeProgress: Double = 30
    /// SYNCING without an active progress block (pre-folder layout checks,
    /// post-pass work), STARTING and RESTARTING: legitimately quiet stretches.
    static let quiet: Double = 600
    /// Idle and offline loops touch state.json every 30s.
    static let idle: Double = 300
    /// A transfer with no visible movement for this long is stalled. Above the
    /// daemon's own 20-minute stall kill: flag only what it failed to recover.
    static let transferStall: Double = 1500
    /// The recorded daemon pid must be gone this long (restart flicker).
    static let pidGrace: Double = 10
    /// After the Mac wakes, state.json is as old as the sleep until the daemon
    /// writes again (seconds, or up to ~1 min while it re-checks the NAS):
    /// not a dead daemon. A dead pid still counts at once.
    static let wakeGrace: Double = 120
}

@MainActor
final class AppState: ObservableObject {
    @Published var status: Status? = nil
    @Published var config: Config? = nil
    @Published var doctor: [DoctorRow] = []
    /// A failed operation (shown as 「操作失败」).
    @Published var lastError: String? = nil
    /// An informational receipt (shown as 「提示」). Never touches lastError.
    @Published var notice: String? = nil
    @Published var installed: Bool = Bridge.isInstalled
    /// Sidebar selection; lives here so a banner can jump to 问题.
    @Published var selectedSection: AppSection? = .overview

    /// mtime of state.json at the last poll (drives the staleness heuristic).
    @Published var stateMtime: Date? = Bridge.stateFileMtime()

    /// Which fix is currently running, keyed by call-site (a doctor row id, or
    /// "restart_daemon" for the overview banner). nil when no fix is in flight —
    /// this gives the UI per-row spinner granularity.
    @Published var fixBusyId: String? = nil
    /// Last fix outcome per call-site key, rendered inline where it was invoked.
    @Published var fixResults: [String: FixOutcome] = [:]

    /// In-flight ctl operations by name, depth-counted. A single Bool used to
    /// be cleared by whichever overlapping call finished FIRST (a settings
    /// change during a 4-minute server-set re-enabled every button).
    @Published private(set) var ops: [String: Int] = [:]

    private var timer: Timer? = nil
    private var wakeObserver: NSObjectProtocol? = nil
    /// When the Mac last woke from sleep (NSWorkspace.didWakeNotification).
    @Published private(set) var lastWake: Date? = nil

    private var sleepObserver: NSObjectProtocol? = nil
    private var sleptAt: Date? = nil

    func noteSystemSleep(at date: Date? = nil) {
        sleptAt = date ?? now()
    }

    /// Also pauses the stall clock across the sleep: the daemon's heartbeat
    /// re-stamps the pre-sleep progress snapshot right after a wake, and time
    /// asleep is not a transfer that failed to move -- but awake time without
    /// movement before the sleep still counts (a stall shown before a short
    /// nap must not vanish for another 25 minutes). Without a known sleep
    /// start the clock restarts at the wake.
    func noteSystemWake(at date: Date? = nil) {
        let woke = date ?? now()
        lastWake = woke
        if let slept = sleptAt, woke > slept {
            // Shift by the time asleep; never past the wake (movement seen
            // after the sleep notice then simply counts from the wake).
            progressSeenAt = min(
                progressSeenAt.addingTimeInterval(woke.timeIntervalSince(slept)), woke)
        } else {
            progressKey = ""
            progressSeenAt = woke
        }
        sleptAt = nil
        if syncStalled { updateStall() }
    }

    /// Clock seam for the liveness logic (tests move time instead of waiting).
    var now: () -> Date = { Date() }

    var busy: Bool { !ops.isEmpty }
    func isRunning(_ op: String) -> Bool { ops[op] != nil }
    /// True while any ctl work (a mutation/doctor run, or a fix) is in flight —
    /// used to cross-disable buttons so two ctl calls never race.
    var anyBusy: Bool { busy || fixBusyId != nil }

    private func begin(_ op: String) { ops[op, default: 0] += 1 }
    private func end(_ op: String) {
        guard let n = ops[op] else { return }
        ops[op] = n > 1 ? n - 1 : nil
    }

    // MARK: lifecycle

    /// Begin 1s state polling and load config once (if installed).
    ///
    /// IDEMPOTENT: the RunLoop retains a scheduled Timer, and AppState lives
    /// for the whole process -- an unguarded second start() (ContentView's
    /// onAppear fires for EVERY new window: reopen, ⌘N, ...) would leak one
    /// poller per window forever, each ticking pollTick every second
    /// (supervised review, 2026-08-01).
    func start() {
        pollTick()
        guard timer == nil else { return }
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pollTick() }
        }
        t.tolerance = 0.25
        // .common: keep polling while a menu tracks or an open panel is modal.
        RunLoop.main.add(t, forMode: .common)
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.noteSystemWake() }
        }
        sleepObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.willSleepNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.noteSystemSleep() }
        }
        timer = t
        if installed {
            Task { await reloadConfig() }
            Task { await checkBackendVersion() }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Cheap poll: re-read state.json, its mtime, and refresh the install flag.
    func pollTick() {
        let nowInstalled = Bridge.isInstalled
        if nowInstalled != installed {
            installed = nowInstalled
            if nowInstalled {
                // Became installed (the wizard's install finished, or another
                // tool installed): nothing had loaded config for this state.
                Task {
                    await self.reloadConfig(silent: true)
                    await self.checkBackendVersion()
                }
            } else {
                config = nil
                doctor = []
                backendVersion = nil
                backendOutdated = false
            }
        }
        status = Bridge.readState()
        stateMtime = Bridge.stateFileMtime()
        updateLiveness()
        updateStall()
        if postInstallHint != nil, !unresponsive,
           ["MOUNTED_IDLE", "SYNCING"].contains(status?.state.uppercased() ?? "") {
            postInstallHint = nil
        }
        retryConfigIfNeeded()
        probeAgentIfNeeded()
    }

    // MARK: liveness (state.json can outlive the daemon)

    /// The recorded daemon process is gone although the state it left claims a
    /// running daemon (SIGKILL, crash, OOM): unresponsive within seconds rather
    /// than after the idle budget.
    @Published private(set) var daemonGone = false
    private var pidDeadSince: Date? = nil

    /// Transfer phase with no visible movement past LivenessBudget.transferStall
    /// while the heartbeat keeps state.json fresh -- the "syncing forever"
    /// display a wedged pass used to produce.
    @Published private(set) var syncStalled = false
    private var progressKey = ""
    private var progressSeenAt = Date()

    /// launchd's view while state.json says stopped; nil when not applicable.
    @Published private(set) var agentStatus: Bridge.AgentStatus? = nil
    private var agentLoadedStreak = 0
    private var agentProbeAt = Date.distantPast
    private var agentProbing = false

    /// Age of the last state.json write per the self-reported updated_at, or nil
    /// if unknown/never written. Kept as a fallback for stateFileAgeSeconds.
    var statusAgeSeconds: Double? {
        guard let iso = status?.updatedAt, let d = parseISO(iso) else { return nil }
        return now().timeIntervalSince(d)
    }

    /// Seconds since state.json was last written on disk. Prefers the real file
    /// mtime (the daemon rewrites at least every 3s while syncing / 30s idle) and
    /// falls back to the self-reported updated_at when the mtime is unreadable.
    var stateFileAgeSeconds: Double? {
        if let m = stateMtime { return now().timeIntervalSince(m) }
        return statusAgeSeconds
    }

    /// True while a sync is genuinely progressing: a live pass heartbeats
    /// state.json at least every ~3s, so an "active" progress block whose file
    /// mtime is older than 30s is a frozen leftover (e.g. the daemon was
    /// SIGKILLed mid-pass) and must not be rendered as a moving progress bar.
    var progressLive: Bool {
        guard let p = status?.progress, p.active, !daemonGone,
              let age = stateFileAgeSeconds else { return false }
        return age <= LivenessBudget.activeProgress
    }

    /// The recorded state can no longer be trusted as live. STOPPED/PAUSED are
    /// written deliberately on exit and never time out (a 30-minute pause used
    /// to turn into 「守护进程无响应」); every other state gets a budget matched
    /// to the daemon's real write cadence.
    var unresponsive: Bool {
        guard installed, let s = status else { return false }
        if daemonGone { return true }
        if let w = lastWake, now().timeIntervalSince(w) < LivenessBudget.wakeGrace {
            return false
        }
        guard let age = stateFileAgeSeconds else { return false }
        let st = s.state.uppercased()
        if st == "STOPPED" || st == "PAUSED" { return false }
        if s.progress?.active == true { return age > LivenessBudget.activeProgress }
        if st == "SYNCING" || st == "STARTING" || st == "RESTARTING" {
            return age > LivenessBudget.quiet
        }
        return age > LivenessBudget.idle
    }

    /// state.json says stopped, but launchd still has the agent loaded (seen
    /// twice in a row): the daemon exited on its own -- a crash loop, or it gave
    /// up -- rather than being paused.
    var agentExited: Bool { agentLoadedStreak >= 2 }

    /// The folders' problems that a green 「空闲」 must not hide (the daemon
    /// writes MOUNTED_IDLE after every pass, whatever it left held or failed),
    /// and the reason a pass failed (last_error: e.g. rsync rejecting the
    /// upload command, which keeps the mount and retries on a backoff).
    var needsAttention: Bool {
        guard let s = status else { return false }
        return s.deletionsHeld || s.totalFailedFiles > 0 || s.deletionDisabled
            || !(s.lastError ?? "").isEmpty
    }

    /// What the badge and the menu bar say: the raw state folded together with
    /// what it cannot know by itself.
    var displayState: String {
        guard installed, let s = status else { return "OFFLINE" }
        if unresponsive { return "UNRESPONSIVE" }
        let st = s.state.uppercased()
        if st == "STOPPED" || st == "PAUSED" {
            if agentExited { return "EXITED" }
            if agentStatus?.loaded == false { return "PAUSED" }
            return "STOPPED"
        }
        if syncStalled { return "STALLED" }
        if st == "MOUNTED_IDLE" && needsAttention { return "ATTENTION" }
        return st
    }

    private func updateLiveness() {
        var dead = false
        if installed, let s = status, let pid = s.daemonPid, pid > 0 {
            let st = s.state.uppercased()
            if !["STOPPED", "PAUSED", "RESTARTING"].contains(st) {
                dead = !Bridge.processAlive(pid)
            }
        }
        if dead {
            if pidDeadSince == nil { pidDeadSince = now() }
        } else {
            pidDeadSince = nil
        }
        let gone = pidDeadSince.map { now().timeIntervalSince($0) >= LivenessBudget.pidGrace }
            ?? false
        if gone != daemonGone { daemonGone = gone }
    }

    private func updateStall() {
        guard let p = status?.progress, p.active, p.isTransferring, !daemonGone else {
            if syncStalled { syncStalled = false }
            progressKey = ""
            return
        }
        guard progressLive else {
            // Only the state file is stale (e.g. the first polls after a wake,
            // before the daemon writes again; a dead daemon is "unresponsive").
            // Keep the baseline: dropping it restarted the stall clock.
            if syncStalled { syncStalled = false }
            return
        }
        let key = [p.startedAt ?? "", p.folder, "\(p.filesDone)", "\(p.bytesDone ?? -1)",
                   "\(p.batchesDone ?? -1)", p.current, p.lastTransferAt ?? ""]
            .joined(separator: "|")
        if key != progressKey {
            progressKey = key
            progressSeenAt = now()
        }
        let moved = max(parseISO(p.lastTransferAt) ?? .distantPast, progressSeenAt)
        let stalled = now().timeIntervalSince(moved) > LivenessBudget.transferStall
        if stalled != syncStalled { syncStalled = stalled }
    }

    /// Ask launchd whether the agent is loaded, only while state.json says
    /// stopped: every 30s, or 5s after a "loaded" answer (a pause's bootout
    /// can still be unloading when the daemon's final write lands).
    private func probeAgentIfNeeded() {
        let st = status?.state.uppercased() ?? ""
        guard installed, st == "STOPPED" || st == "PAUSED" else {
            if agentStatus != nil { agentStatus = nil }
            agentLoadedStreak = 0
            return
        }
        let interval: Double = agentLoadedStreak > 0 ? 5 : 30
        guard !agentProbing, now().timeIntervalSince(agentProbeAt) >= interval else { return }
        agentProbing = true
        agentProbeAt = now()
        Task {
            let result = await Bridge.agentStatus()
            self.agentProbing = false
            let still = self.status?.state.uppercased() ?? ""
            guard still == "STOPPED" || still == "PAUSED" else { return }
            self.agentStatus = result
            if let r = result, r.loaded {
                self.agentLoadedStreak += 1
            } else {
                self.agentLoadedStreak = 0
            }
        }
    }

    // MARK: config

    private var lastConfigAttempt = Date.distantPast
    private var configLoading = false

    /// Refresh config via the ctl contract. `silent`: a background retry, whose
    /// failure must not raise an alert every 15 seconds.
    func reloadConfig(silent: Bool = false) async {
        configLoading = true
        lastConfigAttempt = now()
        defer { configLoading = false }
        do {
            config = try await Bridge.runCtlDecoding(Config.self, ["config-get", "--json"])
        } catch {
            if !silent { lastError = describe(error) }
        }
    }

    /// Config was loaded once at start and never again: one failed attempt (a
    /// cold python3 at login, config.ini mid-replace) left 设置/同步项 on
    /// 「加载配置中…」 until the app restarted.
    private func retryConfigIfNeeded() {
        guard installed, config == nil, !configLoading,
              now().timeIntervalSince(lastConfigAttempt) > 15 else { return }
        Task { await self.reloadConfig(silent: true) }
    }

    // MARK: reporting

    /// A readable message for a thrown Bridge error.
    private func describe(_ error: Error) -> String {
        if case BridgeError.timedOut = error {
            return "后端调用超时，且在宽限期内没有退出（可能卡在 NAS 读写上）。"
                + "请在「概览」确认守护进程状态，必要时点「恢复」或「重启守护进程」。"
        }
        return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    /// Show a receipt -- unless nobody can see it: with every window closed
    /// (popover-driven use on the gateway) it would greet the user hours later
    /// as a stale message. Failures are always kept.
    private func showNotice(_ text: String?) {
        guard let text = text, !text.isEmpty else { return }
        if NSApp.windows.contains(where: { $0.isVisible && $0.canBecomeMain }) {
            notice = text
        }
    }

    /// After a timeout the backend may have stopped the daemon, rewritten the
    /// config, or both: re-read everything instead of showing stale values.
    private func refreshAfterTimeout() async {
        pollTick()
        await reloadConfig(silent: true)
    }

    private func isTimeout(_ error: Error) -> Bool {
        if case BridgeError.timedOut = error { return true }
        return false
    }

    /// Build a user-facing message for a ctl call that exited non-zero.
    /// Prefers the backend's own {"error":...}; otherwise shows what it wrote
    /// to stderr (an argparse "invalid choice" for an outdated backend, a
    /// traceback for a crash) and points at the update button when relevant.
    private func ctlFailure(_ run: Bridge.CtlRun, fallback: String) -> String {
        var text: String
        if let env = try? JSONDecoder().decode(ServerSetEnvelope.self, from: run.data),
           let msg = env.error, !msg.isEmpty {
            text = msg
        } else {
            text = fallback
            if let tail = run.errTail { text += "\n后端输出：\(tail)" }
            if backendOutdated {
                text += "\n已安装的后端（\(backendVersion ?? "版本未知")）比本应用（\(appVersion)）旧，"
                    + "请在「设置 → 守护进程」点「更新后端」后重试。"
            }
        }
        return timeoutNoted(run, text)
    }

    /// The deadline passed and the backend was asked to stop (it still got to
    /// run its cleanup): say so, whatever it printed.
    private func timeoutNoted(_ run: Bridge.CtlRun, _ text: String) -> String {
        guard run.timedOut else { return text }
        return "操作超时，已请求后端中止并收尾。\n" + text
            + "\n请在「概览」确认守护进程状态（必要时点「恢复」）。"
    }

    // MARK: mutations

    /// Run a config-mutating ctl command; on success apply the returned Config
    /// (and surface its `notice`), then poll state immediately. Returns whether
    /// the backend accepted it.
    @discardableResult
    private func mutate(_ args: [String], op: String = "config") async -> Bool {
        begin(op)
        defer { end(op) }
        do {
            let cfg = try await Bridge.runCtlDecoding(Config.self, args)
            config = cfg
            showNotice(cfg.notice)
            pollTick()
            return true
        } catch {
            lastError = describe(error)
            if isTimeout(error) { await refreshAfterTimeout() }
            return false
        }
    }

    /// Run a ctl action and ALWAYS show its reply to the user.
    ///
    /// These commands SUCCEED (status 0) while printing the part the user
    /// actually needs -- "already syncing, request queued", "已在运行，无需恢复".
    /// Discarding that stdout is why clicking 立即同步 (2026-07-28) and 恢复
    /// (2026-07-31) looked like they did nothing.
    private func runAndReport(_ args: [String], op: String, timeout: TimeInterval,
                              fallbackOK: String, fallbackFail: String) async {
        begin(op)
        defer { end(op) }
        do {
            let run = try await Bridge.runCtl(args, timeout: timeout)
            let text = (String(data: run.data, encoding: .utf8) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if run.status != 0 {
                if let env = try? JSONDecoder().decode(CtlError.self, from: run.data),
                   let msg = env.error, !msg.isEmpty {
                    lastError = timeoutNoted(run, msg)
                } else if text.isEmpty {
                    lastError = ctlFailure(run, fallback: fallbackFail)
                } else {
                    lastError = timeoutNoted(run, text)
                }
                if run.timedOut { await refreshAfterTimeout() }
            } else {
                showNotice(text.isEmpty ? fallbackOK : text)
            }
            pollTick()
        } catch {
            lastError = describe(error)
            if isTimeout(error) { await refreshAfterTimeout() }
        }
    }

    func syncNow() async {
        await runAndReport(["sync-now"], op: "sync-now", timeout: 30,
                           fallbackOK: "已触发立即同步。",
                           fallbackFail: "无法立即同步：守护进程可能未在运行。")
    }
    // pause/resume wait for the daemon to stop/start (resume: bootstrap and
    // kickstart are 30s each, plus a liveness wait): 30s cut healthy slow
    // restarts off as failures.
    func pause() async {
        await runAndReport(["pause"], op: "pause", timeout: 150,
                           fallbackOK: "已暂停。",
                           fallbackFail: "无法暂停 MyCloud（launchctl 失败）。")
    }
    func resume() async {
        await runAndReport(["resume"], op: "resume", timeout: 150,
                           fallbackOK: "已恢复。",
                           fallbackFail: "无法恢复 MyCloud（launchctl 失败）。")
    }

    func addSource(url: URL) async {
        await mutate(["config-add-source", url.path])
    }

    func removeSource(path: String) async {
        await mutate(["config-remove-source", path])
    }

    func setDest(rel: String) async {
        await mutate(["config-set-dest", rel])
    }

    @discardableResult
    func setSetting(key: String, value: String) async -> Bool {
        await mutate(["config-set", key, value])
    }

    /// A server-set (or install) that stopped at "the share carries a different
    /// sentinel": the request is parked here until the user confirms adoption
    /// or cancels. `folders` is non-nil for the install flow.
    struct PendingAdopt {
        let message: String
        let foundSentinel: String
        let host: String
        let share: String
        let anonymous: Bool
        let username: String
        let password: String
        var folders: [String]? = nil
    }
    @Published var pendingAdopt: PendingAdopt? = nil

    // MARK: install / uninstall (GUI-driven; no terminal)

    /// nil = not checked yet; false = Command Line Tools (python3/rsync) missing.
    @Published var cltInstalled: Bool? = nil
    /// Set after an install that finished but whose daemon did not come online
    /// (typically: Full Disk Access not granted yet). Drives the 概览 banner;
    /// cleared by pollTick once the daemon is online.
    @Published var postInstallHint: String? = nil

    var hasBundledBackend: Bool { Bridge.bundledBackendDir != nil }

    /// Version of the backend actually installed under Application Support,
    /// and whether it is OLDER than the one bundled in this app. A backend older
    /// than the app lacks newer subcommands, and the failure looks like
    /// "后端未返回可解析的结果" -- so the GUI detects it and offers the one-click
    /// refresh (2026-09-12 field report). A NEWER backend is left alone: the
    /// old `!=` test recommended "updating" it, i.e. a downgrade.
    @Published var backendVersion: String? = nil
    @Published var backendOutdated = false

    var appVersion: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "—"
    }

    /// Numeric dotted-version comparison: -1, 0 or 1. Non-numeric parts count
    /// as 0 ("1.2" == "1.2.0").
    nonisolated static func compareVersions(_ a: String, _ b: String) -> Int {
        func parts(_ s: String) -> [Int] {
            s.split(separator: ".").map { comp in
                Int(comp.prefix(while: { $0.isNumber })) ?? 0
            }
        }
        let x = parts(a), y = parts(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0
            let r = i < y.count ? y[i] : 0
            if l != r { return l < r ? -1 : 1 }
        }
        return 0
    }

    func checkBackendVersion() async {
        guard installed, hasBundledBackend else {
            backendVersion = nil; backendOutdated = false; return
        }
        do {
            let run = try await Bridge.runCtl(["version"], timeout: 20)
            if run.status != 0 {
                // Too old to even know the command: definitively outdated.
                backendVersion = nil; backendOutdated = true; return
            }
            let info = try? JSONDecoder().decode(VersionInfo.self, from: run.data)
            let v = info?.backendVersion ?? ""
            backendVersion = v.isEmpty ? nil : v
            backendOutdated = v.isEmpty || AppState.compareVersions(v, appVersion) < 0
        } catch {
            backendVersion = nil
            backendOutdated = false   // could not ask; do not cry wolf
        }
    }

    /// Copy the app's bundled backend over the installed one and restart the
    /// daemon, keeping every connection setting and backup source as they are.
    func refreshBackend() async {
        guard let backend = Bridge.bundledBackendDir else {
            lastError = "此应用包内没有后端文件，无法更新。"
            return
        }
        begin("refresh")
        defer { end("refresh") }
        do {
            let run = try await Bridge.runCtl(["install", "--from", backend, "--refresh"],
                                              timeout: 300, ctl: backend + "/mycloudctl.py")
            if run.status != 0 {
                lastError = ctlFailure(run, fallback: "更新后端失败。")
                if run.timedOut { await refreshAfterTimeout() }
                return
            }
            let result = (try? JSONDecoder().decode(InstallResult.self, from: run.data))
                ?? InstallResult()
            showNotice(result.notice ?? "后端已更新，守护进程已重启。")
            pollTick()
            await checkBackendVersion()
            await reloadConfig()
        } catch {
            lastError = describe(error)
            if isTimeout(error) { await refreshAfterTimeout() }
        }
    }

    /// `xcode-select -p` succeeds only with the Command Line Tools (or Xcode)
    /// installed -- that is where /usr/bin/python3 and rsync come from.
    func checkEnvironment() async {
        do {
            let run = try await Bridge.runProcess("/usr/bin/xcode-select", ["-p"], timeout: 15)
            cltInstalled = (run.status == 0)
        } catch {
            cltInstalled = false
        }
    }

    /// Pops the system "Install the command line developer tools?" dialog.
    func installCommandLineTools() async {
        _ = try? await Bridge.runProcess("/usr/bin/xcode-select", ["--install"], timeout: 15)
    }

    /// Run the bundled ctl's `install`: lays out Application Support, copies the
    /// backend from the bundle, configures + verifies the NAS connection
    /// (password on stdin only), renders and loads the LaunchAgents. On
    /// success `installed` flips on the next poll and the main UI appears.
    func install(host: String, share: String, anonymous: Bool, username: String,
                 password: String, folders: [String], adopt: Bool = false) async {
        guard let backend = Bridge.bundledBackendDir else {
            lastError = "此应用包内没有后端文件，无法安装。请使用 build_app.sh 生成的 MyCloud.app。"
            return
        }
        var args = ["install", "--from", backend, "--host", host, "--share", share,
                    "--mode", anonymous ? "anonymous" : "user"]
        let secret: String? = (!anonymous && !password.isEmpty) ? password : nil
        if !anonymous {
            args += ["--username", username]
            if secret != nil { args.append("--password-stdin") }
        }
        for f in folders { args += ["--folder", f] }
        if adopt { args.append("--adopt") }
        begin("install")
        defer { end("install") }
        do {
            let run = try await Bridge.runCtl(args, timeout: 420, stdin: secret,
                                              ctl: backend + "/mycloudctl.py")
            let dec = JSONDecoder()
            if run.status != 0 {
                let env = try? dec.decode(ServerSetEnvelope.self, from: run.data)
                if env?.needsConfirm == "adopt" && !run.timedOut {
                    pendingAdopt = PendingAdopt(
                        message: env?.error ?? "", foundSentinel: env?.foundSentinel ?? "",
                        host: host, share: share, anonymous: anonymous,
                        username: username, password: password, folders: folders)
                } else {
                    lastError = ctlFailure(run, fallback: "安装失败。")
                }
                pollTick()
                return
            }
            let result = (try? dec.decode(InstallResult.self, from: run.data)) ?? InstallResult()
            postInstallHint = (result.online == nil)
                ? "守护进程已安装但尚未上线——请按下面的提示授予「完全磁盘访问」，然后重启守护进程。"
                : nil
            showNotice(result.notice)
            pollTick()
            if installed {
                await reloadConfig()
                await checkBackendVersion()
            }
        } catch {
            lastError = describe(error)
            pollTick()
        }
    }

    /// Run the installed ctl's `uninstall --yes`: agents, plists and the app dir
    /// go (config + logs backed up); the NAS is never touched. The GUI falls
    /// back to the install wizard on the next poll.
    func uninstall() async {
        begin("uninstall")
        defer { end("uninstall") }
        do {
            let run = try await Bridge.runCtl(["uninstall", "--yes"], timeout: 180)
            let dec = JSONDecoder()
            if run.status != 0 {
                lastError = ctlFailure(run, fallback: "卸载失败。")
                pollTick()
                return
            }
            if let cfg = try? dec.decode(Config.self, from: run.data) {
                showNotice(cfg.notice)
            }
            config = nil
            postInstallHint = nil
            pollTick()
        } catch {
            lastError = describe(error)
            pollTick()
        }
    }

    // MARK: iCloud materialisation (runs HERE, in the user session)

    /// Live progress line while `materialize` runs ("已下载 120/967（3.1 GB）").
    @Published var materializeProgress: String? = nil
    /// A download is running. Deliberately NOT `busy`: it can take hours, and
    /// only uninstall / backend refresh / a second download must wait for it.
    @Published private(set) var materializing = false
    private var materializeGen = 0
    private var materializeHandle: Bridge.RunHandle? = nil

    /// Download the cloud-only iCloud files so the daemon can back them up.
    ///
    /// This MUST run in the GUI's (user session) context: a launchd daemon
    /// cannot trigger iCloud downloads -- fileproviderd refuses and every file
    /// stalls for 120s (PLAN 14, field-verified).
    func materializeCloudFiles() async {
        guard !materializing else { return }
        materializeGen += 1
        let gen = materializeGen
        let handle = Bridge.RunHandle()
        materializeHandle = handle
        materializing = true
        materializeProgress = "正在扫描仅云端文件…"
        defer {
            materializing = false
            materializeProgress = nil
            materializeHandle = nil
        }
        do {
            // [self]: AppState lives as long as the app; the explicit strong
            // capture only lasts for this run's callbacks.
            let run = try await Bridge.runCtl(["materialize"], timeout: 6 * 3600,
                                              handle: handle) { [self] line in
                let l = line.trimmingCharacters(in: .whitespaces)
                guard !l.isEmpty else { return }
                Task { @MainActor [weak self] in
                    // A late line must not resurrect the spinner after the run.
                    guard let self = self, self.materializeGen == gen,
                          self.materializing else { return }
                    self.materializeProgress = l
                }
            }
            if handle.isCancelled {
                showNotice("已取消下载。已下载完成的文件会照常备份。")
                pollTick()
                return
            }
            let text = (String(data: run.data, encoding: .utf8) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            // materialize exits 1 when some files failed; its own report is the
            // message either way.
            if run.status == 0 {
                showNotice(text.isEmpty ? "下载完成。" : text)
            } else {
                lastError = text.isEmpty
                    ? ctlFailure(run, fallback: "下载未能完成，请查看日志。")
                    : timeoutNoted(run, text)
            }
            pollTick()
        } catch {
            lastError = describe(error)
        }
    }

    func cancelMaterialize() {
        materializeHandle?.cancel()
    }

    /// Reveal the log folder (the GUI equivalent of `mycloudctl logs`).
    func openLogFolder() {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MyCloud", isDirectory: true)
        NSWorkspace.shared.open(dir)
    }

    /// Full Disk Access cannot be granted programmatically (TCC); open the pane
    /// directly and let the user drop python3 in.
    func openFullDiskAccessSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
            NSWorkspace.shared.open(url)
        }
    }

    /// Reveal /usr/bin/python3 in Finder (selected) so it can be dragged into
    /// the Full Disk Access list -- the `open -R` trick, one click.
    func revealPython3InFinder() {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: "/usr/bin/python3")])
    }

    /// 云盘连接 (设置): host / share + auth mode. In registered-user mode the
    /// password is piped to ctl on STDIN -- never an argument -- and ctl stores
    /// it (login Keychain + nsmb.conf 0600) only AFTER the verification mount
    /// succeeded; this layer never persists it. An empty password keeps the
    /// credential ctl already has. Worst case ctl stops the daemon (45s),
    /// releases its mount, verifies (60s), lists shares and restarts the daemon
    /// (bootstrap + kickstart + liveness wait) -- hence 360s.
    func setServer(host: String, share: String, anonymous: Bool,
                   username: String, password: String, adopt: Bool = false) async {
        var args = ["server-set", "--host", host, "--share", share,
                    "--mode", anonymous ? "anonymous" : "user"]
        let secret: String? = (!anonymous && !password.isEmpty) ? password : nil
        if !anonymous {
            args += ["--username", username]
            if secret != nil { args.append("--password-stdin") }
        }
        if adopt { args.append("--adopt") }
        begin("server-set")
        defer { end("server-set") }
        do {
            let run = try await Bridge.runCtl(args, timeout: 360, stdin: secret)
            let dec = JSONDecoder()
            if run.status != 0 {
                let env = try? dec.decode(ServerSetEnvelope.self, from: run.data)
                if env?.needsConfirm == "adopt" && !run.timedOut {
                    pendingAdopt = PendingAdopt(
                        message: env?.error ?? "", foundSentinel: env?.foundSentinel ?? "",
                        host: host, share: share, anonymous: anonymous,
                        username: username, password: password)
                } else {
                    lastError = ctlFailure(run, fallback: "连接设置失败。")
                    if run.timedOut { await refreshAfterTimeout() }
                }
                return
            }
            guard let cfg = try? dec.decode(Config.self, from: run.data) else {
                lastError = BridgeError.decodeFailed.errorDescription
                await reloadConfig(silent: true)
                return
            }
            config = cfg
            showNotice(cfg.notice)
            pollTick()
        } catch {
            lastError = describe(error)
            if isTimeout(error) { await refreshAfterTimeout() }
        }
    }

    /// Re-issue the parked server-set / install with explicit adoption of the
    /// foreign sentinel. The request is passed in by the alert button that
    /// confirmed it: dismissing an alert clears `pendingAdopt` itself, so
    /// reading it back afterwards found nothing and 「确认接管」 silently did
    /// nothing.
    func confirmAdopt(_ p: PendingAdopt) async {
        pendingAdopt = nil
        if let folders = p.folders {
            await install(host: p.host, share: p.share, anonymous: p.anonymous,
                          username: p.username, password: p.password,
                          folders: folders, adopt: true)
        } else {
            await setServer(host: p.host, share: p.share, anonymous: p.anonymous,
                            username: p.username, password: p.password, adopt: true)
        }
    }

    func cancelAdopt() {
        pendingAdopt = nil
    }

    /// Approve held deletions non-interactively (GUI shows its own confirm box).
    /// The --yes path emits a JSON envelope ({"approved":N,...} or {"error":...}),
    /// so decode it: surface the backend error, or confirm how many were released.
    func approveDeletions() async {
        begin("approve")
        defer { end("approve") }
        do {
            let res = try await Bridge.runCtlDecoding(
                ApproveResult.self, ["approve-deletions", "--yes"])
            showNotice("已批准 \(res.approved) 项删除，对应的 NAS 镜像将在下一轮归档进回收站。")
            pollTick()
        } catch {
            lastError = describe(error)
        }
    }

    /// Fetch + decode the diagnostic rows. Shared by the manual 运行诊断 button
    /// and the reopen_doctor fix follow-up; no busy/UI bookkeeping of its own.
    private func fetchDoctorRows() async throws -> [DoctorRow] {
        // doctor exits 1 when a check fails, but still emits the JSON array;
        // runCtl does not throw on non-zero exit, so this is fine.
        try await Bridge.runCtlDecoding([DoctorRow].self,
                                        ["doctor", "--json"], timeout: 90)
    }

    /// Run the diagnostic table and store the rows. Clears any stale inline fix
    /// results so a fresh run starts clean.
    func runDoctor() async {
        begin("doctor")
        defer { end("doctor") }
        fixResults.removeAll()
        do {
            doctor = try await fetchDoctorRows()
        } catch {
            lastError = describe(error)
        }
    }

    /// Run a one-click ctl `fix <fixId>` and record its outcome under `key` (a
    /// doctor row id, or "restart_daemon" for the overview banner). On a
    /// followup=="reopen_doctor" the table is silently refreshed in place
    /// WITHOUT clearing the just-recorded outcome. Never throws — a launch/decode
    /// failure or an {"error":...} envelope becomes a red inline result.
    func runFix(_ fixId: String, key: String) async {
        guard !fixId.isEmpty, fixBusyId == nil else { return }
        fixBusyId = key
        defer { fixBusyId = nil }
        do {
            // restart_daemon waits for the daemon to stop and come back online.
            let res = try await Bridge.runCtlDecoding(FixResult.self,
                                                      ["fix", fixId], timeout: 150)
            let detail = res.detail.isEmpty
                ? (res.ok ? "修复成功。" : (res.error ?? "修复失败。"))
                : res.detail
            fixResults[key] = FixOutcome(ok: res.ok, detail: detail)
            if res.followup == "reopen_doctor" {
                if let rows = try? await fetchDoctorRows() { doctor = rows }
            }
            pollTick()
        } catch {
            fixResults[key] = FixOutcome(ok: false, detail: describe(error))
            pollTick()
        }
    }

    /// Convenience for the 守护进程无响应 banner's 重启守护进程 button (B1).
    func restartDaemon() async {
        await runFix("restart_daemon", key: "restart_daemon")
    }

    /// Convenience for 设置 → 立即校验备份: kicks off a deep verify via the same
    /// `fix` envelope (fix_id run_deep_verify) with inline result under its key.
    func runDeepVerify() async {
        await runFix("run_deep_verify", key: "run_deep_verify")
    }

    /// Mount the share for destination browsing; returns the mountpoint on
    /// success, nil on failure (with lastError set).
    func mountForBrowse() async -> String? {
        begin("mount")
        defer { end("mount") }
        do {
            let result = try await Bridge.runCtlDecoding(MountResult.self,
                                                         ["mount"],
                                                         timeout: 90)
            if let mp = result.mountpoint, result.mounted { return mp }
            lastError = result.error ?? "挂载失败。"
            return nil
        } catch {
            lastError = describe(error)
            return nil
        }
    }

    /// The alert was dismissed: drop what it showed (an adoption prompt first).
    func dismissAlert() {
        if pendingAdopt != nil {
            pendingAdopt = nil
            return
        }
        lastError = nil
        notice = nil
    }

    func clearError() { lastError = nil }
}
