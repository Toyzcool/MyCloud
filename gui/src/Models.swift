// Models.swift — Codable models mirroring the mycloudctl JSON contract
// (PLAN 12.2). EVERY field is optional-with-default so the GUI can never crash
// on a missing, null, extra, or wrong-typed key: each struct decodes through
// the `lenient` helper, which swallows both absence and type mismatches.
//
// The contract was verified by running `config-get --json` / `status --json` /
// `doctor --json` against a temp MYCLOUD_APP dir; key names below match exactly.

import Foundation

extension KeyedDecodingContainer {
    /// Decode an optional value, returning nil on absence, JSON null, OR a type
    /// mismatch. Never throws — this is what makes the models crash-proof.
    func lenient<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        return (try? decodeIfPresent(T.self, forKey: key)) ?? nil
    }
}

// MARK: - config-get --json

struct Source: Decodable, Identifiable, Hashable {
    var path: String = ""
    var isFile: Bool = false
    var name: String = ""

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path
        case isFile = "is_file"
        case name
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.lenient(String.self, .path) ?? ""
        isFile = c.lenient(Bool.self, .isFile) ?? false
        name = c.lenient(String.self, .name) ?? ""
    }
}

struct Config: Decodable {
    var host: String = ""
    var share: String = ""
    var username: String = ""
    // NAS connection mode: "user" (credentials for `username`) or "anonymous"
    // (SMB guest session). Mirrors [server] auth_mode.
    var authMode: String = "user"
    var destRel: String = "MyCloud"
    var sources: [Source] = []
    var excludes: [String] = []
    var retentionDays: Int = 30
    var trashMaxGb: Int = 50
    var hotInterval: Int = 30
    var fullInterval: Int = 900
    var maxDeleteFiles: Int = 200
    var maxDeletePct: Int = 20
    var offlineNotifyHours: Int = 24
    var watchMode: String = "auto"
    // iCloud handling (v2). policy: "skip" (leave cloud-only files out of backup)
    // or "download" (materialize + back up in batches). evict releases local
    // space after each batch is backed up (only meaningful under "download").
    var icloudPolicy: String = "skip"
    var icloudEvictAfterBackup: Bool = false
    var warnings: [String] = []
    // Only present on mutation responses (config-add-source, etc.).
    var notice: String? = nil
    var reloadSignalled: Bool? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case host, share, username
        case authMode = "auth_mode"
        case destRel = "dest_rel"
        case sources, excludes
        case retentionDays = "retention_days"
        case trashMaxGb = "trash_max_gb"
        case hotInterval = "hot_interval"
        case fullInterval = "full_interval"
        case maxDeleteFiles = "max_delete_files"
        case maxDeletePct = "max_delete_pct"
        case offlineNotifyHours = "offline_notify_hours"
        case watchMode = "watch_mode"
        case icloudPolicy = "icloud_policy"
        case icloudEvictAfterBackup = "icloud_evict_after_backup"
        case warnings, notice
        case reloadSignalled = "reload_signalled"
        case error
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        host = c.lenient(String.self, .host) ?? ""
        share = c.lenient(String.self, .share) ?? ""
        username = c.lenient(String.self, .username) ?? ""
        authMode = c.lenient(String.self, .authMode) ?? "user"
        destRel = c.lenient(String.self, .destRel) ?? "MyCloud"
        sources = c.lenient([Source].self, .sources) ?? []
        excludes = c.lenient([String].self, .excludes) ?? []
        retentionDays = c.lenient(Int.self, .retentionDays) ?? 30
        trashMaxGb = c.lenient(Int.self, .trashMaxGb) ?? 50
        hotInterval = c.lenient(Int.self, .hotInterval) ?? 30
        fullInterval = c.lenient(Int.self, .fullInterval) ?? 900
        maxDeleteFiles = c.lenient(Int.self, .maxDeleteFiles) ?? 200
        maxDeletePct = c.lenient(Int.self, .maxDeletePct) ?? 20
        offlineNotifyHours = c.lenient(Int.self, .offlineNotifyHours) ?? 24
        watchMode = c.lenient(String.self, .watchMode) ?? "auto"
        icloudPolicy = c.lenient(String.self, .icloudPolicy) ?? "skip"
        icloudEvictAfterBackup = c.lenient(Bool.self, .icloudEvictAfterBackup) ?? false
        warnings = c.lenient([String].self, .warnings) ?? []
        notice = c.lenient(String.self, .notice)
        reloadSignalled = c.lenient(Bool.self, .reloadSignalled)
        error = c.lenient(String.self, .error)
    }
}

// MARK: - status --json (state.json, read directly by the GUI poller)

struct Progress: Decodable {
    var active: Bool = false
    var folder: String = ""
    // Pinned contract phases: "planning" | "downloading" | "transferring" |
    // "verifying". Legacy "scanning" is gone but still rendered gracefully if a
    // pre-v2 backend emits it; an unknown/empty phase falls back to transferring.
    var phase: String = ""
    var filesDone: Int = 0
    var totalFiles: Int? = nil   // nullable per PLAN 12.1
    var bytesDone: Int? = nil    // v2: byte progress (optional)
    var bytesTotal: Int? = nil   // v2: byte total (optional)
    var current: String = ""
    var startedAt: String? = nil
    // Activity signals (2026-07-27). filesDone advances in JUMPS -- rsync
    // block-buffers its itemize output and a batch is up to 500 files -- so a
    // counter-only UI looks paused during a perfectly healthy transfer. These
    // keep moving, letting the UI answer "is it still working?".
    var lastTransferAt: String? = nil
    var batchesDone: Int? = nil
    var batchesTotal: Int? = nil

    enum CodingKeys: String, CodingKey {
        case active, folder, phase
        case filesDone = "files_done"
        case totalFiles = "total_files"
        case bytesDone = "bytes_done"
        case bytesTotal = "bytes_total"
        case current
        case startedAt = "started_at"
        case lastTransferAt = "last_transfer_at"
        case batchesDone = "batches_done"
        case batchesTotal = "batches_total"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        active = c.lenient(Bool.self, .active) ?? false
        folder = c.lenient(String.self, .folder) ?? ""
        phase = c.lenient(String.self, .phase) ?? ""
        filesDone = c.lenient(Int.self, .filesDone) ?? 0
        totalFiles = c.lenient(Int.self, .totalFiles)
        bytesDone = c.lenient(Int.self, .bytesDone)
        bytesTotal = c.lenient(Int.self, .bytesTotal)
        current = c.lenient(String.self, .current) ?? ""
        startedAt = c.lenient(String.self, .startedAt)
        lastTransferAt = c.lenient(String.self, .lastTransferAt)
        batchesDone = c.lenient(Int.self, .batchesDone)
        batchesTotal = c.lenient(Int.self, .batchesTotal)
    }

    /// Phase predicates. `isScanning` is kept for the legacy pre-v2 phase so old
    /// backends still render an indeterminate "building the list" bar.
    var isPlanning: Bool { phase == "planning" }
    var isDownloading: Bool { phase == "downloading" }
    var isTransferring: Bool { phase == "transferring" }
    var isVerifying: Bool { phase == "verifying" }
    var isScanning: Bool { phase == "scanning" }

    /// True when both byte counters are present and the total is meaningful, so
    /// the UI can drive the progress bar off transferred VOLUME.
    var hasBytes: Bool {
        guard let bt = bytesTotal, bt > 0, bytesDone != nil else { return false }
        return true
    }

    /// Completion fraction (0...1) by BYTES -- the honest measure of a transfer
    /// across 315k files whose sizes span several orders of magnitude, where a
    /// file-count percentage badly misrepresents remaining work. nil when byte
    /// totals are absent, so callers can fall back to counts.
    var byteFraction: Double? {
        guard let bt = bytesTotal, bt > 0, let bd = bytesDone else { return nil }
        return min(max(Double(bd) / Double(bt), 0), 1)
    }
}

/// Per-folder rollup of files that live only in iCloud (dataless placeholders).
/// Always present on v2 folders; when config icloud_policy=="download" the same
/// count means "pending download" rather than "permanently skipped".
struct IcloudSkipped: Decodable {
    var count: Int = 0
    var bytes: Int = 0            // 64-bit Int: comfortably holds tens of GB
    var samples: [String] = []    // a few relpaths for the user to recognize

    enum CodingKeys: String, CodingKey {
        case count, bytes, samples
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        count = c.lenient(Int.self, .count) ?? 0
        bytes = c.lenient(Int.self, .bytes) ?? 0
        samples = c.lenient([String].self, .samples) ?? []
    }
}

struct FolderStatus: Decodable {
    var state: String = ""
    var lastOk: String? = nil
    var filesCopied: Int = 0
    var filesTrashed: Int = 0
    var heldDeletions: [String] = []   // relpaths currently held ([] if none)
    /// The REAL number of held deletions. heldDeletions is capped at 1000 per
    /// folder for state.json's size, but one approval releases all of them --
    /// a confirm box counting the list said "1000" for a 60,000-file removal.
    /// Absent from state written by older backends.
    var heldTotal: Int? = nil
    var failedFiles: [String] = []
    var orphaned: Bool = false
    var icloudSkipped: IcloudSkipped = IcloudSkipped()   // always present (v2)
    /// Deletions held back while an evicted iCloud file may have moved there
    /// (macOS <= 13 placeholders). count/samples; bytes unused. Absent from
    /// state written by older backends and before a whole-folder pass.
    var deferredDeletions: IcloudSkipped? = nil

    enum CodingKeys: String, CodingKey {
        case state
        case lastOk = "last_ok"
        case filesCopied = "files_copied"
        case filesTrashed = "files_trashed"
        case heldDeletions = "held_deletions"
        case heldTotal = "held_total"
        case failedFiles = "failed_files"
        case orphaned
        case icloudSkipped = "icloud_skipped"
        case deferredDeletions = "deferred_deletions"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = c.lenient(String.self, .state) ?? ""
        lastOk = c.lenient(String.self, .lastOk)
        filesCopied = c.lenient(Int.self, .filesCopied) ?? 0
        filesTrashed = c.lenient(Int.self, .filesTrashed) ?? 0
        heldDeletions = c.lenient([String].self, .heldDeletions) ?? []
        heldTotal = c.lenient(Int.self, .heldTotal)
        failedFiles = c.lenient([String].self, .failedFiles) ?? []
        orphaned = c.lenient(Bool.self, .orphaned) ?? false
        icloudSkipped = c.lenient(IcloudSkipped.self, .icloudSkipped) ?? IcloudSkipped()
        deferredDeletions = c.lenient(IcloudSkipped.self, .deferredDeletions)
    }

    /// Held deletions to report: the persisted total (older backends: the list).
    var heldCount: Int { max(heldTotal ?? 0, heldDeletions.count) }
    /// No total persisted and the list is at the daemon's cap (1000): the real
    /// number is unknown and at least this.
    var heldCountUncertain: Bool { heldTotal == nil && heldDeletions.count >= 1000 }
    /// The list shows only part of what an approval releases.
    var heldTruncated: Bool { heldCount > heldDeletions.count || heldCountUncertain }
}

/// Top-level deep-verify block (v2 status --json). May be absent entirely on
/// older backends — kept fully optional and every field tolerant.
struct VerifyStatus: Decodable {
    var running: Bool = false
    var startedAt: String? = nil
    var finishedAt: String? = nil
    var missingUploaded: Int = 0     // files that were missing on NAS, re-uploaded
    var extraFound: Int = 0          // extra files found on NAS
    var driftReuploaded: Int = 0     // content-drifted files re-uploaded
    var errors: [String] = []

    enum CodingKeys: String, CodingKey {
        case running
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case missingUploaded = "missing_uploaded"
        case extraFound = "extra_found"
        case driftReuploaded = "drift_reuploaded"
        case errors
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        running = c.lenient(Bool.self, .running) ?? false
        startedAt = c.lenient(String.self, .startedAt)
        finishedAt = c.lenient(String.self, .finishedAt)
        missingUploaded = c.lenient(Int.self, .missingUploaded) ?? 0
        extraFound = c.lenient(Int.self, .extraFound) ?? 0
        driftReuploaded = c.lenient(Int.self, .driftReuploaded) ?? 0
        errors = c.lenient([String].self, .errors) ?? []
    }

    /// A completed run has a finish timestamp; the result summary renders on this.
    var hasResult: Bool { !(finishedAt ?? "").isEmpty }
}

struct Status: Decodable {
    var state: String = "OFFLINE"
    var updatedAt: String? = nil
    var lastSyncOkAt: String? = nil
    var lastError: String? = nil
    var deletionDisabled: Bool = false
    var watcherDegraded: Bool = false
    var progress: Progress? = nil
    var folders: [String: FolderStatus] = [:]
    // Present only when read via `status --json` (ctl-computed), not in the raw
    // state.json the poller reads. Kept optional so both sources decode.
    var derived: Derived? = nil
    // v2 deep-verify block; absent on old backends (tolerated).
    var verify: VerifyStatus? = nil
    // Failing guard layer + mechanism while OFFLINE (absent when online).
    var guardInfo: GuardInfo? = nil
    // The daemon process that wrote this state (announced at startup).
    var daemonPid: Int? = nil
    var daemonStartedAt: String? = nil

    enum CodingKeys: String, CodingKey {
        case state
        case updatedAt = "updated_at"
        case lastSyncOkAt = "last_sync_ok_at"
        case lastError = "last_error"
        case deletionDisabled = "deletion_disabled"
        case watcherDegraded = "watcher_degraded"
        case progress, folders, derived, verify
        case guardInfo = "guard"
        case daemonPid = "daemon_pid"
        case daemonStartedAt = "daemon_started_at"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = c.lenient(String.self, .state) ?? "OFFLINE"
        updatedAt = c.lenient(String.self, .updatedAt)
        lastSyncOkAt = c.lenient(String.self, .lastSyncOkAt)
        lastError = c.lenient(String.self, .lastError)
        deletionDisabled = c.lenient(Bool.self, .deletionDisabled) ?? false
        watcherDegraded = c.lenient(Bool.self, .watcherDegraded) ?? false
        progress = c.lenient(Progress.self, .progress)
        folders = c.lenient([String: FolderStatus].self, .folders) ?? [:]
        derived = c.lenient(Derived.self, .derived)
        verify = c.lenient(VerifyStatus.self, .verify)
        guardInfo = c.lenient(GuardInfo.self, .guardInfo)
        daemonPid = c.lenient(Int.self, .daemonPid)
        daemonStartedAt = c.lenient(String.self, .daemonStartedAt)
    }

    // Computed rollups from the raw folders map (so the GUI does not depend on
    // the ctl-only `derived` block when polling state.json directly).
    var totalHeldDeletions: Int { folders.values.reduce(0) { $0 + $1.heldCount } }
    /// Some folder's list shows only part of its held deletions.
    var heldListTruncated: Bool { folders.values.contains { $0.heldTruncated } }
    /// totalHeldDeletions is a lower bound (an older backend, capped list).
    var heldCountUncertain: Bool { folders.values.contains { $0.heldCountUncertain } }
    var totalFailedFiles: Int { folders.values.reduce(0) { $0 + $1.failedFiles.count } }
    var deletionsHeld: Bool { totalHeldDeletions > 0 }
    var orphanedFolders: [String] {
        folders.filter { $0.value.orphaned }.map { $0.key }.sorted()
    }
    var hasActiveProgress: Bool { progress?.active ?? false }

    // iCloud cloud-only rollups (for the 问题 iCloud section / 设置 hints).
    var totalIcloudSkippedCount: Int {
        folders.values.reduce(0) { $0 + $1.icloudSkipped.count }
    }
    var totalIcloudSkippedBytes: Int {
        folders.values.reduce(0) { $0 + $1.icloudSkipped.bytes }
    }
    /// Folders with cloud-only files, sorted by name (drives the iCloud section).
    var icloudSkippedFolders: [(String, FolderStatus)] {
        folders.filter { $0.value.icloudSkipped.count > 0 }.sorted { $0.key < $1.key }
    }
    var deferredDeletionFolders: [(String, FolderStatus)] {
        folders.filter { ($0.value.deferredDeletions?.count ?? 0) > 0 }.sorted { $0.key < $1.key }
    }
}

// Optional ctl-computed rollup (status --json only). Mirrors the "derived" key.
struct Derived: Decodable {
    var totalHeldDeletions: Int = 0
    var totalFailedFiles: Int = 0
    var deletionsHeld: Bool = false
    var orphanedFolders: [String] = []
    var permanentlyFailingFiles: [String: [String]] = [:]

    enum CodingKeys: String, CodingKey {
        case totalHeldDeletions = "total_held_deletions"
        case totalFailedFiles = "total_failed_files"
        case deletionsHeld = "deletions_held"
        case orphanedFolders = "orphaned_folders"
        case permanentlyFailingFiles = "permanently_failing_files"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        totalHeldDeletions = c.lenient(Int.self, .totalHeldDeletions) ?? 0
        totalFailedFiles = c.lenient(Int.self, .totalFailedFiles) ?? 0
        deletionsHeld = c.lenient(Bool.self, .deletionsHeld) ?? false
        orphanedFolders = c.lenient([String].self, .orphanedFolders) ?? []
        permanentlyFailingFiles =
            c.lenient([String: [String]].self, .permanentlyFailingFiles) ?? [:]
    }
}

// MARK: - doctor --json

struct DoctorRow: Decodable, Identifiable {
    var rowId: String = ""
    var name: String = ""
    var ok: Bool = false
    var detail: String = ""
    var hint: String = ""
    // Present only on failing rows the backend knows how to repair (pinned
    // contract): a machine fix id + a human button label. Both nil otherwise.
    var fixId: String? = nil
    var fixLabel: String? = nil

    var id: String { rowId.isEmpty ? name : rowId }

    /// A one-click fix is offered only for a failing row that names a fix id.
    var offersFix: Bool { !ok && !(fixId ?? "").isEmpty }

    enum CodingKeys: String, CodingKey {
        case rowId = "id"
        case name, ok, detail, hint
        case fixId = "fix_id"
        case fixLabel = "fix_label"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rowId = c.lenient(String.self, .rowId) ?? ""
        name = c.lenient(String.self, .name) ?? ""
        ok = c.lenient(Bool.self, .ok) ?? false
        detail = c.lenient(String.self, .detail) ?? ""
        hint = c.lenient(String.self, .hint) ?? ""
        fixId = c.lenient(String.self, .fixId)
        fixLabel = c.lenient(String.self, .fixLabel)
    }
}

// MARK: - fix <fix_id>

/// Result of `ctl fix <fix_id>` (pinned contract):
/// {"ok": bool, "detail": string, "followup": "reopen_doctor"|null}.
struct FixResult: Decodable {
    var ok: Bool = false
    var detail: String = ""
    var followup: String? = nil   // "reopen_doctor" | nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, detail, followup, error
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.lenient(Bool.self, .ok) ?? false
        detail = c.lenient(String.self, .detail) ?? ""
        followup = c.lenient(String.self, .followup)
        error = c.lenient(String.self, .error)
    }
}

// MARK: - mount

struct MountResult: Decodable {
    var mounted: Bool = false
    var mountpoint: String? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case mounted, mountpoint, error
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mounted = c.lenient(Bool.self, .mounted) ?? false
        mountpoint = c.lenient(String.self, .mountpoint)
        error = c.lenient(String.self, .error)
    }
}

// MARK: - approve-deletions --yes

struct ApproveResult: Decodable {
    var approved: Int = 0
    var folders: [String: [String]] = [:]
    var signalled: Bool = false
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case approved, folders, signalled, error
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        approved = c.lenient(Int.self, .approved) ?? 0
        folders = c.lenient([String: [String]].self, .folders) ?? [:]
        signalled = c.lenient(Bool.self, .signalled) ?? false
        error = c.lenient(String.self, .error)
    }
}

// Generic error envelope: any ctl command emits {"error": "..."} on failure.
struct CtlError: Decodable {
    var error: String? = nil
}

// MARK: - server-set error envelope

/// Non-zero exit of `server-set`: a plain error, or a request for explicit
/// confirmation (`needs_confirm == "adopt"` when the share already carries a
/// different MyCloud sentinel -- taking it over makes this machine its writer).
struct ServerSetEnvelope: Decodable {
    var error: String? = nil
    var needsConfirm: String? = nil
    var foundSentinel: String? = nil

    enum CodingKeys: String, CodingKey {
        case error
        case needsConfirm = "needs_confirm"
        case foundSentinel = "found_sentinel"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        error = c.lenient(String.self, .error)
        needsConfirm = c.lenient(String.self, .needsConfirm)
        foundSentinel = c.lenient(String.self, .foundSentinel)
    }
}

// MARK: - state.json "guard" (why the mount guard is failing right now)

/// The daemon persists the failing guard layer and its mechanism so the GUI can
/// turn a bare OFFLINE into an actionable hint -- a sentinel `cat` denied with
/// "Operation not permitted" means /usr/bin/python3 lacks Full Disk Access.
struct GuardInfo: Decodable {
    var layer: String = ""
    var detail: String = ""
    var softPass: Bool = false

    enum CodingKeys: String, CodingKey {
        case layer, detail
        case softPass = "soft_pass"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        layer = c.lenient(String.self, .layer) ?? ""
        detail = c.lenient(String.self, .detail) ?? ""
        softPass = c.lenient(Bool.self, .softPass) ?? false
    }

    /// TCC refused the launchd daemon's read: Full Disk Access is missing. A
    /// mount failure is never that (mount_smbfs runs as a system tool), even
    /// when its message happens to contain the same words.
    var isPermissionDenied: Bool {
        layer != "mount" && detail.contains("Operation not permitted")
    }
}

// MARK: - install result

struct InstallResult: Decodable {
    var installed: Bool = false
    var online: String? = nil
    var fdaHint: Bool = false
    var notice: String? = nil

    enum CodingKeys: String, CodingKey {
        case installed, online, notice
        case fdaHint = "fda_hint"
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        installed = c.lenient(Bool.self, .installed) ?? false
        online = c.lenient(String.self, .online)
        fdaHint = c.lenient(Bool.self, .fdaHint) ?? false
        notice = c.lenient(String.self, .notice)
    }
}

// MARK: - version (installed backend vs the one bundled in this app)

struct VersionInfo: Decodable {
    var backendVersion: String = ""
    var commands: [String] = []

    enum CodingKeys: String, CodingKey {
        case backendVersion = "backend_version"
        case commands
    }

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        backendVersion = c.lenient(String.self, .backendVersion) ?? ""
        commands = c.lenient([String].self, .commands) ?? []
    }
}
