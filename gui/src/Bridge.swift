// Bridge.swift — the thin shell's only channel to the backend.
//
// Two paths:
//   1. runCtl(...)  — spawns `/usr/bin/python3 <ctl> <args>` for every WRITE
//      (config mutations, sync-now, approve, mount, doctor). Pipe-deadlock safe
//      (stdout drained via readabilityHandler) and always bounded by a timeout.
//   2. readState()  — reads runtime/state.json directly. Far cheaper than
//      spawning ctl every second, used by the 1s status poller.
//
// Backend location (PLAN 12.3): dev builds point MYCLOUD_CTL at the repo's
// src/mycloudctl.py; installed builds use the copy under Application Support.

import Foundation

enum BridgeError: Error, LocalizedError {
    case notInstalled
    case launchFailed(String)
    case timedOut
    case decodeFailed
    case ctl(String)

    var errorDescription: String? {
        switch self {
        case .notInstalled: return "未安装守护进程（找不到 mycloudctl.py）。"
        case .launchFailed(let m): return "无法启动后端：\(m)"
        case .timedOut: return "后端调用超时。"
        case .decodeFailed: return "无法解析后端返回的数据（后端可能过旧、不支持该命令——请同步更新 mycloudctl）。"
        case .ctl(let m): return m
        }
    }
}

enum Bridge {
    // MARK: locations

    /// Absolute path to mycloudctl.py. MYCLOUD_CTL (dev) wins; else the copy the
    /// installer drops under ~/Library/Application Support/MyCloud.
    static var ctlPath: String {
        let env = ProcessInfo.processInfo.environment["MYCLOUD_CTL"]
        if let dev = env, !dev.isEmpty { return dev }
        return NSHomeDirectory()
            + "/Library/Application Support/MyCloud/mycloudctl.py"
    }

    /// The LaunchAgent plist `mycloudctl install` writes last.
    static var agentPlistPath: String {
        NSHomeDirectory() + "/Library/LaunchAgents/com.mycloud.agent.plist"
    }

    /// True once an install COMPLETED (drives the onboarding page). The ctl
    /// file alone is not enough: `install` copies mycloudctl.py before it
    /// verifies the NAS, so a first install that failed on a wrong password or
    /// an adoption prompt used to flip the GUI out of the wizard into a main UI
    /// that could never load its config -- with no way back but uninstall. The
    /// agent plist is written only after the connection verified (and removed
    /// by uninstall; pause keeps it). A dev run (MYCLOUD_CTL) needs only ctl.
    static var isInstalled: Bool {
        let fm = FileManager.default
        guard fm.fileExists(atPath: ctlPath) else { return false }
        if let dev = ProcessInfo.processInfo.environment["MYCLOUD_CTL"], !dev.isEmpty {
            return true
        }
        return fm.fileExists(atPath: agentPlistPath)
    }

    /// The backend shipped inside the app bundle (Resources/backend): what the
    /// GUI installer copies into Application Support, and the ctl it runs for
    /// `install` while nothing is installed yet. nil in a dev run without a
    /// built bundle.
    static var bundledBackendDir: String? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let dir = res.appendingPathComponent("backend", isDirectory: true).path
        return FileManager.default.fileExists(atPath: dir + "/mycloudctl.py") ? dir : nil
    }

    /// APP base dir (MYCLOUD_APP override, matching src/mycloud/paths.py).
    static var appDir: URL {
        let env = ProcessInfo.processInfo.environment["MYCLOUD_APP"]
        if let app = env, !app.isEmpty {
            return URL(fileURLWithPath: app, isDirectory: true)
        }
        return URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Application Support/MyCloud",
                                    isDirectory: true)
    }

    /// runtime/state.json — the file the daemon rewrites atomically.
    static var stateURL: URL {
        appDir.appendingPathComponent("runtime/state.json")
    }

    // MARK: direct state read (poller)

    /// Read + decode state.json. Returns nil when the file is absent (backend
    /// never ran) or unreadable; decoding itself never throws (lenient models).
    static func readState() -> Status? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(Status.self, from: data)
    }

    /// Modification time of state.json on disk, or nil if absent. This is the
    /// ground truth for "when did the daemon last write" — more trustworthy than
    /// the self-reported updated_at inside the file — so it drives the staleness
    /// / 守护进程无响应 heuristic (B1).
    static func stateFileMtime() -> Date? {
        guard let attrs = try? FileManager.default
            .attributesOfItem(atPath: stateURL.path) else { return nil }
        return attrs[.modificationDate] as? Date
    }

    /// Does process `pid` exist? The daemon records its pid in state.json, and
    /// this is a far sharper "is it running" signal than the file's age: its
    /// heartbeat thread keeps state.json fresh for as long as the process
    /// lives, and a SIGKILLed daemon's last state (often 空闲) used to stay on
    /// screen for the whole idle budget. EPERM means it exists (not ours).
    static func processAlive(_ pid: Int) -> Bool {
        guard pid > 0, pid <= Int(Int32.max) else { return false }
        if kill(pid_t(pid), 0) == 0 { return true }
        return errno == EPERM
    }

    /// launchd's view of the agent: nil when launchctl could not be asked,
    /// else whether it is loaded plus the last exit code it reports (the
    /// "异常退出" detail when state.json says stopped but the agent is loaded).
    struct AgentStatus: Sendable {
        let loaded: Bool
        let lastExit: String?
    }

    static func agentStatus() async -> AgentStatus? {
        let target = "gui/\(getuid())/com.mycloud.agent"
        guard let run = try? await runProcess("/bin/launchctl", ["print", target],
                                              timeout: 10, killGrace: 2) else {
            return nil
        }
        guard run.status == 0 else { return AgentStatus(loaded: false, lastExit: nil) }
        let text = String(decoding: run.data, as: UTF8.self)
        var lastExit: String? = nil
        for line in text.split(separator: "\n") {
            let s = line.trimmingCharacters(in: .whitespaces)
            if s.hasPrefix("last exit code = ") {
                lastExit = String(s.dropFirst("last exit code = ".count))
                break
            }
        }
        return AgentStatus(loaded: true, lastExit: lastExit)
    }

    /// One pipe's bytes, plus whole-line delivery for onOutput. `consume` and
    /// `flushTail` run on that handle's serial readability queue; `bytes` is
    /// read after EOF (or under the lock from any thread).
    private final class PipeSink: @unchecked Sendable {
        private let lock = NSLock()
        private var data = Data()
        private var lineBuf = Data()
        private var ended = false
        let onLine: (@Sendable (String) -> Void)?
        init(onLine: (@Sendable (String) -> Void)?) { self.onLine = onLine }

        func consume(_ chunk: Data) {
            lock.lock(); data.append(chunk); lock.unlock()
            guard let cb = onLine else { return }
            lineBuf.append(chunk)
            // Whole lines only: a chunk boundary inside a 3-byte UTF-8
            // character used to make the whole chunk undecodable.
            while let nl = lineBuf.firstIndex(of: 0x0A) {
                let line = Data(lineBuf[lineBuf.startIndex..<nl])
                lineBuf.removeSubrange(lineBuf.startIndex...nl)
                if let s = String(data: line, encoding: .utf8) { cb(s) }
            }
        }
        func flushTail() {
            if let cb = onLine, !lineBuf.isEmpty, let s = String(data: lineBuf, encoding: .utf8) {
                cb(s)
            }
            lineBuf.removeAll()
        }
        /// True exactly once (EOF bookkeeping).
        func end() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if ended { return false }
            ended = true
            return true
        }
        var bytes: Data { lock.lock(); defer { lock.unlock() }; return data }
    }

    /// The run's single-resume slot, timeout flag, and the only strong
    /// reference to the Process (released on completion, so pipes and fds do
    /// not linger until the timeout would have fired).
    private final class RunBox: @unchecked Sendable {
        private let lock = NSLock()
        private var done = false
        private var timedOut = false
        private var proc: Process?
        private var timer: DispatchWorkItem?
        init(_ p: Process) { proc = p }
        var process: Process? { lock.lock(); defer { lock.unlock() }; return proc }
        func setTimer(_ t: DispatchWorkItem) { lock.lock(); timer = t; lock.unlock() }
        func markTimedOut() { lock.lock(); timedOut = true; lock.unlock() }
        var didTimeOut: Bool { lock.lock(); defer { lock.unlock() }; return timedOut }
        /// True exactly once; cancels the timer and drops the Process.
        func claim() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if done { return false }
            done = true
            timer?.cancel(); timer = nil; proc = nil
            return true
        }
    }

    /// Lets a caller stop a long run (materialize's 取消). SIGTERM only: the
    /// child's own handlers decide what to clean up.
    final class RunHandle: @unchecked Sendable {
        private let lock = NSLock()
        private var proc: Process?
        private var cancelled = false
        init() {}
        var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
        /// False when cancel() already happened (the caller then stops it).
        fileprivate func attach(_ p: Process) -> Bool {
            lock.lock(); defer { lock.unlock() }
            proc = p
            return !cancelled
        }
        fileprivate func detach() { lock.lock(); proc = nil; lock.unlock() }
        func cancel() {
            lock.lock()
            cancelled = true
            let p = proc
            lock.unlock()
            if let p = p, p.isRunning { p.terminate() }
        }
    }

    // MARK: ctl subprocess

    /// Result of a ctl run: raw stdout plus the process exit status (so callers
    /// that ignore stdout can still detect a non-zero exit).
    struct CtlRun: Sendable {
        let data: Data
        let status: Int32
        /// Whatever the child wrote to stderr. Discarding it used to make a
        /// failing ctl undiagnosable: an OLD backend meets an unknown
        /// subcommand, argparse exits 2 with an empty stdout and puts
        /// "invalid choice: 'server-set'" here -- the user only saw "后端未返回
        /// 可解析的结果" (2026-09-12 field report).
        var errData: Data = Data()
        /// SIGTERM was sent at the deadline, and the child still exited within
        /// the grace period (so data/status are its own final answer).
        var timedOut = false

        /// Last line of stderr, trimmed and capped, for an error message.
        var errTail: String? {
            let text = (String(data: errData, encoding: .utf8) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let line = text.split(separator: "\n").last.map(String.init),
                  !line.isEmpty else { return nil }
            return line.count > 300 ? String(line.suffix(300)) : line
        }
    }

    /// Run `python3 <ctl> <args>` and return stdout + exit status. Never throws
    /// on a non-zero exit (ctl emits {"error":...} JSON on stdout in that case —
    /// the caller decodes it); throws only on launch failure or timeout.
    /// `stdin`, when given, is written to the child's standard input (one line,
    /// newline-terminated) and the pipe is closed. This is the ONLY channel
    /// for secrets: a password in `args` would be visible to every process on
    /// the machine via ps / the process table.
    static func runCtl(_ args: [String],
                       timeout: TimeInterval = 30,
                       stdin: String? = nil,
                       ctl: String? = nil,
                       handle: RunHandle? = nil,
                       onOutput: (@Sendable (String) -> Void)? = nil) async throws -> CtlRun {
        let path = ctl ?? ctlPath
        guard FileManager.default.fileExists(atPath: path) else {
            throw BridgeError.notInstalled
        }
        return try await runProcess("/usr/bin/python3", [path] + args,
                                    timeout: timeout, stdin: stdin, handle: handle,
                                    onOutput: onOutput)
    }

    /// Run any executable with the same bounded-lifetime contract as runCtl.
    /// Used for ctl itself and for environment probes (`xcode-select -p`).
    ///
    /// Completion waits for BOTH pipes to reach EOF, so every byte and every
    /// onOutput line is delivered before the result: the old termination-time
    /// readToEnd raced the readability handler and lost a successful call's
    /// whole stdout about once in a thousand runs (more under load) -- which the
    /// GUI then reported as "backend too old". onOutput receives whole lines.
    ///
    /// At the deadline the child gets SIGTERM and `killGrace` to finish: ctl
    /// turns SIGTERM into SystemExit so its finally blocks (resume the daemon it
    /// stopped, unmount a verification mount) run; a 3s SIGKILL used to cut
    /// that short. If it exits in time, its own output is returned with
    /// timedOut=true; if it is wedged (uninterruptible SMB I/O), SIGKILL and
    /// throw .timedOut regardless.
    static func runProcess(_ executable: String, _ arguments: [String],
                           timeout: TimeInterval = 30,
                           killGrace: TimeInterval = 45,
                           stdin: String? = nil,
                           handle: RunHandle? = nil,
                           onOutput: (@Sendable (String) -> Void)? = nil) async throws -> CtlRun {
        return try await withCheckedThrowingContinuation { cont in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: executable)
            proc.arguments = arguments
            var env = ProcessInfo.processInfo.environment
            // ctl never flushes; through a pipe Python block-buffers stdout, so
            // materialize's per-batch progress arrived only at exit.
            env["PYTHONUNBUFFERED"] = "1"
            proc.environment = env
            let outPipe = Pipe()
            let errPipe = Pipe()
            proc.standardOutput = outPipe
            proc.standardError = errPipe
            let inPipe: Pipe? = (stdin == nil) ? nil : Pipe()
            if let p = inPipe { proc.standardInput = p }

            let box = RunBox(proc)
            let out = PipeSink(onLine: onOutput)
            let err = PipeSink(onLine: nil)
            let eof = DispatchGroup()
            @Sendable func finish(_ result: Result<CtlRun, Error>) {
                if box.claim() {
                    handle?.detach()
                    cont.resume(with: result)
                }
            }

            let outH = outPipe.fileHandleForReading
            let errH = errPipe.fileHandleForReading
            for (h, sink) in [(outH, out), (errH, err)] {
                eof.enter()
                h.readabilityHandler = { h in
                    let chunk = h.availableData
                    if chunk.isEmpty {
                        // EOF: stop the source (no busy loop), free the fd now.
                        h.readabilityHandler = nil
                        sink.flushTail()
                        try? h.close()
                        if sink.end() { eof.leave() }
                    } else {
                        sink.consume(chunk)
                    }
                }
            }

            proc.terminationHandler = { p in
                let status = p.terminationStatus
                DispatchQueue.global().async {
                    // A grandchild holding a pipe open must not stall us forever.
                    _ = eof.wait(timeout: .now() + 2)
                    finish(.success(CtlRun(data: out.bytes, status: status,
                                           errData: err.bytes,
                                           timedOut: box.didTimeOut)))
                }
            }

            let work = DispatchWorkItem {
                guard let p = box.process, p.isRunning else { return }
                box.markTimedOut()
                p.terminate()
                let pid = p.processIdentifier
                DispatchQueue.global().asyncAfter(deadline: .now() + killGrace) {
                    if let p = box.process, p.isRunning, pid > 0 { kill(pid, SIGKILL) }
                    finish(.failure(BridgeError.timedOut))
                }
            }
            box.setTimer(work)
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: work)

            do {
                try proc.run()
                if let h = handle, !h.attach(proc) { proc.terminate() }
                if let p = inPipe, let text = stdin {
                    let w = p.fileHandleForWriting
                    // EPIPE instead of SIGPIPE if the child already exited:
                    // try? only catches the ObjC exception, not the signal.
                    _ = fcntl(w.fileDescriptor, F_SETNOSIGPIPE, 1)
                    try? w.write(contentsOf: Data((text + "\n").utf8))
                    try? w.close()
                }
            } catch {
                outH.readabilityHandler = nil
                errH.readabilityHandler = nil
                if out.end() { eof.leave() }
                if err.end() { eof.leave() }
                finish(.failure(BridgeError.launchFailed(error.localizedDescription)))
            }
        }
    }

    // MARK: decode helpers

    /// Run ctl and decode stdout as T. If stdout is an {"error":...} envelope,
    /// throws BridgeError.ctl with the backend's message.
    static func runCtlDecoding<T: Decodable>(_ type: T.Type,
                                             _ args: [String],
                                             timeout: TimeInterval = 30,
                                             stdin: String? = nil)
        async throws -> T {
        let data = try await runCtl(args, timeout: timeout, stdin: stdin).data
        let dec = JSONDecoder()
        if let env = try? dec.decode(CtlError.self, from: data),
           let msg = env.error {
            throw BridgeError.ctl(msg)
        }
        guard let value = try? dec.decode(T.self, from: data) else {
            throw BridgeError.decodeFailed
        }
        return value
    }
}
