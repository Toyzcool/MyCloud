// LogicTests.swift — behavior tests for the GUI's non-visual logic.
//
// RenderSmokeTest proves every view renders; it cannot see WRONG behavior. The
// 2026-09-25 review found exactly that class: a subprocess bridge that lost a
// successful call's stdout, a health display that said 空闲✓ for a dead
// daemon, a busy flag any overlapping call could clear, a config that never
// reloaded after one failure. These tests pin the fixes against a real
// AppState/Bridge, a sandboxed MYCLOUD_APP and a fake ctl (MYCLOUD_CTL).
//
// Compiled by gui/selftest.sh with gui/src/*.swift MINUS MyCloudApp.swift.

import AppKit
import Foundation
import SwiftUI

// MARK: harness

@MainActor private var failures: [String] = []
@MainActor private var passed = 0

@MainActor
private func check(_ cond: Bool, _ msg: @autoclosure () -> String, line: Int = #line) {
    if cond {
        passed += 1
    } else {
        let m = "line \(line): \(msg())"
        failures.append(m)
        print("FAIL: \(m)")
    }
}

@MainActor
private final class Flag { var done = false }

/// Spin the main run loop until `cond` holds (false on timeout).
@MainActor
@discardableResult
private func spin(timeout: TimeInterval, until cond: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while !cond() {
        if Date() >= deadline { return false }
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    return true
}

/// The async twin of spin(): suspends (so main-actor Tasks the code under test
/// spawned can run) instead of blocking the actor.
@MainActor
@discardableResult
private func waitFor(_ timeout: TimeInterval, until cond: () -> Bool) async -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while !cond() {
        if Date() >= deadline { return false }
        try? await Task.sleep(nanoseconds: 10_000_000)
    }
    return true
}

/// Run an async body to completion on the main actor.
@MainActor
private func runAsync(_ name: String, timeout: TimeInterval = 60,
                      _ body: @escaping @MainActor () async -> Void) {
    print("RUN: \(name)")
    let flag = Flag()
    Task { @MainActor in
        await body()
        flag.done = true
    }
    if !spin(timeout: timeout, until: { flag.done }) {
        check(false, "\(name): did not finish within \(Int(timeout))s")
    }
}

private final class Lines: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [(String, Date)] = []
    func add(_ s: String) { lock.lock(); items.append((s, Date())); lock.unlock() }
    var all: [String] { lock.lock(); defer { lock.unlock() }; return items.map { $0.0 } }
    var stamps: [Date] { lock.lock(); defer { lock.unlock() }; return items.map { $0.1 } }
}

private func text(_ d: Data) -> String { String(decoding: d, as: UTF8.self) }

// MARK: sandbox

private let sandbox: URL = {
    let u = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("mycloud-logic-\(getpid())", isDirectory: true)
    try? FileManager.default.createDirectory(
        at: u.appendingPathComponent("app/runtime"), withIntermediateDirectories: true)
    try? FileManager.default.createDirectory(
        at: u.appendingPathComponent("ctl"), withIntermediateDirectories: true)
    return u
}()

private let fakeCtl = """
import json, os, sys, time
d = os.environ["FAKE_CTL_DIR"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
def bump(name):
    p = os.path.join(d, "count." + name)
    n = int(open(p).read()) if os.path.exists(p) else 0
    open(p, "w").write(str(n + 1))
    return n
n = bump(cmd)
CFG = {"host": "192.168.5.1", "share": "SHARE", "username": "admin",
       "auth_mode": "user", "dest_rel": "MyCloud", "sources": []}
if cmd == "config-get":
    if os.path.exists(os.path.join(d, "config_fail_first")) and n == 0:
        sys.exit(1)
    print(json.dumps(CFG))
elif cmd == "config-set":
    time.sleep(1.0)
    print(json.dumps(CFG))
elif cmd == "sync-now":
    time.sleep(0.2)
    print("已请求立即同步。")
elif cmd == "version":
    print(json.dumps({"backend_version": "9.9.9", "commands": []}))
else:
    print(json.dumps({"error": "unknown command " + cmd}))
    sys.exit(2)
"""

private func count(_ cmd: String) -> Int {
    let p = sandbox.appendingPathComponent("ctl/count.\(cmd)").path
    return Int((try? String(contentsOfFile: p, encoding: .utf8)) ?? "0") ?? 0
}

private func iso(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.string(from: d)
}

private var stateFile: URL { sandbox.appendingPathComponent("app/runtime/state.json") }

private func writeState(_ obj: [String: Any], mtime: Date? = nil) {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    try! data.write(to: stateFile, options: .atomic)
    if let m = mtime {
        try! FileManager.default.setAttributes([.modificationDate: m], ofItemAtPath: stateFile.path)
    }
}

private func setMtime(_ m: Date) {
    try! FileManager.default.setAttributes([.modificationDate: m], ofItemAtPath: stateFile.path)
}

/// A pid that surely belongs to no process any more.
private func deadPid() -> Int {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/true")
    try! p.run()
    p.waitUntilExit()
    return Int(p.processIdentifier)
}

// MARK: Bridge

@MainActor
private func bridgeTests() {
    runAsync("bridge: lines stream whole and all arrive before return") {
        let lines = Lines()
        let script = "import time\nfor i in range(5):\n    print('行%d' % i)\n    time.sleep(0.15)\n"
        do {
            let run = try await Bridge.runProcess("/usr/bin/python3", ["-c", script],
                                                  timeout: 20, onOutput: { lines.add($0) })
            check(lines.all == ["行0", "行1", "行2", "行3", "行4"], "lines: \(lines.all)")
            check(text(run.data) == "行0\n行1\n行2\n行3\n行4\n", "stdout: \(text(run.data))")
            let s = lines.stamps
            // PYTHONUNBUFFERED: lines arrive as printed, not all at exit.
            if s.count == 5 {
                check(s[4].timeIntervalSince(s[0]) > 0.4, "lines arrived in one burst")
            }
            check(!run.timedOut && run.status == 0, "status \(run.status)")
        } catch {
            check(false, "threw \(error)")
        }
    }

    runAsync("bridge: no successful call loses its stdout (8 x 60 concurrent)", timeout: 120) {
        let empties = await withTaskGroup(of: Int.self) { group -> Int in
            for _ in 0..<8 {
                group.addTask {
                    var bad = 0
                    for _ in 0..<60 {
                        let run = try? await Bridge.runProcess("/bin/echo", ["ok"], timeout: 20)
                        if run.map({ text($0.data) }) != "ok\n" { bad += 1 }
                    }
                    return bad
                }
            }
            var total = 0
            for await n in group { total += n }
            return total
        }
        check(empties == 0, "\(empties) of 480 calls lost their stdout")
    }

    runAsync("bridge: deadline sends SIGTERM and lets the cleanup finish") {
        let script = """
        import signal, sys, time
        def h(*a):
            time.sleep(3.5)   # longer than the old 3s SIGKILL
            print("cleaned")
            sys.exit(3)
        signal.signal(signal.SIGTERM, h)
        print("started")
        time.sleep(30)
        """
        let t0 = Date()
        do {
            let run = try await Bridge.runProcess("/usr/bin/python3", ["-c", script],
                                                  timeout: 0.5, killGrace: 10)
            check(run.timedOut, "timedOut not set")
            check(run.status == 3, "status \(run.status)")
            check(text(run.data).contains("cleaned"), "cleanup output lost: \(text(run.data))")
            check(Date().timeIntervalSince(t0) < 9, "took \(Date().timeIntervalSince(t0))s")
        } catch {
            check(false, "threw \(error) -- the cleanup was cut off")
        }
    }

    runAsync("bridge: a child ignoring SIGTERM is killed after the grace") {
        let t0 = Date()
        do {
            _ = try await Bridge.runProcess("/bin/sh", ["-c", "trap '' TERM; exec sleep 30"],
                                            timeout: 0.3, killGrace: 0.7)
            check(false, "returned instead of throwing timedOut")
        } catch BridgeError.timedOut {
            check(Date().timeIntervalSince(t0) < 5, "took \(Date().timeIntervalSince(t0))s")
        } catch {
            check(false, "threw \(error)")
        }
    }

    runAsync("bridge: a grandchild holding the pipe does not stall completion") {
        let t0 = Date()
        do {
            let run = try await Bridge.runProcess("/bin/sh", ["-c", "sleep 5 & echo hi"],
                                                  timeout: 20)
            check(text(run.data) == "hi\n", "stdout \(text(run.data))")
            check(run.status == 0 && !run.timedOut, "status \(run.status)")
            check(Date().timeIntervalSince(t0) < 4, "took \(Date().timeIntervalSince(t0))s")
        } catch {
            check(false, "threw \(error)")
        }
    }

    runAsync("bridge: launch failure throws launchFailed") {
        do {
            _ = try await Bridge.runProcess("/nonexistent/mycloud-x", [], timeout: 5)
            check(false, "did not throw")
        } catch BridgeError.launchFailed {
            check(true, "")
        } catch {
            check(false, "threw \(error)")
        }
    }

    runAsync("bridge: stdin reaches the child and is closed") {
        do {
            let run = try await Bridge.runProcess("/bin/cat", [], timeout: 10, stdin: "s3cret")
            check(text(run.data) == "s3cret\n", "stdout \(text(run.data))")
        } catch {
            check(false, "threw \(error)")
        }
    }

    runAsync("bridge: RunHandle.cancel stops a run promptly") {
        let h = Bridge.RunHandle()
        let t0 = Date()
        Task.detached {
            try? await Task.sleep(nanoseconds: 300_000_000)
            h.cancel()
        }
        do {
            let run = try await Bridge.runProcess("/bin/sleep", ["30"], timeout: 60, handle: h)
            check(h.isCancelled, "not marked cancelled")
            check(run.status != 0, "status \(run.status)")
            check(Date().timeIntervalSince(t0) < 5, "took \(Date().timeIntervalSince(t0))s")
        } catch {
            check(false, "threw \(error)")
        }
    }
}

// MARK: AppState liveness

@MainActor
private func livenessTests() {
    print("RUN: liveness budgets follow the daemon's write cadence")
    do {
        let a = AppState()
        a.installed = true
        let t0 = Date()
        a.now = { t0 }
        var st = Status()
        st.state = "MOUNTED_IDLE"
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-200)
        check(!a.unresponsive, "idle 200s flagged")
        a.stateMtime = t0.addingTimeInterval(-400)
        check(a.unresponsive, "idle 400s not flagged (was 32 minutes)")

        st.state = "SYNCING"               // post-pass work: no active progress
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-400)
        check(!a.unresponsive, "quiet SYNCING 400s flagged (healthy prune)")
        a.stateMtime = t0.addingTimeInterval(-700)
        check(a.unresponsive, "quiet SYNCING 700s not flagged")

        st.state = "STARTING"
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-500)
        check(!a.unresponsive, "STARTING 500s flagged")
        a.stateMtime = t0.addingTimeInterval(-700)
        check(a.unresponsive, "STARTING 700s not flagged")

        st.state = "stopped"               // a pause never times out
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-100_000)
        check(!a.unresponsive, "STOPPED flagged as unresponsive")
        check(a.displayState == "STOPPED", "stopped display \(a.displayState)")

        var p = Progress()
        p.active = true
        p.phase = "transferring"
        st.state = "SYNCING"
        st.progress = p
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-10)
        check(!a.unresponsive && a.progressLive, "live progress 10s")
        a.stateMtime = t0.addingTimeInterval(-31)
        check(a.unresponsive && !a.progressLive, "frozen progress 31s")

        a.installed = false
        check(!a.unresponsive && a.displayState == "OFFLINE", "not installed")
    }

    print("RUN: right after a system wake the old state is not 'unresponsive'")
    do {
        let a = AppState()
        a.installed = true
        let t0 = Date()
        a.now = { t0 }
        var st = Status()
        st.state = "MOUNTED_IDLE"
        a.status = st
        a.stateMtime = t0.addingTimeInterval(-3600)   // slept an hour
        check(a.unresponsive, "an hour-old idle state not flagged")
        a.noteSystemWake(at: t0)
        check(!a.unresponsive, "flagged during the wake grace")
        a.now = { t0.addingTimeInterval(LivenessBudget.wakeGrace + 1) }
        check(a.unresponsive, "the wake grace never ends")
    }

    print("RUN: a pass failure the daemon reported (last_error) needs attention")
    do {
        let a = AppState()
        a.installed = true
        let t0 = Date()
        a.now = { t0 }
        a.stateMtime = t0
        var st = Status()
        st.state = "MOUNTED_IDLE"
        a.status = st
        check(!a.needsAttention && a.displayState == "MOUNTED_IDLE", "clean idle")
        st.lastError = "无法上传：本机的 rsync 不接受 MyCloud 的上传命令"
        a.status = st
        check(a.needsAttention && a.displayState == "ATTENTION",
              "last_error hidden: \(a.displayState)")
        check(menuBarSymbol(for: st, installed: true) == "exclamationmark.icloud",
              "menu bar shows a check over a failing upload")
        check(menuBarSymbol(for: st, installed: true, progressLive: true)
              == "arrow.clockwise.icloud", "a live retry still shows as syncing")
        st.lastError = ""
        a.status = st
        check(!a.needsAttention, "an empty last_error needs attention")
    }

    print("RUN: waking mid-transfer does not read the sleep as a stall")
    do {
        let t0 = Date()
        let prog: [String: Any] = [
            "active": true, "phase": "transferring", "folder": "Docs",
            "files_done": 10, "bytes_done": 100, "batches_done": 1,
            "current": "a/b.bin", "started_at": iso(t0.addingTimeInterval(-60)),
            "last_transfer_at": iso(t0.addingTimeInterval(-1)),
        ]
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t0)
        let a = AppState()
        a.now = { t0 }
        a.pollTick()
        check(a.progressLive && !a.syncStalled, "baseline")

        // Slept 30 min; the heartbeat re-stamps the same snapshot on wake.
        let woke = t0.addingTimeInterval(1800)
        a.noteSystemWake(at: woke)
        a.now = { woke.addingTimeInterval(1) }
        setMtime(woke.addingTimeInterval(1))
        a.pollTick()
        check(!a.syncStalled && a.displayState != "STALLED",
              "the sleep read as a stall: \(a.displayState)")

        // Variant: the first post-wake poll ran before the wake notification.
        let a2 = AppState()
        a2.now = { t0 }
        setMtime(t0)
        a2.pollTick()
        a2.now = { woke.addingTimeInterval(1) }
        setMtime(woke.addingTimeInterval(1))
        a2.pollTick()
        check(a2.syncStalled, "precondition: the pre-sleep snapshot looks stalled")
        a2.noteSystemWake(at: woke)
        check(!a2.syncStalled, "the wake did not clear a stall raised before it")

        // A real stall after the wake is still flagged.
        let late = woke.addingTimeInterval(LivenessBudget.transferStall + 10)
        a.now = { late }
        setMtime(late)
        a.pollTick()
        check(a.syncStalled, "a genuine post-wake stall is never flagged")
    }

    print("RUN: a stall shown before a short nap survives the wake")
    do {
        let t0 = Date()
        let prog: [String: Any] = [
            "active": true, "phase": "transferring", "folder": "Docs",
            "files_done": 3, "bytes_done": 30, "batches_done": 1,
            "current": "x.bin", "started_at": iso(t0.addingTimeInterval(-60)),
            "last_transfer_at": iso(t0.addingTimeInterval(-1)),
        ]
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t0)
        let a = AppState()
        a.now = { t0 }
        a.pollTick()
        let stuck = t0.addingTimeInterval(LivenessBudget.transferStall + 100)
        a.now = { stuck }
        setMtime(stuck)
        a.pollTick()
        check(a.syncStalled, "precondition: stalled while awake")
        a.noteSystemSleep(at: stuck)
        let woke = stuck.addingTimeInterval(60)
        a.now = { woke }                         // as in production
        a.noteSystemWake(at: woke)
        a.pollTick()                             // still the pre-sleep mtime
        a.now = { woke.addingTimeInterval(1) }
        setMtime(woke.addingTimeInterval(1))     // the daemon writes again
        a.pollTick()
        check(a.syncStalled && a.displayState == "STALLED",
              "a nap hid a wedged transfer: \(a.displayState)")

        // Movement seen between the sleep notice and the sleep itself.
        let c = AppState()
        c.now = { t0 }
        setMtime(t0)
        c.pollTick()
        c.noteSystemSleep(at: t0.addingTimeInterval(5))
        var moved = prog
        moved["files_done"] = 4
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": moved],
                   mtime: t0.addingTimeInterval(305))
        c.now = { t0.addingTimeInterval(305) }
        c.pollTick()                             // saw movement 300 s after the notice
        let back = t0.addingTimeInterval(305 + 3600)
        c.now = { back }
        c.noteSystemWake(at: back)
        setMtime(back.addingTimeInterval(1))
        c.now = { back.addingTimeInterval(1) }
        c.pollTick()
        check(!c.syncStalled, "the sleep after late movement read as a stall")
        // ...and the clock restarts at the wake, not later.
        let due = back.addingTimeInterval(LivenessBudget.transferStall + 10)
        c.now = { due }
        setMtime(due)
        c.pollTick()
        check(c.syncStalled, "the stall clock was pushed past the wake")
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t0)

        // A long sleep right after movement still does not count.
        let b = AppState()
        b.now = { t0 }
        setMtime(t0)
        b.pollTick()
        b.noteSystemSleep(at: t0.addingTimeInterval(10))
        let late = t0.addingTimeInterval(10 + 3600)
        b.noteSystemWake(at: late)
        b.now = { late.addingTimeInterval(1) }
        setMtime(late.addingTimeInterval(1))
        b.pollTick()
        check(!b.syncStalled, "an hour asleep read as a stall")
    }

    print("RUN: the attention sentence is never a bare 。")
    check(attentionSentence([]) == "有需要处理的问题。", "empty parts")
    check(attentionSentence(["a", "b"]) == "a；b。", "joined parts")

    print("RUN: display state folds held deletions into 需要处理")
    do {
        let a = AppState()
        a.installed = true
        let t0 = Date()
        a.now = { t0 }
        a.stateMtime = t0
        var st = Status()
        st.state = "MOUNTED_IDLE"
        a.status = st
        check(a.displayState == "MOUNTED_IDLE", "clean idle \(a.displayState)")
        var f = FolderStatus()
        f.heldDeletions = ["a", "b"]
        st.folders = ["x": f]
        a.status = st
        check(a.needsAttention && a.displayState == "ATTENTION", "held: \(a.displayState)")
        a.stateMtime = t0.addingTimeInterval(-1000)
        check(a.displayState == "UNRESPONSIVE", "stale beats attention: \(a.displayState)")
    }

    print("RUN: a dead daemon pid turns unresponsive within the grace, not the idle budget")
    do {
        writeState(["state": "MOUNTED_IDLE", "daemon_pid": Int(getpid())])
        let a = AppState()
        check(a.installed, "sandbox install not detected")
        let t0 = Date()
        a.now = { t0 }
        a.pollTick()
        check(!a.daemonGone && !a.unresponsive, "live pid flagged")

        writeState(["state": "MOUNTED_IDLE", "daemon_pid": deadPid()])
        a.pollTick()
        check(!a.daemonGone, "flagged before the grace (restart flicker)")
        a.now = { t0.addingTimeInterval(LivenessBudget.pidGrace + 1) }
        a.pollTick()
        check(a.daemonGone && a.unresponsive, "dead pid not flagged")
        check(a.displayState == "UNRESPONSIVE", "display \(a.displayState)")

        writeState(["state": "stopped", "daemon_pid": deadPid()])
        a.pollTick()
        check(!a.daemonGone && !a.unresponsive, "a stopped daemon's pid is supposed to be gone")
        // Let the (harmless) launchctl probe this triggered finish.
        spin(timeout: 3, until: { false })
    }

    print("RUN: a transfer without movement is flagged stalled; any movement clears it")
    do {
        let t0 = Date()
        var prog: [String: Any] = [
            "active": true, "phase": "transferring", "folder": "Docs",
            "files_done": 10, "bytes_done": 100, "batches_done": 1,
            "current": "a/b.bin", "started_at": iso(t0.addingTimeInterval(-3000)),
            "last_transfer_at": iso(t0.addingTimeInterval(-2000)),
        ]
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t0)
        let a = AppState()
        a.now = { t0 }
        a.pollTick()
        check(a.progressLive, "progress not live")
        check(!a.syncStalled, "stalled on first sight")

        let t1 = t0.addingTimeInterval(LivenessBudget.transferStall + 100)
        a.now = { t1 }
        setMtime(t1)                               // the heartbeat keeps it fresh
        a.pollTick()
        check(a.syncStalled, "no movement for > budget, not flagged")
        check(a.displayState == "STALLED", "display \(a.displayState)")
        check(menuBarSymbol(for: a.status, installed: true,
                            stale: a.unresponsive || a.syncStalled,
                            progressLive: a.progressLive) == "exclamationmark.icloud",
              "menu bar still says syncing")

        prog["files_done"] = 11
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t1)
        a.pollTick()
        check(!a.syncStalled, "movement did not clear the stall")

        prog["phase"] = "planning"                  // long walks are not stalls
        writeState(["state": "SYNCING", "daemon_pid": Int(getpid()), "progress": prog], mtime: t1)
        a.pollTick()
        a.now = { t1.addingTimeInterval(LivenessBudget.transferStall * 2) }
        setMtime(t1.addingTimeInterval(LivenessBudget.transferStall * 2))
        a.pollTick()
        check(!a.syncStalled, "planning flagged as stalled")
    }
}

// MARK: AppState operations

@MainActor
private func operationTests() {
    runAsync("ops: an overlapping call no longer clears the other's busy flag") {
        let a = AppState()
        let slow = Task { @MainActor in await a.setSetting(key: "retention_days", value: "30") }
        try? await Task.sleep(nanoseconds: 100_000_000)
        check(a.isRunning("config"), "config op not registered")
        await a.syncNow()                               // 0.2s, finishes first
        check(a.busy && a.isRunning("config"), "busy cleared by the faster call")
        let ok = await slow.value
        check(ok, "setSetting reported failure")
        check(!a.busy, "busy stuck after both finished")
        check(a.lastError == nil, "unexpected error: \(a.lastError ?? "")")
    }

    runAsync("ops: a failed config load is retried in the background, silently") {
        // Earlier AppStates' own background config loads must land first.
        await waitFor(1, until: { false })
        let marker = sandbox.appendingPathComponent("ctl/config_fail_first").path
        FileManager.default.createFile(atPath: marker, contents: Data())
        try? FileManager.default.removeItem(
            at: sandbox.appendingPathComponent("ctl/count.config-get"))
        defer { try? FileManager.default.removeItem(atPath: marker) }
        writeState(["state": "MOUNTED_IDLE", "daemon_pid": Int(getpid())])

        let a = AppState()
        let t0 = Date()
        a.now = { t0 }
        a.pollTick()                                    // first attempt fails
        check(await waitFor(10, until: { count("config-get") == 1 }), "no first attempt")
        await waitFor(1, until: { false })
        check(a.config == nil, "config loaded from a failing ctl")
        check(a.lastError == nil, "silent retry raised an alert")

        a.now = { t0.addingTimeInterval(5) }
        a.pollTick()
        await waitFor(1, until: { false })
        check(count("config-get") == 1, "retried before the throttle")

        a.now = { t0.addingTimeInterval(16) }
        a.pollTick()
        check(await waitFor(10, until: { a.config != nil }), "config never reloaded")
        check(a.config?.host == "192.168.5.1", "wrong config")
    }

    runAsync("ops: confirmAdopt acts on the request it is given") {
        let a = AppState()
        let before = count("server-set")
        let p = AppState.PendingAdopt(message: "m", foundSentinel: "s", host: "h",
                                      share: "S", anonymous: true, username: "",
                                      password: "")
        a.pendingAdopt = nil            // what the alert's dismissal leaves behind
        await a.confirmAdopt(p)
        check(count("server-set") == before + 1, "server-set was not re-issued")
        check(a.lastError != nil, "fake ctl's refusal not surfaced")
    }

    print("RUN: dismissing the alert clears what it showed")
    do {
        let a = AppState()
        a.lastError = "e"
        a.notice = "n"
        a.dismissAlert()
        check(a.lastError == nil && a.notice == nil, "not cleared")
        a.pendingAdopt = AppState.PendingAdopt(message: "m", foundSentinel: "s", host: "h",
                                               share: "S", anonymous: true, username: "",
                                               password: "")
        a.lastError = "e"
        a.dismissAlert()
        check(a.pendingAdopt == nil && a.lastError == "e", "adopt dismissal dropped the error")
    }
}

// MARK: pure helpers and models

@MainActor
private func helperTests() {
    print("RUN: version comparison only calls OLDER outdated")
    check(AppState.compareVersions("1.2.1", "1.3.0") == -1, "1.2.1 < 1.3.0")
    check(AppState.compareVersions("1.3.0", "1.3.0") == 0, "equal")
    check(AppState.compareVersions("1.10.0", "1.9.9") == 1, "numeric, not lexical")
    check(AppState.compareVersions("1.3", "1.3.0") == 0, "missing parts are 0")
    check(AppState.compareVersions("2.0b1", "2.0") == 0, "suffixes ignored")

    print("RUN: menu bar symbols are whitelisted")
    var st = Status()
    st.state = "STARTING"
    check(menuBarSymbol(for: st, installed: true) == "arrow.clockwise.icloud", "STARTING")
    st.state = "RESTARTING"
    check(menuBarSymbol(for: st, installed: true) == "arrow.clockwise.icloud", "RESTARTING")
    st.state = "SOMETHING_NEW"
    check(menuBarSymbol(for: st, installed: true) == "icloud", "unknown state got a check")
    st.state = "MOUNTED_IDLE"
    check(menuBarSymbol(for: st, installed: true) == "checkmark.icloud", "idle")
    check(menuBarSymbol(for: st, installed: true, stale: true) == "exclamationmark.icloud",
          "stale")
    check(menuBarSymbol(for: st, installed: false) == "icloud.slash", "not installed")
    st.state = "SYNCING"
    check(menuBarSymbol(for: st, installed: true) == "arrow.clockwise.icloud", "syncing")

    print("RUN: state labels cover the new states")
    check(stateLabel("STARTING", installed: true) == "启动中", "STARTING")
    check(stateLabel("RESTARTING", installed: true) == "重启中", "RESTARTING")
    check(stateLabel("stopped", installed: true) == "已停止", "stopped")
    check(stateLabel("PAUSED", installed: true) == "已暂停", "PAUSED")
    check(stateLabel("STALLED", installed: true) == "同步停滞", "STALLED")
    check(stateLabel("ATTENTION", installed: true) == "需要处理", "ATTENTION")
    check(stateLabel("EXITED", installed: true).contains("退出"), "EXITED")
    check(stateLabel("MOUNTED_IDLE", installed: false) == "未安装", "not installed")

    print("RUN: models decode held_total, daemon_pid and guard layers")
    let dec = JSONDecoder()
    let f1 = try? dec.decode(FolderStatus.self,
                             from: Data(#"{"held_deletions":["a","b"],"held_total":60000}"#.utf8))
    check(f1?.heldCount == 60000 && f1?.heldTruncated == true
          && f1?.heldCountUncertain == false, "held_total ignored")
    let capped = "[" + (0..<1000).map { "\"f\($0)\"" }.joined(separator: ",") + "]"
    let f2 = try? dec.decode(FolderStatus.self,
                             from: Data("{\"held_deletions\":\(capped)}".utf8))
    check(f2?.heldCount == 1000 && f2?.heldCountUncertain == true, "capped list not uncertain")
    let g1 = try? dec.decode(GuardInfo.self, from: Data(
        #"{"layer":"mount","detail":"mount_smbfs: Operation not permitted"}"#.utf8))
    check(g1?.isPermissionDenied == false, "mount failure blamed on Full Disk Access")
    let g2 = try? dec.decode(GuardInfo.self, from: Data(
        #"{"layer":"sentinel","detail":"cat: x: Operation not permitted"}"#.utf8))
    check(g2?.isPermissionDenied == true, "sentinel TCC denial missed")
    let s1 = try? dec.decode(Status.self, from: Data(
        #"{"state":"STARTING","daemon_pid":4242,"daemon_started_at":"2026-09-26T00:00:00Z"}"#.utf8))
    check(s1?.daemonPid == 4242 && s1?.daemonStartedAt != nil, "daemon pid not decoded")

    print("RUN: the journal tail reads only the end of a large journal")
    let journal = sandbox.appendingPathComponent("app/runtime/journal.ndjson")
    var body = ""
    for i in 0..<3000 {
        body += "{\"ts\":\"2026-09-26T00:00:00Z\",\"folder\":\"f\(i)\",\"copied\":\(i),"
            + "\"trashed\":0,\"status\":\"ok\",\"pad\":\"\(String(repeating: "x", count: 60))\"}\n"
    }
    try? body.data(using: .utf8)!.write(to: journal)
    let tail = readJournalTail(limit: 20)
    check(tail.count == 20, "tail count \(tail.count)")
    check(tail.first?.folder == "f2999" && tail.last?.folder == "f2980",
          "tail order \(tail.first?.folder ?? "-")…\(tail.last?.folder ?? "-")")
}

// MARK: entry

@main
@MainActor
struct LogicTests {
    static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)

        let ctl = sandbox.appendingPathComponent("ctl/mycloudctl.py")
        try? fakeCtl.data(using: .utf8)!.write(to: ctl)
        setenv("MYCLOUD_APP", sandbox.appendingPathComponent("app").path, 1)
        setenv("MYCLOUD_CTL", ctl.path, 1)
        setenv("FAKE_CTL_DIR", sandbox.appendingPathComponent("ctl").path, 1)
        writeState(["state": "MOUNTED_IDLE"])

        helperTests()
        bridgeTests()
        livenessTests()
        operationTests()

        try? FileManager.default.removeItem(at: sandbox)
        if failures.isEmpty {
            print("LOGIC-TESTS: ALL OK (\(passed) checks)")
            exit(0)
        }
        print("LOGIC-TESTS: \(failures.count) FAILURE(S)")
        for f in failures { print("   \(f)") }
        exit(1)
    }
}
