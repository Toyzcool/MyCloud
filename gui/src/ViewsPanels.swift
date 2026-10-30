// ViewsPanels.swift — the 同步项 / 问题 / 设置 panels split out of Views.swift.
// Shared helpers (stateLabel, relativeTimeCN, truncateMiddle, …) live in
// Views.swift. Chinese copy throughout; every write goes through AppState.

import SwiftUI
import AppKit

// MARK: - macOS 12 compatibility shims (gui-macos12)

/// Settings-style container for the 同步项 / 设置 panels.
/// macOS 13+: Form + .formStyle(.grouped). macOS 12: a List -- Monterey's bare
/// Form has no scroll container and mis-lays-out its label column (the 设置页
/// "乱版" report from the gateway, 2026-09-12; the earlier ScrollView wrapper
/// only cured the clipping). List scrolls, renders Section headers and gives
/// every row the full width on every supported version.
struct CompatForm<Content: View>: View {
    private let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        if #available(macOS 13.0, *) {
            Form { content }.formStyle(.grouped)
        } else {
            List { content }
        }
    }
}

/// LabeledContent is macOS 13+; this renders the same label-left/value-right
/// row on every supported macOS version — one code path, no fork.
struct LabeledRow<Content: View>: View {
    private let label: String
    private let content: Content

    init(_ label: String, @ViewBuilder content: () -> Content) {
        self.label = label
        self.content = content()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
            Spacer()
            content
                .multilineTextAlignment(.trailing)
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - 同步项 Sources

struct SourcesView: View {
    @EnvironmentObject var app: AppState

    @State private var pendingRemove: Source? = nil
    @State private var manualDest: String = ""
    @State private var pendingDest: String? = nil     // awaiting confirmation

    var body: some View {
        CompatForm {
            sourcesSection
            destSection
        }
        .navigationTitle("同步项")
        .toolbar {
            ToolbarItemGroup {
                Button {
                    pickSources(files: true)
                } label: { Label("添加文件", systemImage: "doc.badge.plus") }
                Button {
                    pickSources(files: false)
                } label: { Label("添加文件夹", systemImage: "folder.badge.plus") }
            }
        }
        // anyBusy too: ctl rewrites config.ini read-modify-write without a
        // lock, so a change racing a server-set could be lost.
        .disabled(!app.installed || app.anyBusy)
        // Confirmations act on the value handed to the alert (`presenting:`):
        // dismissal resets the @State before a button's Task could read it.
        .alert("移除同步项", isPresented: Binding(
            get: { pendingRemove != nil },
            set: { if !$0 { pendingRemove = nil } }
        ), presenting: pendingRemove) { src in
            Button("取消", role: .cancel) {}
            Button("移除", role: .destructive) {
                Task { await app.removeSource(path: src.path) }
            }
        } message: { src in
            Text("将不再同步「\(src.name)」。\nNAS 上已备份的镜像会保留为 orphaned（孤立），不会被移动或删除。")
        }
        // Dest-change confirmation.
        .alert("修改目标目录", isPresented: Binding(
            get: { pendingDest != nil },
            set: { if !$0 { pendingDest = nil } }
        ), presenting: pendingDest) { rel in
            Button("取消", role: .cancel) {}
            Button("确认修改") {
                Task { await app.setDest(rel: rel) }
            }
        } message: { rel in
            Text("新的落点为「\(rel)」。\n旧位置的数据会原样保留（标记为 orphaned），如不再需要请手动清理。")
        }
        .onAppear { manualDest = app.config?.destRel ?? "" }
        .onChange(of: app.config?.destRel) { new in manualDest = new ?? manualDest }
    }

    // MARK: sources list

    @ViewBuilder
    private var sourcesSection: some View {
        Section("同步项（文件或文件夹）") {
            if let cfg = app.config, !cfg.sources.isEmpty {
                ForEach(cfg.sources) { src in
                    HStack(spacing: 10) {
                        Image(systemName: src.isFile ? "doc" : "folder")
                            .foregroundColor(.accentColor)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(src.name).font(.body)
                            Text(truncateMiddle(src.path))
                                .font(.caption).foregroundColor(.secondary)
                        }
                        Spacer()
                    }
                    .contextMenu {
                        Button("删除", role: .destructive) { pendingRemove = src }
                    }
                    .swipeActions {
                        Button("删除", role: .destructive) { pendingRemove = src }
                    }
                }
            } else if app.config != nil {
                Text("尚未添加任何同步项。请用右上角按钮添加文件或文件夹。")
                    .foregroundColor(.secondary)
            } else {
                Text("加载配置中…").foregroundColor(.secondary)
            }
        }
    }

    // MARK: destination

    @ViewBuilder
    private var destSection: some View {
        Section("目标目录") {
            let cfg = app.config
            LabeledRow("当前落点") { Text(cfg?.destRel ?? "—") }
            LabeledRow("完整路径预览") {
                Text(destPreview(cfg)).font(.caption)
                    .foregroundColor(.secondary)
                    .textSelection(.enabled)
            }
            HStack {
                TextField("手动输入相对路径（如 MyCloud）", text: $manualDest)
                    .textFieldStyle(.roundedBorder)
                Button("应用") {
                    let v = manualDest.trimmingCharacters(in: .whitespaces)
                    if !v.isEmpty && v != cfg?.destRel { pendingDest = v }
                }
                .disabled(manualDest.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Button {
                browseForDest()
            } label: {
                Label("浏览 NAS 选择目录…", systemImage: "externaldrive.connected.to.line.below")
            }
            if app.isRunning("mount") {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("正在挂载 NAS…").font(.caption).foregroundColor(.secondary)
                }
            }
        }
    }

    private func destPreview(_ cfg: Config?) -> String {
        guard let c = cfg else { return "—" }
        let host = c.host.isEmpty ? "host" : c.host
        let share = c.share.isEmpty ? "share" : c.share
        let rel = c.destRel.isEmpty ? "MyCloud" : c.destRel
        return "//\(host)/\(share)/\(rel)/data/"
    }

    // MARK: NSOpenPanel flows

    private func pickSources(files: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = files
        panel.canChooseDirectories = !files
        panel.allowsMultipleSelection = true
        panel.prompt = "添加"
        panel.message = files ? "选择要同步的文件" : "选择要同步的文件夹"
        guard panel.runModal() == .OK else { return }
        let urls = panel.urls
        Task {
            for url in urls { await app.addSource(url: url) }
        }
    }

    private func browseForDest() {
        Task { @MainActor in
            guard let mp = await app.mountForBrowse() else { return }
            let mountURL = URL(fileURLWithPath: mp, isDirectory: true)
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = true
            panel.allowsMultipleSelection = false
            panel.directoryURL = mountURL
            panel.prompt = "选择"
            panel.message = "在已挂载的 NAS 中选择备份落点目录"
            guard panel.runModal() == .OK, let chosen = panel.url else { return }
            // Compute path relative to the mountpoint; reject anything outside.
            let base = mountURL.standardizedFileURL.path
                .hasSuffix("/") ? mountURL.standardizedFileURL.path
                : mountURL.standardizedFileURL.path + "/"
            let picked = chosen.standardizedFileURL.path
            guard picked == mountURL.standardizedFileURL.path || picked.hasPrefix(base) else {
                app.lastError = "所选目录必须位于挂载点内：\(mp)"
                return
            }
            var rel = String(picked.dropFirst(mountURL.standardizedFileURL.path.count))
            while rel.hasPrefix("/") { rel.removeFirst() }
            if rel.isEmpty {
                app.lastError = "请选择挂载点下的一个子目录作为落点。"
                return
            }
            pendingDest = rel
        }
    }
}

// MARK: - 问题 Problems

/// A fix awaiting a confirmation alert before it runs (destructive / semantic
/// changes only). `key` is the doctor row id used for busy + inline result.
private struct PendingFix: Identifiable {
    let id = UUID()
    let key: String
    let fixId: String
}

struct ProblemsView: View {
    @EnvironmentObject var app: AppState
    @State private var showApproveConfirm = false
    @State private var pendingFix: PendingFix? = nil

    private var status: Status? { app.status }

    /// Folders (sorted) that currently have held deletions.
    private var heldFolders: [(String, FolderStatus)] {
        (status?.folders ?? [:])
            .filter { !$0.value.heldDeletions.isEmpty }
            .sorted { $0.key < $1.key }
    }

    /// Folders (sorted) with permanently failed files.
    private var failedFolders: [(String, FolderStatus)] {
        (status?.folders ?? [:])
            .filter { !$0.value.failedFiles.isEmpty }
            .sorted { $0.key < $1.key }
    }

    private var totalHeld: Int { status?.totalHeldDeletions ?? 0 }
    /// "N", or "至少 N" when an older backend persisted only the capped list.
    private var heldText: String {
        (status?.heldCountUncertain ?? false) ? "至少 \(totalHeld)" : "\(totalHeld)"
    }

    // iCloud cloud-only rollups (from the folders map via the model helpers).
    private var icloudFolders: [(String, FolderStatus)] { status?.icloudSkippedFolders ?? [] }
    private var totalIcloudCount: Int { status?.totalIcloudSkippedCount ?? 0 }
    private var totalIcloudBytes: Int { status?.totalIcloudSkippedBytes ?? 0 }
    /// Current iCloud policy from config; defaults to "skip" if config not loaded.
    private var icloudPolicy: String { app.config?.icloudPolicy ?? "skip" }
    /// Verify errors present (non-empty) — treated as a problem for 一切正常 gating.
    private var hasVerifyErrors: Bool { !(status?.verify?.errors.isEmpty ?? true) }

    /// The daemon itself is not working (dead/wedged, stalled, exited, offline).
    private var daemonProblem: Bool {
        guard app.installed, let s = status else { return false }
        return app.unresponsive || app.syncStalled || app.agentExited
            || s.state.uppercased() == "OFFLINE"
    }

    private var hasAnyProblem: Bool {
        guard let s = status else { return false }
        return daemonProblem
            || s.deletionDisabled || totalHeld > 0 || !failedFolders.isEmpty
            || !s.orphanedFolders.isEmpty || s.watcherDegraded
            || (s.lastError != nil && !(s.lastError ?? "").isEmpty)
            || totalIcloudCount > 0 || hasVerifyErrors
            || !(s.deferredDeletionFolders.isEmpty)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let s = status {
                    if daemonProblem { daemonRow(s) }
                    if s.deletionDisabled { deletionDisabledBanner }
                    if totalHeld > 0 { heldSection }
                    if !failedFolders.isEmpty { failedSection }
                    if totalIcloudCount > 0 { icloudSection }
                    if !s.deferredDeletionFolders.isEmpty {
                        deferredSection(s.deferredDeletionFolders)
                    }
                    if !s.orphanedFolders.isEmpty { orphanedSection(s.orphanedFolders) }
                    if s.watcherDegraded { watcherRow }
                    if let e = s.lastError, !e.isEmpty { lastErrorRow(e) }
                    if let v = s.verify, v.hasResult { verifyResultSection(v) }
                    if !hasAnyProblem { allClear }
                } else {
                    Text("暂无状态数据。").foregroundColor(.secondary)
                }
                Divider()
                doctorSection
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .navigationTitle("问题")
        .alert("批准全部删除", isPresented: $showApproveConfirm) {
            Button("取消", role: .cancel) {}
            Button("批准删除 \(heldText) 项", role: .destructive) {
                Task { await app.approveDeletions() }
            }
        } message: {
            Text("将放行当前冻结的 \(heldText) 个删除操作（列表可能只显示了其中一部分），对应的 NAS 镜像会被归档进回收站。此动作会真正传播删除，请确认。")
        }
        // (The old "switch icloud_policy to download" confirmation is gone: the
        // daemon cannot perform those downloads, so the button now runs
        // materialize in this app instead -- see icloudSection.)
        // Confirm gate for the fixes that change semantics or delete data.
        .alert("确认操作", isPresented: Binding(
            get: { pendingFix != nil },
            set: { if !$0 { pendingFix = nil } }
        ), presenting: pendingFix) { pf in
            Button("取消", role: .cancel) { pendingFix = nil }
            Button(fixConfirmLabel(pf.fixId),
                   role: pf.fixId == "prune_trash_now" ? .destructive : nil) {
                let key = pf.key, fid = pf.fixId
                Task { await app.runFix(fid, key: key) }
                pendingFix = nil
            }
        } message: { pf in
            Text(fixConfirmMessage(pf.fixId))
        }
    }

    // MARK: one-click fixes

    /// Route a tapped fix: confirm the destructive/semantic ones first, run the
    /// rest (restart_daemon, mount_share, open_* …) immediately.
    private func onFixTapped(_ row: DoctorRow) {
        guard let fixId = row.fixId, !fixId.isEmpty else { return }
        if fixId == "rerun_capability_probe" || fixId == "prune_trash_now" {
            pendingFix = PendingFix(key: row.id, fixId: fixId)
        } else {
            Task { await app.runFix(fixId, key: row.id) }
        }
    }

    private func fixConfirmLabel(_ fixId: String) -> String {
        switch fixId {
        case "prune_trash_now": return "立即清理"
        case "rerun_capability_probe": return "重新检测"
        default: return "继续"
        }
    }

    private func fixConfirmMessage(_ fixId: String) -> String {
        switch fixId {
        case "rerun_capability_probe":
            return "将重新检测 rsync 行为指纹，一致才会恢复删除同步。"
        case "prune_trash_now":
            return "将立即永久删除超过保留期的回收站历史。此操作不可撤销。"
        default:
            return "确认执行该修复？"
        }
    }

    // MARK: banners & sections

    /// Why 一切正常 is not shown when the folders look fine: the daemon itself.
    private func daemonRow(_ s: Status) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: stateSymbol(app.displayState, installed: app.installed))
                .foregroundColor(stateColor(app.displayState, installed: app.installed))
            VStack(alignment: .leading, spacing: 4) {
                Text("守护进程：\(stateLabel(app.displayState, installed: app.installed))")
                    .font(.headline)
                if let g = s.guardInfo, !g.detail.isEmpty {
                    Text(g.isPermissionDenied
                         ? "系统拒绝读取 NAS：需要为 /usr/bin/python3 授予「完全磁盘访问」。"
                         : (g.layer == "mount" ? "无法挂载 NAS：\(g.detail)"
                                               : "挂载检查失败（\(g.layer)）：\(g.detail)"))
                        .font(.caption).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button("前往「概览」处理") { app.selectedSection = .overview }
            }
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    private var deletionDisabledBanner: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.octagon.fill").foregroundColor(.red)
            VStack(alignment: .leading, spacing: 4) {
                Text("能力指纹变化，删除传播已停用").font(.headline).foregroundColor(.red)
                Text("openrsync 行为发生变化，删除传播已冻结（复制仍在继续）。请运行诊断确认后再恢复。")
                    .font(.callout).foregroundColor(.secondary)
                Button("查看诊断") { Task { await app.runDoctor() } }
                    .disabled(app.anyBusy)
            }
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.12)))
    }

    private var heldSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("待确认删除（共 \(heldText) 项）", systemImage: "trash.slash")
                    .font(.headline)
                Spacer()
                Button("批准全部删除") { showApproveConfirm = true }
                    .disabled(!app.installed || app.anyBusy)
            }
            if status?.heldListTruncated ?? false {
                Text("列表每个同步项只记录前 1000 项；批准会放行全部 \(heldText) 项。")
                    .font(.caption).foregroundColor(.orange)
            }
            ForEach(heldFolders, id: \.0) { name, fs in
                DisclosureGroup("\(name)（\(fs.heldCountUncertain ? "至少 " : "")\(fs.heldCount) 项）") {
                    ForEach(fs.heldDeletions, id: \.self) { rel in
                        Text(truncateMiddle(rel, max: 64))
                            .font(.caption).foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    private var failedSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("永久失败清单", systemImage: "xmark.circle").font(.headline)
            Text("以下文件多次同步失败。若目标为 exFAT/FAT，常见原因是文件名或时间戳不兼容，可尝试重命名后重试。")
                .font(.caption).foregroundColor(.secondary)
            ForEach(failedFolders, id: \.0) { name, fs in
                DisclosureGroup("\(name)（\(fs.failedFiles.count) 项）") {
                    ForEach(fs.failedFiles, id: \.self) { rel in
                        Text(truncateMiddle(rel, max: 64))
                            .font(.caption).foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    /// Deletions the daemon holds back because an evicted iCloud file (macOS
    /// <= 13 placeholder) may have moved there: informational, no action.
    private func deferredSection(_ folders: [(String, FolderStatus)]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("暂缓的删除", systemImage: "pause.circle").font(.headline)
            Text("这些文件在本机已不存在，但可能只是被移动了、目前仅存于 iCloud 云端。为免误删唯一的备份，NAS 上的副本暂时保留；文件下载回本机并备份后，删除会正常处理。")
                .font(.caption).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(folders, id: \.0) { name, fs in
                DisclosureGroup("\(name)（\(fs.deferredDeletions?.count ?? 0) 个）") {
                    ForEach(fs.deferredDeletions?.samples ?? [], id: \.self) { rel in
                        Text(truncateMiddle(rel, max: 64))
                            .font(.caption).foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    // iCloud cloud-only files. Under "skip" this is an actionable section (with a
    // per-folder breakdown + a button to switch to download); under "download"
    // it is a passive "pending, in progress" row.
    @ViewBuilder
    private var icloudSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if icloudPolicy == "skip" {
                Label("仅存于 iCloud 云端、未包含在备份", systemImage: "icloud.and.arrow.down")
                    .font(.headline).foregroundColor(.orange)
                Text("以下文件尚未下载到本机（iCloud 优化存储），因此当前不在备份范围内。")
                    .font(.caption).foregroundColor(.secondary)
                ForEach(icloudFolders, id: \.0) { name, fs in
                    DisclosureGroup(
                        "\(name)（\(fs.icloudSkipped.count) 个文件 / \(gbString(fs.icloudSkipped.bytes)) GB）"
                    ) {
                        ForEach(fs.icloudSkipped.samples, id: \.self) { rel in
                            Text(truncateMiddle(rel, max: 64))
                                .font(.caption).foregroundColor(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                Text("后台守护进程受 macOS 限制无法触发 iCloud 下载，因此由本应用在前台分批下载；下载完成后守护进程会自动把它们备份到 NAS。")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    if app.materializing {
                        Button("取消下载") { app.cancelMaterialize() }
                    } else {
                        Button {
                            Task { @MainActor in await app.materializeCloudFiles() }
                        } label: {
                            Label("立即下载到本机", systemImage: "icloud.and.arrow.down")
                        }
                        .disabled(!app.installed || app.anyBusy)
                    }
                    if let p = app.materializeProgress {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text(p).font(.caption).foregroundColor(.secondary)
                        }
                    }
                }
            } else {
                // policy == download: files are queued and processed in batches.
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "icloud.and.arrow.down").foregroundColor(.blue)
                    Text("待下载备份 \(totalIcloudCount) 个文件（后台分批进行中，可在设置调整）。")
                        .font(.callout).foregroundColor(.secondary)
                    Spacer()
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    // Result of the last completed deep verify (finished_at present).
    private func verifyResultSection(_ v: VerifyStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("深度校验", systemImage: "checkmark.shield").font(.headline)
            // "已排队重传", not "补传": these counts are rows queued for the
            // follow-up pass, not files already uploaded.
            Text("上次深度校验：缺失 \(v.missingUploaded) / 多余 \(v.extraFound) / 内容不一致 \(v.driftReuploaded)（缺失与不一致的文件已排队重传）")
                .font(.callout).foregroundColor(.secondary)
            Text("完成于 \(relativeTimeCN(v.finishedAt))")
                .font(.caption).foregroundColor(.secondary)
            if !v.errors.isEmpty {
                DisclosureGroup("校验错误（\(v.errors.count) 项）") {
                    ForEach(v.errors, id: \.self) { e in
                        Text(e).font(.caption).foregroundColor(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    private func orphanedSection(_ names: [String]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("孤立目录（orphaned）", systemImage: "archivebox").font(.headline)
            Text("这些镜像的同步项已被移除或落点已变更，NAS 上的数据原样保留、不再更新。确认无用后可在 NAS 手动删除。")
                .font(.caption).foregroundColor(.secondary)
            ForEach(names, id: \.self) { n in
                Text("• \(n)").font(.callout).foregroundColor(.secondary)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    private var watcherRow: some View {
        HStack(spacing: 10) {
            Image(systemName: "eye.trianglebadge.exclamationmark").foregroundColor(.orange)
            Text("kqueue 监视降级为定时扫描模式（同步仍正常进行，仅实时性下降）。")
                .font(.callout)
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }

    private func lastErrorRow(_ e: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.bubble").foregroundColor(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text("最近错误").font(.headline)
                Text(e).font(.callout).foregroundColor(.secondary)
            }
            Spacer()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    private var allClear: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.seal.fill").foregroundColor(.green)
                .font(.title2)
            Text("一切正常，暂无待处理问题。").font(.headline)
        }
        .padding(.vertical, 8)
    }

    // MARK: doctor

    @ViewBuilder
    private var doctorSection: some View {
        HStack {
            Text("诊断").font(.headline)
            Spacer()
            Button {
                Task { await app.runDoctor() }
            } label: {
                if app.isRunning("doctor") {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("运行中…")
                    }
                } else {
                    Label("运行诊断", systemImage: "stethoscope")
                }
            }
            .disabled(!app.installed || app.anyBusy)
        }
        if app.doctor.isEmpty {
            Text("点击「运行诊断」检查挂载、rsync 能力、目标可写性等。")
                .font(.caption).foregroundColor(.secondary)
        } else {
            VStack(spacing: 0) {
                ForEach(app.doctor) { row in
                    doctorRow(row)
                    Divider()
                }
            }
        }
    }

    private func doctorRow(_ row: DoctorRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: row.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundColor(row.ok ? .green : .red)
            VStack(alignment: .leading, spacing: 4) {
                Text(doctorNameCN(row)).font(.callout)
                if !row.detail.isEmpty {
                    Text(row.detail).font(.caption).foregroundColor(.secondary)
                }
                if !row.ok && !row.hint.isEmpty {
                    Text("建议：\(row.hint)").font(.caption).foregroundColor(.orange)
                }
                // One-click fix for a failing row the backend can repair.
                if row.offersFix {
                    Button {
                        onFixTapped(row)
                    } label: {
                        if app.fixBusyId == row.id {
                            HStack(spacing: 6) {
                                ProgressView().controlSize(.small)
                                Text("处理中…")
                            }
                        } else {
                            Label(row.fixLabel ?? "一键修复",
                                  systemImage: "wrench.and.screwdriver")
                        }
                    }
                    .controlSize(.small)
                    .disabled(!app.installed || app.anyBusy)
                }
                if let outcome = app.fixResults[row.id] {
                    Text(outcome.detail).font(.caption)
                        .foregroundColor(outcome.ok ? .green : .red)
                }
            }
            Spacer()
        }
        .padding(.vertical, 6)
    }
}

/// Chinese label for a doctor check, keyed by its stable `id` (the ctl output
/// itself stays English so the CLI/tests are unaffected; the GUI localizes the
/// visible check NAME here — PLAN 12.3 中文界面). The dynamic English detail/hint
/// are shown as-is. Unknown ids fall back to the backend name.
func doctorNameCN(_ row: DoctorRow) -> String {
    let base = row.rowId.split(separator: ":").first.map(String.init) ?? row.rowId
    let map: [String: String] = [
        "python_clt": "python3 版本与命令行工具",
        "config_loads": "配置文件可加载",
        "tcp445": "NAS 445 端口可达",
        "mount_present": "SMB 共享已挂载",
        "sentinel": "挂载哨兵匹配安装 UUID",
        "smb_session": "SMB 会话存活",
        "bare_dir_mode": "未挂载时裸挂载点权限为 0500",
        "rsync_capability": "rsync 能力指纹符合预期",
        "rsync_upload": "rsync 上传命令自检",
        "fda_scandir": "完全磁盘访问（可读取备份源）",
        "nas_space": "NAS 剩余空间与回收站容量",
        "launchagent": "LaunchAgent 已加载",
        "state_fresh": "state.json 新鲜度",
    ]
    if let cn = map[base] {
        // For per-folder scandir rows, keep the folder name visible.
        if base == "fda_scandir" && row.rowId.contains(":") {
            let folder = row.rowId.split(separator: ":").dropFirst()
                .joined(separator: ":")
            return "\(cn)：\(folder)"
        }
        return cn
    }
    return row.name
}

// MARK: - 设置 Settings

/// One integer setting: a Stepper + TextField pair with inline range validation.
/// Commits through the supplied async closure only when the value is in range
/// and actually changed; a rejected commit rolls the field back.
struct IntSettingRow: View {
    let title: String
    let unit: String
    let range: ClosedRange<Int>
    let current: Int
    /// Returns whether the backend accepted the value.
    let commit: (Int) async -> Bool

    @State private var value: Int = 0

    private var outOfRange: Bool { !range.contains(value) }
    /// "Editing" is simply "differs from the stored value": the old flag was
    /// set by onAppear's own assignment, after which the row ignored every
    /// later change of the stored value.
    private var editing: Bool { value != current }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(title)
                Spacer()
                TextField("", value: $value, format: .number)
                    .frame(width: 80)
                    .multilineTextAlignment(.trailing)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { apply() }
                if !unit.isEmpty { Text(unit).foregroundColor(.secondary) }
                Stepper("", value: $value, in: range)
                    .labelsHidden()
            }
            if outOfRange {
                Text("请输入 \(range.lowerBound)–\(range.upperBound) 之间的值。")
                    .font(.caption).foregroundColor(.red)
            }
            if editing && !outOfRange {
                HStack(spacing: 10) {
                    Button("应用") { apply() }.font(.caption)
                    Button("还原") { value = current }.font(.caption)
                }
            }
        }
        .onAppear { value = current }
        .onChange(of: current) { new in value = new }
    }

    private func apply() {
        guard range.contains(value), value != current else { return }
        let wanted = value
        Task { @MainActor in
            if !(await commit(wanted)) { value = current }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var app: AppState
    @State private var pendingIcloudDownload = false

    // 云盘连接 drafts: edited locally, committed by 测试并保存 (ctl server-set).
    // Re-seeded from config whenever the stored connection changes.
    @State private var hostDraft = ""
    @State private var shareDraft = ""
    @State private var anonymousDraft = false
    @State private var usernameDraft = ""
    @State private var passwordDraft = ""
    @State private var pendingServerApply = false
    @State private var pendingUninstall = false

    private var appVersion: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "—"
    }

    /// Identity of the stored connection; a change re-seeds the drafts.
    private var connectionKey: String {
        guard let c = app.config else { return "" }
        return [c.host, c.share, c.username, c.authMode].joined(separator: "|")
    }

    var body: some View {
        CompatForm {
            if let cfg = app.config {
                connectionSection(cfg)
                policySection(cfg)
                watchSection(cfg)
                icloudSection(cfg)
            } else if app.installed {
                Section { Text("加载配置中…").foregroundColor(.secondary) }
            }
            daemonSection
            Section {
                Text("MyCloud 版本 \(appVersion)")
                    .font(.caption).foregroundColor(.secondary)
            }
        }
        .navigationTitle("设置")
        .onAppear { seedConnectionDrafts() }
        .onChange(of: connectionKey) { _ in seedConnectionDrafts() }
        // Changing host/share re-targets the backup: confirm before applying.
        .alert("更换备份目标", isPresented: $pendingServerApply) {
            Button("取消", role: .cancel) {}
            Button("确认更换") { Task { @MainActor in await applyServer() } }
        } message: {
            Text("将连接到新的服务器/共享并重建备份清单，对新目标重新全量备份。旧目标上的数据原样保留，不会被移动或删除。")
        }
        .alert("卸载 MyCloud？", isPresented: $pendingUninstall) {
            Button("取消", role: .cancel) {}
            Button("卸载", role: .destructive) {
                Task { @MainActor in await app.uninstall() }
            }
        } message: {
            Text("将停止并移除本机的守护进程、登录启动项和配置（先备份）。NAS 上已备份的数据原样保留，不会被移动或删除。")
        }
        // (The 接管现有备份？ prompt is ContentView's root alert, so the
        // install wizard can show it too.)
        // Confirm switching the iCloud policy to download (bulk materialize).
        .alert("切换为下载后备份", isPresented: $pendingIcloudDownload) {
            Button("取消", role: .cancel) {}
            Button("确认切换") {
                Task { await app.setSetting(key: "icloud_policy", value: "download") }
            }
        } message: {
            Text("将开始分批下载仅存于 iCloud 的云端文件并纳入备份。文件较多时会耗费较长时间与网络流量，默认每批备份后自动释放本地空间。")
        }
    }

    // MARK: 云盘连接

    private func seedConnectionDrafts() {
        guard let cfg = app.config else { return }
        hostDraft = cfg.host
        shareDraft = cfg.share
        anonymousDraft = (cfg.authMode == "anonymous")
        usernameDraft = anonymousDraft ? "" : cfg.username
        passwordDraft = ""
    }

    private var trimmedHost: String { hostDraft.trimmingCharacters(in: .whitespaces) }
    private var trimmedShare: String { shareDraft.trimmingCharacters(in: .whitespaces) }
    private var trimmedUser: String { usernameDraft.trimmingCharacters(in: .whitespaces) }

    /// Same normalisation as ctl: case-insensitive, ignoring surrounding
    /// slashes -- `SHARE/` vs `share` is not a target change.
    private func norm(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespaces)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .lowercased()
    }

    private func targetChanged(_ cfg: Config) -> Bool {
        !cfg.host.isEmpty
            && (norm(hostDraft) != norm(cfg.host) || norm(shareDraft) != norm(cfg.share))
    }

    private var canApplyServer: Bool {
        guard !trimmedHost.isEmpty, !trimmedShare.isEmpty else { return false }
        return anonymousDraft || !trimmedUser.isEmpty
    }

    private func applyServer() async {
        let secret = passwordDraft
        passwordDraft = ""   // never keep the secret in view state
        await app.setServer(host: trimmedHost, share: trimmedShare,
                            anonymous: anonymousDraft, username: trimmedUser,
                            password: secret)
    }

    private func passwordPlaceholder(_ cfg: Config) -> String {
        let sameCredential = cfg.authMode == "user" && cfg.username == trimmedUser
            && !targetChanged(cfg)
        return sameCredential ? "留空则沿用已保存的密码" : "必填"
    }

    private func inputRow<F: View>(_ label: String,
                                   @ViewBuilder field: () -> F) -> some View {
        HStack {
            Text(label)
            Spacer()
            field()
                .frame(width: 240)
                .textFieldStyle(.roundedBorder)
                .disabled(app.anyBusy)
        }
    }

    private func connectionSection(_ cfg: Config) -> some View {
        Section("云盘连接") {
            inputRow("服务器地址") { TextField("例如 192.168.5.1", text: $hostDraft) }
            inputRow("共享名") { TextField("例如 SHARE", text: $shareDraft) }
            Picker("连接模式", selection: $anonymousDraft) {
                Text("注册用户（用户名 + 密码）").tag(false)
                Text("匿名（guest，无需密码）").tag(true)
            }
            .disabled(app.anyBusy)
            if !anonymousDraft {
                inputRow("用户名") { TextField("例如 admin", text: $usernameDraft) }
                inputRow("密码") {
                    SecureField(passwordPlaceholder(cfg), text: $passwordDraft)
                }
            }
            Text(anonymousDraft
                 ? "匿名模式使用 SMB guest 会话，NAS 需允许该共享的匿名访问；将不再使用该服务器的已存密码（从 nsmb.conf 移除，钥匙串旧条目保留）。"
                 : "密码保存在登录钥匙串，并写入 nsmb.conf（权限 600）供后台守护进程使用；不会出现在命令行参数或日志中。保存前会先用该密码实测连接。")
                .font(.caption).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            // Always present (a blank when not applicable): inserting/removing
            // a row while the user is typing makes Monterey's List rebuild the
            // section and drop the text field's focus.
            Text(targetChanged(cfg)
                 ? "更换服务器或共享名后，备份清单将重建并对新目标重新全量备份；旧目标上的数据原样保留。"
                 : " ")
                .font(.caption).foregroundColor(.orange)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button {
                    if targetChanged(cfg) {
                        pendingServerApply = true
                    } else {
                        Task { @MainActor in await applyServer() }
                    }
                } label: {
                    if app.isRunning("server-set") {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("正在验证连接…")
                        }
                    } else {
                        Label("测试并保存", systemImage: "checkmark.circle")
                    }
                }
                .disabled(!canApplyServer || app.anyBusy)
                Spacer()
                Text("当前：//\(cfg.authMode == "anonymous" ? "guest" : cfg.username)@\(cfg.host)/\(cfg.share)")
                    .font(.caption).foregroundColor(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private func policySection(_ cfg: Config) -> some View {
        Section("回收站与阈值") {
            Group {
            IntSettingRow(title: "回收站保留期", unit: "天", range: 1...365,
                          current: cfg.retentionDays) { v in
                await app.setSetting(key: "retention_days", value: String(v))
            }
            IntSettingRow(title: "回收站容量上限", unit: "GB", range: 1...500,
                          current: cfg.trashMaxGb) { v in
                await app.setSetting(key: "trash_max_gb", value: String(v))
            }
            IntSettingRow(title: "删除审批阈值（文件数）", unit: "个", range: 1...100000,
                          current: cfg.maxDeleteFiles) { v in
                await app.setSetting(key: "max_delete_files", value: String(v))
            }
            IntSettingRow(title: "删除审批阈值（占比）", unit: "%", range: 1...100,
                          current: cfg.maxDeletePct) { v in
                await app.setSetting(key: "max_delete_pct", value: String(v))
            }
            }
            .disabled(app.anyBusy)
        }
    }

    private func watchSection(_ cfg: Config) -> some View {
        Section("扫描与监视") {
            Group {
            IntSettingRow(title: "热点扫描间隔", unit: "秒", range: 5...600,
                          current: cfg.hotInterval) { v in
                await app.setSetting(key: "hot_interval", value: String(v))
            }
            IntSettingRow(title: "全量扫描间隔", unit: "秒", range: 60...86400,
                          current: cfg.fullInterval) { v in
                await app.setSetting(key: "full_interval", value: String(v))
            }
            Picker("监视模式", selection: Binding(
                get: { cfg.watchMode },
                set: { newVal in
                    if newVal != cfg.watchMode {
                        Task { await app.setSetting(key: "watch_mode", value: newVal) }
                    }
                }
            )) {
                Text("自动").tag("auto")
                Text("kqueue").tag("kqueue")
                Text("定时扫描").tag("scan")
            }
            }
            .disabled(app.anyBusy)
        }
    }

    private func icloudSection(_ cfg: Config) -> some View {
        Section("iCloud 云端文件") {
            Picker("iCloud 策略", selection: Binding(
                get: { cfg.icloudPolicy },
                set: { newVal in
                    guard newVal != cfg.icloudPolicy else { return }
                    if newVal == "download" {
                        // Gate the switch behind a confirmation; the picker snaps
                        // back to the current value until the user confirms.
                        pendingIcloudDownload = true
                    } else {
                        Task { await app.setSetting(key: "icloud_policy", value: newVal) }
                    }
                }
            )) {
                Text("跳过").tag("skip")
                Text("下载后备份").tag("download")
            }
            .disabled(app.anyBusy)
            Toggle("备份后自动释放本地空间", isOn: Binding(
                get: { cfg.icloudEvictAfterBackup },
                set: { newVal in
                    Task {
                        await app.setSetting(key: "icloud_evict_after_backup",
                                             value: newVal ? "true" : "false")
                    }
                }
            ))
            .disabled(cfg.icloudPolicy != "download" || app.anyBusy)
            Text("「下载后备份」会分批把仅存于 iCloud 的云端文件下载到本机并备份到 NAS。开启释放后，每批备份完成即从本机移除，避免占满磁盘。")
                .font(.caption).foregroundColor(.secondary)

            Button {
                Task { await app.runDeepVerify() }
            } label: {
                if app.fixBusyId == "run_deep_verify" {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("校验中…")
                    }
                } else {
                    Label("立即校验备份", systemImage: "checkmark.shield")
                }
            }
            .disabled(!app.installed || app.anyBusy)
            if let r = app.fixResults["run_deep_verify"] {
                Text(r.detail).font(.caption).foregroundColor(r.ok ? .green : .red)
            }
            Text("MyCloud 每周自动深度校验一次，确保 NAS 备份与本机一致；也可随时手动校验。")
                .font(.caption).foregroundColor(.secondary)
        }
    }

    @ViewBuilder
    private var daemonSection: some View {
        Section("守护进程") {
            LabeledRow("安装状态") {
                Label(app.installed ? "已安装" : "未安装",
                      systemImage: app.installed ? "checkmark.circle.fill" : "xmark.circle.fill")
                    .foregroundColor(app.installed ? .green : .red)
            }
            if app.installed {
                LabeledRow("后端版本") {
                    HStack(spacing: 6) {
                        Text(app.backendVersion ?? "未知")
                        if app.backendOutdated {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundColor(.orange)
                        }
                    }
                }
                if app.backendOutdated {
                    Text("已安装的后端比本应用（\(app.appVersion)）旧。旧后端不认识新命令，"
                         + "会让「云盘连接」等操作失败。更新只替换程序文件并重启守护进程，"
                         + "连接设置与备份源保持不变。")
                        .font(.caption).foregroundColor(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Task { @MainActor in await app.refreshBackend() }
                } label: {
                    if app.isRunning("refresh") {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small); Text("正在更新…")
                        }
                    } else {
                        Label(app.backendOutdated ? "更新后端（推荐）" : "重新安装后端文件",
                              systemImage: "arrow.triangle.2.circlepath")
                    }
                }
                .disabled(app.anyBusy || app.materializing || !app.hasBundledBackend)
                Button {
                    app.openLogFolder()
                } label: {
                    Label("打开日志文件夹", systemImage: "doc.text.magnifyingglass")
                }
                Button(role: .destructive) {
                    pendingUninstall = true
                } label: {
                    Label("卸载 MyCloud 守护进程…", systemImage: "trash")
                }
                .disabled(app.anyBusy || app.materializing)
                Text("卸载只移除本机的守护进程、登录启动项、配置与命令行工具（配置和日志会先备份到 Application Support/MyCloud.bak-日期）。NAS 上的备份数据不会有任何改动。")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("尚未安装：请在概览页的安装向导中完成安装。")
                    .font(.caption).foregroundColor(.secondary)
            }
        }
    }
}

/// Open a project doc via NSWorkspace, trying the dev repo root, the backend's
/// parent dir, then the app bundle resources. Falls back to revealing appDir.
func openProjectDoc(_ name: String) {
    let fm = FileManager.default
    var candidates: [URL] = []
    if let repo = ProcessInfo.processInfo.environment["MYCLOUD_REPO"], !repo.isEmpty {
        candidates.append(URL(fileURLWithPath: repo).appendingPathComponent(name))
    }
    candidates.append(Bridge.appDir.deletingLastPathComponent()
        .appendingPathComponent(name))
    if let res = Bundle.main.resourceURL {
        candidates.append(res.appendingPathComponent(name))
    }
    for url in candidates where fm.fileExists(atPath: url.path) {
        NSWorkspace.shared.open(url)
        return
    }
    NSWorkspace.shared.open(Bridge.appDir)
}
