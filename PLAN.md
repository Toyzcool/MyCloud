# MyCloud 实施方案

> 版本 v1.0（2026-07-18）· 方案制定：Fable 5 · 代码实现：Opus 4.8
> 目标：在 macOS 上实现「开机自启动、iCloud 式」的本地文件夹 → 路由器 SMB 硬盘单向自动备份。

---

## 1. 项目目标与需求分析

**用户原始需求**：电脑启动时自动将指定文件同步到路由器 Samba 硬盘（192.168.5.1，用户名 admin）作为备份；文件有更新变动时自动同步，体验类似 iCloud。

拆解为硬性需求：

| # | 需求 | 说明 |
|---|------|------|
| R1 | 登录自启 | launchd LaunchAgent，安装后无需任何手动操作 |
| R2 | 启动全量 + 变更增量 | 启动做一次全量校验同步，之后文件变动自动增量同步 |
| R3 | 单向备份 | 本机是唯一数据源，NAS 是备份目标（不做多设备双向同步） |
| R4 | 删除安全 | 本地删除会传播，但**永不**直接销毁 NAS 上唯一副本（回收站版本化） |
| R5 | 断线自愈 | NAS 开机离线、路由器重启、休眠唤醒、换网络，全部无人工干预自动恢复 |
| R6 | 防灾难写入 | 挂载点失效时**绝不能**把文件写进本地磁盘（经典 /Volumes 假挂载点灾难） |
| R7 | 配置驱动 | 用户只编辑一个配置文件：备份哪些文件夹、排除规则、保留期 |
| R8 | 可观测 | 日志 + 状态查询 + 持续失败时 macOS 通知 |
| R9 | 可安装/可卸载 | install.sh / uninstall.sh，卸载不碰 NAS 上的备份数据 |

**明确不做**（v1 范围外）：多设备双向同步、文件按需下载（iCloud 的 Files On-Demand）、菜单栏 GUI（预留接口，见 §6.9）。

---

## 2. 已核实的环境事实（2026-07-18 本机实测）

以下事实全部在目标机器上验证过，**编码时以此为准，不得假设其他环境**：

- **Mac**：macOS 26.6（Darwin 25.6.0），**arm64**，单用户 `toyz`。
- **Python**：`/usr/bin/python3` = **3.9.6**（CLT shim，代码必须以 3.9 为目标：无 tomllib、无 match 语句）。⚠️ `/usr/local/bin/python3`（3.11.15）和 `/usr/local/bin/brew` 均为 **x86_64/Rosetta**，处于 Homebrew 弃用路径，**禁止依赖**。
- **rsync**：`/usr/bin/rsync` 是 Apple **openrsync**（"rsync 2.6.9 compatible, protocol 29"），GNU rsync 3.x 的旗标不完整。未安装 fswatch / rclone / unison。
- **⚠️ 决定性实测结论**：本机 openrsync 在 `-b/--backup-dir` 开启时会**静默忽略一切 `--delete*` 参数**（exit 0、无警告，甚至连文件更新也可能被抑制）。因此「一条 rsync 命令同时做镜像+版本化+删除」在本机**不可行**，必须采用两阶段协议（§6.1）。同时实测确认：`-b --backup-dir` 正确地把被覆盖文件移入备份目录；dry-run 输出 `*deleting <相对路径>` 格式可稳定解析。
- **NAS**：路由器 USB 硬盘，SMB 地址 `192.168.5.1`，账号 `admin`（密码不记录在本仓库：安装时输入，存于钥匙串与 nsmb.conf），共享名 **`SHARE`**。
- **SMB 协商版本仅 2.002**（廉价路由器栈）：小文件性能一般、无快照、原子重命名语义不保证 —— 首次全量同步预期耗时较长。
- **路由器屏蔽 ICMP**：ping 不通但 SMB 正常。**探活禁止用 ping**，改用 TCP 445 探测 / 挂载表 / 哨兵文件。
- **TCC**：launchd 拉起的进程读 `~/Documents`、`~/Desktop` 等目录需要用户为 `/usr/bin/python3` 授予「完全磁盘访问权限」；macOS 26 首次访问局域网还有「本地网络」隐私授权。安装器和 doctor 必须覆盖这两点。

---

## 3. 方案选型（设计评审结论)

采用「3 独立设计 × 3 评委裁决」流程，三套候选架构：

| 候选方案 | 核心思路 | 可靠性 | 简单性 | 契合度 | 排名 |
|---|---|---|---|---|---|
| **A. 零依赖 launchd + Python 守护进程** ✅ | 系统自带工具链（openrsync + mount_smbfs + python3 stdlib），单文件守护进程，两阶段同步 | 8~8.5 | 7~8.5 | 7~9 | **3 票第一** |
| B. Homebrew rsync3 + fswatch + bash 守护进程 | 引入成熟 GNU rsync 3.x 与 fswatch | 5 | 4~5.5 | 6~7 | 第二/第三 |
| C. Swift FSEvents + 菜单栏 App | 编译型 Swift agent，FSEvents 秒级同步 + 菜单栏 UI | 4.5~5 | 3.5~4 | 6.5~8 | 第二/第三 |

**淘汰理由（均为本机实证，非猜测）**：
- 方案 B：本机 brew 是 Rosetta 安装（依赖全是 x86_64 转译）；bash 3.2 守护进程存在已证实的语法级 bug（`read -t 0.5` 非法）；brew upgrade 会改变二进制 cdhash 静默失效 TCC 授权；FDA 授给 `/bin/bash` 等于授给全机所有脚本。
- 方案 C：其核心删除机制（`--delete-delay` + `--backup-dir`）被实测证明在本机静默失效，文档中的降级方案同样失效——需要重构而非换旗标；~4000 行 Swift + AppKit 是最大实现面，且每次重编译都会重置 FDA 授权。

**最终架构 = 方案 A + 评委嫁接改进**（改进清单见 §6 各节内嵌标注 ⭐）。

---

## 4. 总体架构

```
┌────────────────────────────── macOS (登录会话) ──────────────────────────────┐
│                                                                              │
│  com.mycloud.agent.plist (LaunchAgent, RunAtLoad, KeepAlive={SuccessfulExit:false}) │
│      └─▶ /usr/bin/python3 mycloudd.py   ← 守护进程入口 + mycloud/ 模块包（py3.9 stdlib）│
│            │                                                                 │
│            │  状态机: OFFLINE → MOUNTED_IDLE → SYNCING → (DELETIONS_HELD / DEGRADED) │
│            │                                                                 │
│            ├── 挂载管理器: mount_smbfs → ~/Library/Application Support/MyCloud/mnt │
│            │     四层健康守卫: TCP:445 / mount 表 / 哨兵文件(子进程超时读) / smbutil statshares │
│            ├── 变更检测: Tier1 目录 mtime 热扫描(30s)+kqueue(auto) │ Tier2 全量校验(15min) │
│            ├── 同步引擎: 两阶段协议 (Phase1 openrsync 复制+覆盖版本化 → Phase2 dry-run 解析+Python 归档删除) │
│            ├── 回收站管理: .trash/<UTC时间戳>/ + 保留期修剪 + 容量上限          │
│            ├── 断路器: 大规模删除 HOLD + 通知 + mycloudctl approve-deletions   │
│            └── 观测: 滚动日志 / state.json / NDJSON 流水 / osascript 通知      │
│                                                                              │
│  com.mycloud.watchdog.plist (独立 /bin/sh 看门狗, 每日): state.json>48h 未更新 → 通知 ⭐ │
│  mycloudctl (CLI): status / sync-now / approve-deletions / pause / resume / doctor / logs │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │ SMB 2.002
                                      ▼
┌────────────────────────── NAS //admin@192.168.5.1/SHARE ─────────────────────┐
│  /MyCloud/data/<文件夹名>/     ← 镜像本体（每个配置的源文件夹一个目录）          │
│  /MyCloud/.trash/<UTC时间戳>/  ← 被覆盖的旧版本 + 被删除文件（保留 N 天后修剪）   │
│  /MyCloud/last_sync.json       ← 同步心跳（任何设备可直接查看备份新鲜度）⭐       │
│  /.mycloud_sentinel            ← 安装 UUID 哨兵（挂载身份守卫）                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

**第三方依赖：0 个。** 调用的每个二进制（`/usr/bin/rsync`、`/sbin/mount_smbfs`、`/usr/bin/python3`、`/usr/bin/osascript`、`/usr/bin/security`、`/usr/sbin/diskutil`、`/usr/bin/caffeinate`、`/usr/bin/smbutil`）均随 macOS 26.6 出厂。

---

## 5. 目录结构

**代码仓库**（本项目目录）：

```
MyCloud/
├── PLAN.md                          # 本方案
├── README.md                        # 用户文档（含 FDA/本地网络授权图文步骤、恢复文件指南）
├── src/
│   ├── mycloudd.py                  # 守护进程入口
│   ├── mycloudctl.py                # CLI 入口
│   └── mycloud/                     # 模块包（按职责拆分便于并行开发与测试，安装时整目录复制）
│       ├── paths.py / config.py / util.py / state.py   # T1 地基
│       ├── mount.py                 # T2 挂载管理 + 四层守卫
│       ├── engine.py                # T3/T4 两阶段同步引擎
│       ├── trash.py                 # T5 回收站/保留期/空间水位
│       ├── scanner.py / kq.py       # T6/T7 变更检测
│       ├── notify.py                # T9 日志/通知/TCC
│       └── daemon.py                # T8 主循环状态机
├── install/
│   ├── install.sh                   # 幂等安装器
│   ├── uninstall.sh                 # 卸载器（不碰 NAS 数据）
│   ├── selftest.sh                  # 装后真实 NAS 冒烟测试 ⭐
│   ├── com.mycloud.agent.plist.template
│   └── com.mycloud.watchdog.plist.template   # 独立活性看门狗 ⭐
└── tests/
    ├── test_unit.py                 # 单元测试（unittest）
    ├── test_integration.py          # 集成测试
    └── integration.sh               # hdiutil 假 NAS 测试编排
```

**运行时布局**：`<APP>` = `~/Library/Application Support/MyCloud/`，含 `mycloudd.py`、`config.ini`、`mnt/`（裸目录恒为 chmod **0500**）、`runtime/`（state.json、snapshot.json、daemon.lock、excludes.txt、approval.token、journal.ndjson）；日志在 `~/Library/Logs/MyCloud/`。

---

## 6. 关键设计决策

### 6.1 两阶段同步协议（本方案的核心，由本机实测倒逼）

**Phase 1 —— 复制 + 覆盖版本化**（rsync 执行）：

```
/usr/bin/rsync -rltD -b --backup-dir=<MNT>/MyCloud/.trash/<UTC戳>/<文件夹名> \
  --modify-window=2 --timeout=180 --contimeout=15 --partial \
  --itemize-changes --stats --exclude-from=<RUNTIME>/excludes.txt \
  <SRC>/  <MNT>/MyCloud/data/<文件夹名>/
```

- 用 `-rltD` 而非 `-a`：`-a` 隐含的 perms/owner/group 在 SMB 上无法落实，只会制造逐文件报错和无谓重传；`--modify-window=2` 吸收 exFAT/FAT 的 2 秒时间戳精度。
- **此命令永远不加任何 `--delete*`**（实测会导致整轮静默失效）。
- 被覆盖的旧版本由 rsync 自己经 `--backup-dir` 移入当轮 `.trash/<时间戳>/`（实测有效）。

**Phase 2 —— 删除传播**（rsync 计算，Python 执行）：

1. 跑 dry-run：`/usr/bin/rsync -rltD -n --itemize-changes --delete-during --exclude-from=<同一排除文件> <SRC>/ <DST>/`，解析 `*deleting ` 行（格式已实测）。排除语义与 Phase 1 逐字节一致。
2. 过断路器（§6.4）。
3. 每个待删路径：realpath 前缀校验必须位于镜像根内 → **⭐ 执行 rename 前对源路径再 stat 一次**（关闭「dry-run 后文件又被本地重建」的竞态窗口）→ `os.rename` 移入 `.trash/<同一时间戳>/<文件夹名>/<相对路径>`（同卷 rename，瞬时零拷贝）→ 自底向上清理空目录。
4. 含换行符的文件名无法通过行解析——fail-safe（永不误删，但删除不传播），此类文件在 status 中计数展示 ⭐。

**能力自探针（每次启动强制执行）**：在临时沙盒验证「backup-dir 移动覆盖文件」与「-b 抑制 --delete」两个行为指纹。⭐ **指纹变化时 fail-closed**：立即停用删除传播（Phase 2），仅保留复制，并通知用户——防止未来 macOS 更新悄改 openrsync 语义造成误删。

### 6.2 挂载策略与四层守卫（R5/R6）

- **私有挂载点** `<APP>/mnt`，**绝不使用 /Volumes**（避开 Finder/autofs 的 SHARE-1 重名、抢占弹出等整类问题；用户 Finder 里已有的 `/Volumes/SHARE` 挂载互不影响）。
- 裸目录恒为 **chmod 0500**：共享未挂载时任何写入立刻 EACCES —— 「误写本地磁盘塞满启动盘」在结构上不可能发生（R6 的机制保证，而非约定保证）。
- 挂载命令：`/sbin/mount_smbfs -N -o soft,nobrowse,nodev,nosuid -d 0700 -f 0600 //admin@192.168.5.1/SHARE <MNT>`。`-o soft` 保证会话死亡时 I/O 返回 EIO 而非永久挂起；`-N` 免交互（LaunchAgent 无 TTY）。
- **四层健康守卫**（每轮同步前 + 同步中由 60s 看门狗线程持续检查）：① TCP 445 socket 探测（不用 ping）；② `mount -t smbfs` 挂载表解析；③ 哨兵文件读取——用**子进程 + 10s 超时**执行 `cat <MNT>/.mycloud_sentinel` 并比对安装 UUID（僵死 SMB vnode 只会卡死子进程，卡不死守护进程）；④ ⭐ `smbutil statshares -m <MNT>`（限时子进程）验证 SMB 会话真实存活。
- 守卫失败 → 杀掉 rsync 进程组（SIGTERM→10s→SIGKILL，rsync 以 `start_new_session=True` 拉起）→ 强制卸载 → 校验裸目录为空且复位 0500（若非空 = 检测到本地误写，**告警并停止，绝不删除**）→ 退避重连（30/60/120/300/600s 封顶，⭐ ±20% 抖动）→ 挂载成功后全量校验同步。

### 6.3 变更检测（R2「像 iCloud」的实现）

- **Tier 1 热扫描（30s 周期）**：`os.scandir` 只遍历**目录**、比对目录 `st_mtime_ns` 快照。APFS 在任何直接子项创建/删除/重命名时都会更新父目录 mtime，且 macOS 应用几乎全部采用「写临时文件再 rename」的原子保存——所以新建/修改/删除/重命名都能在 ~30s 内被捕获，成本极低（纯目录元数据，5 万目录 ~1-2s）。5s 防抖合并保存风暴。
- **⭐ 子树限定同步**（评委嫁接，关键延迟优化）：热扫描已算出脏目录集合，将其折叠为互不包含的覆盖子树，**只对脏子树跑两阶段同步**（Phase 2 dry-run 也限定在该子树）——变更到 NAS 的端到端延迟从「全树扫描时长」降到秒级~1 分钟级，而不牺牲正确性（全量校验兜底见下）。
- **⭐ kqueue 近实时模式默认 auto**：启动时统计目录数，fd 预算（4096，plist 已抬 NumberOfFiles=16384）内自动启用 `select.kqueue`/`EVFILT_VNODE` 监听实现秒级检测；超预算自动降级为扫描模式并在 `mycloudctl status` 中明示。
- **Tier 2 全量校验（15 分钟周期）**：无条件对全部文件夹跑两阶段同步，rsync 自身的文件级扫描作为最终裁决——兜住 in-place 写入（SQLite、追加日志等不改父目录 mtime 的变更，最坏 15 分钟延迟，文档明示）。⭐ 上一轮未完成则跳过本 tick，并按实测耗时自适应拉长 full_interval（防路由器慢盘导致背靠背扫描）。
- **立即触发**：守护进程启动、断线重连成功、休眠唤醒（每 5s 调度 tick 比对 monotonic 与 wall-clock 差值检测唤醒）、`mycloudctl sync-now`（SIGUSR1）。
- ⭐ 首次全量同步期间用 `/usr/bin/caffeinate -i` 挂住系统不休眠，并将 dry-run 统计出的总文件/字节数写入 state.json 供 status 显示进度。

### 6.4 删除安全与回收站（R4）

- **断路器**：单轮待删数 > `max_delete_files`(默认 200) 或 > 镜像文件数的 `max_delete_pct`(默认 20%) → 该文件夹进入 `DELETIONS_HELD`：Phase 1 照常（新数据不断流），删除冻结，发通知；仅当用户执行 `mycloudctl approve-deletions`（交互确认，先打印待删清单）后放行。⭐ HELD 状态每日重复通知，且在 status 中醒目展示——防止用户错过一条通知后镜像无限期分叉。
- **源消失守卫**：源目录缺失或为空而镜像非空 → 整个文件夹跳过 + 通知；⭐ 记录每个源的 `st_dev`，变化（外接盘被卸载、挂载点变成空壳）即拒绝同步该文件夹。
- **保留期修剪**：每日一次、仅在整轮成功且四层守卫通过后执行；只删 `.trash` 下匹配 `^\d{4}-\d{2}-\d{2}T\d{6}$` 且超过 `retention_days`(默认 30) 的目录；每个 rmtree 目标做 realpath 前缀校验。**修剪是全系统唯一会在 NAS 上真正销毁数据的代码路径**（测试强制断言此性质）。
- ⭐ **trash 容量上限** `trash_max_gb`：超限时从最旧批次起修剪——防高频改动在小盘上把空间翻倍吃满。
- **空间水位**：`statvfs` 剩余 < 5 GiB 告警、< 1 GiB 停止同步并通知。
- ⭐ **配置生命周期语义**：文件夹从 config 移除或改名 → NAS 上对应镜像**原样保留**并在 status 标记 orphaned，绝不自动删除；由用户手工清理。

### 6.5 凭据管理

安装时先走 **Keychain**（`security add-internet-password -r "smb " ...`）并用与守护进程完全相同的旗标做**验证挂载**；失败则按 mount_smbfs 手册的保底路径回退到 `~/Library/Preferences/nsmb.conf`（chmod 600）写入密码段——回退前**明确告知用户是明文存储并征得同意**。密码永不出现在进程 argv 和日志中。（另：路由器出厂弱口令务必修改，README 有提示；本系统对凭据来源无假设，改密后在 GUI 设置页「云盘连接」重新保存即可。）

### 6.6 权限（TCC / 本地网络）

- **完全磁盘访问**：备份 `~/Documents`、`~/Desktop` 等需要用户在系统设置中为 `/usr/bin/python3` 授权。守护进程每轮对每个源做 `os.scandir` 自检，PermissionError 时：日志给出精确操作步骤、一次性通知、并 `open "x-apple.systempreferences:...Privacy_AllFiles"` 直达设置页；该文件夹本轮跳过、其余照常，授权后自动恢复。README 明示「FDA 授给 python3 即授给全机所有 python 脚本」这一范围放大代价（零依赖免编译的交换项）。⭐ 安装后立即验证 TCC 归因确实落在 python3 上（macOS 26 行为需实证，属 M1 的首要验证项）。
- ⭐ **本地网络权限**（macOS 26）：launchd 进程首次连 445 端口可能被隐私弹窗静默拦截，症状与「NAS 掉线」无法区分——doctor 必查、README 必写。

### 6.7 可观测性（R8）

`RotatingFileHandler`（5×5MB）+ launchd stdout/stderr 日志；`state.json`（状态机、最近成功时间、复制/归档计数、HELD 清单、⭐ 永久失败文件名单——exFAT 非法字符/超 4GB 等 exit-23 常客必须以具名列表暴露，而不是埋在日志里）；⭐ 追加式 `journal.ndjson` 同步流水（时间、文件数、字节、时长、退出类别）；⭐ 每轮成功后把小型 `last_sync.json` 写到 NAS 上——任何设备打开共享即可核实备份新鲜度；通知经 osascript，按失败类别 6 小时限流。
⭐ **独立活性看门狗**：单独的 `/bin/sh` LaunchAgent 每日校验 `state.json` 新鲜度，>48h 未更新即通知——专治「macOS 大版本升级弄死 CLT python3、守护进程根本起不来、主进程内的通知兜底随之失效」这一最阴险的静默失败。

### 6.8 launchd 细节

`KeepAlive = {SuccessfulExit: false}`（⭐ 而非 true）：崩溃（非零退出）自动重启，主动干净退出（pause、卸载、重复实例让位 exit 0）不被 launchd 对抗性拉起；`ThrottleInterval=30` 防热循环；`ProcessType=Background` + `LowPriorityIO` + `Nice=5` 保证不干扰前台；`LimitLoadToSessionType=Aqua`。单实例：launchd label 天然唯一 + `fcntl.flock` 双保险。

### 6.9 预留扩展点

`mycloudctl status --json` 输出每文件夹状态机值（upToDate/syncing/deletionsHeld/needsPermission/sourceMissing/degraded），未来可零改动挂接 SwiftBar 菜单栏展示（v1 不做）。

---

## 7. 配置文件设计（用户唯一需要编辑的文件）

`<APP>/config.ini`，`mycloudctl reload`（SIGUSR2）热生效：

```ini
[server]
host = 192.168.5.1
share = SHARE
username = admin
; 密码不在此文件：Keychain 或 nsmb.conf（安装器处理）

[backup]
; 每行一个要备份的本机文件夹（支持 ~）
folders =
    ~/Documents
    ~/Desktop
; rsync 排除模式，每行一个（内置默认：.DS_Store .Trashes .TemporaryItems *.tmp ~$* node_modules/ .git/）
exclude =
    *.photoslibrary
retention_days = 30
trash_max_gb = 50
hot_interval = 30
full_interval = 900
max_delete_files = 200
max_delete_pct = 20
offline_notify_hours = 24
watch_mode = auto        ; auto | kqueue | scan
```

---

## 8. 编码任务拆解（派发给 Opus 4.8）

派发方式：每个任务一个 Opus 4.8 子代理（Agent 工具，`model: "opus"`），按批次串并结合；每任务附本方案相关章节 + 环境事实 + 验收标准。**通用约束**：Python 3.9 stdlib only、零第三方依赖、所有状态文件原子写（tempfile+os.replace）、所有外部子进程必须带超时、密码不进 argv/日志。

**批次 1 —— 骨架（串行，其余任务的地基）**
| 任务 | 内容 | 验收 |
|---|---|---|
| T1 | 项目脚手架 + config 模块 + 原子写工具 + 路径常量；文件夹校验（存在性、expanduser/realpath、拒绝父子嵌套、重名后缀） | 单元测试通过；非法配置给出人话报错 |

**批次 2 —— 核心模块（T1 后可并行开发）**
| 任务 | 内容 | 验收 |
|---|---|---|
| T2 | 挂载管理器 + 四层守卫 + force_unmount + 裸目录 0500 复位/非空即停 | 挂载表解析过 fixture 测试；哨兵读走子进程限时 |
| T3 | Phase 1 引擎（§6.1 精确旗标；进程组拉起；23/24 部分失败逐文件记录并进 named list；stats 入 state） | 本地目录树往返正确；**命令中出现任何 --delete\* 即测试失败** |
| T4 | Phase 2 引擎（dry-run 解析、断路器、rename 前二次 stat、realpath 校验、空目录清理） | 含空格/中文文件名测试；镜像外路径 rename 被拒 |
| T5 | 回收站修剪 + trash 容量上限 + 空间水位 | 时间戳解析/截止计算单测；断言 rmtree 仅出现在修剪路径 |
| T6 | Tier1 热扫描 + 快照 + 防抖 + ⭐ 脏子树折叠限定同步 | 5 万目录扫描 <3s；子树折叠正确性单测 |
| T7 | kqueue auto 模式（fd 预算、降级、卸载时清理 fd） | 超预算自动降级且 status 可见 |

**批次 3 —— 集成（依赖批次 2）**
| 任务 | 内容 | 验收 |
|---|---|---|
| T8 | 主循环/状态机/调度 tick/唤醒检测/看门狗线程/退避+抖动/信号处理/flock/能力自探针（指纹变化 fail-closed 停删除） | 拔盘模拟：rsync 被杀、裸目录空且 0500、自动恢复 |
| T9 | 日志/通知（6h 限流）/TCC 自检+设置深链/持续失败升级通知/journal.ndjson/NAS 心跳 last_sync.json | 各通知类别限流单测 |
| T10 | mycloudctl 全命令（status[--json]/sync-now/approve-deletions 交互确认/pause/resume/reload/logs/doctor 含本地网络权限检查） | doctor 在断网/未授权/未挂载三种场景输出正确诊断 |

**批次 4 —— 交付（依赖 T8-T10）**
| 任务 | 内容 | 验收 |
|---|---|---|
| T11 | plist 模板 ×2（agent + ⭐ 看门狗）+ install.sh（幂等、拒绝 root、Keychain→nsmb.conf 带验证挂载与知情同意、哨兵写入、bootstrap+kickstart、FDA 指引） | 全新安装/重复安装均幂等成功 |
| T12 | uninstall.sh（bootout、卸载挂载、清本机文件、可选清凭据；**醒目声明 NAS 数据分毫未动**） | 卸载后 NAS 端 MyCloud/ 完好 |
| T13 | 测试体系：单测 + hdiutil 假 NAS 集成测试（含 openrsync quirk 回归测试、断路器、拔盘、修剪唯一销毁路径断言）+ ⭐ selftest.sh 真实 NAS 冒烟（**中文文件名往返、rename 进新目录树、哨兵、心跳**） | `python3 -m unittest discover` 全绿；selftest 对真实 NAS 全绿 |
| T14 | README.md：安装步骤（FDA/本地网络授权截图级指引）、恢复文件指南、**明示定位**——这是单副本便利镜像而非 3-2-1 归档备份（NAS 盘损毁即失去唯一备份副本；修剪会永久销毁超期历史） | 按文档从零安装可成功 |

---

## 9. 里程碑

| 里程碑 | 内容 | 出口标准 |
|---|---|---|
| M1 核心镜像 | T1-T4 + T8 精简版（先验证 TCC 归因与 mount_smbfs -N 两个最大不确定点） | 登录自动挂载 + 全量镜像 + 15 分钟级增量；拔盘不写本地盘 |
| M2 近实时 | T6、T7（脏子树 + kqueue） | 普通文件变更 ≤60s（scan）/≤15s（kqueue）到达 NAS |
| M3 健壮性 | T5、T9、T8 完整版 + 看门狗 | §10 全部故障场景通过 |
| M4 交付 | T10-T14 | 从零安装→selftest 全绿→卸载留数据 |

## 10. 验收测试场景（逐条演练）

1. 安装后重启 Mac：无人工干预自动挂载 + 全量同步，Finder 中不出现多余卷。
2. 新建/修改/重命名文件：按 M2 时限出现在 NAS；被覆盖旧版进 `.trash/<时间戳>/`。
3. 中文/空格/长路径文件名在真实 NAS 上往返一致（NFC/NFD 归一化验证——对中文用户是必测项）。
4. 同步进行中拔掉路由器电源：rsync 进程组被杀，挂载点保持空 0500，本地磁盘零写入；来电后自动重挂 + 全量校验。
5. 开机时 NAS 离线：静默退避，24h 后才通知一次；恢复即自动同步。
6. 合盖过夜：唤醒后自动校验 + 同步，无报错风暴。
7. `rm -rf` 一个源文件夹：断路器 HOLD + 通知；approve 后全部进 trash 而非直接消失；HELD 期间每日重复提醒。
8. 源在外接盘且外接盘未挂载：该文件夹跳过 + 告警（st_dev 守卫），镜像不动。
9. in-place 写入（SQLite）：≤ full_interval 到达 NAS。
10. >4GB 文件（若 NAS 为 exFAT）：进具名失败清单并在 status 展示，不静默丢失。
11. NAS 剩余 <1GiB：停止同步 + 通知；修剪后自动恢复。
12. 守护进程被杀/崩溃：launchd 拉起，状态从磁盘无损重建；state.json >48h 未更新时独立看门狗通知。
13. uninstall 后：NAS 上 `MyCloud/` 完好无损。

## 11. 主要风险与对策

| 风险 | 对策 |
|---|---|
| macOS 更新改变 openrsync 语义 | 启动能力自探针 + 指纹变化 fail-closed（停删除、保复制、通知）+ 回归测试 |
| macOS 大版本升级弄死 CLT python3 → 备份静默停止 | 独立 /bin/sh 看门狗（48h 无心跳即通知）+ README 说明重装 CLT |
| `mount_smbfs -N` + Keychain 对该路由器未验证 | 安装时真实验证挂载，失败走 nsmb.conf 保底（知情同意）；属 M1 首验项 |
| TCC 归因不落在 python3 上（macOS 26 未实证） | M1 首验项；失败则改用包装可执行文件方案再评估 |
| 路由器 SMB 2.002 慢、首次全量耗时长 | caffeinate 保活 + 进度展示 + 子树限定同步降低日常扫描量 |
| FAT 时区/DST 偏移触发一次性全量重传 | --modify-window=2；检测「全库统一 1h 差」特征仅记日志不告警 |
| 单盘单副本非真备份 | README/安装输出明示定位；建议用户对 NAS 盘另行冷备 |

## 12. 阶段二：MyCloud.app 图形界面（2026-07-19 新增需求）

**需求**：Mac 原生应用，支持①选择要同步的文件或目录、②选择同步目标目录、③展示同步进度、④展示同步问题。

**架构定位**：GUI 是已验证引擎之上的**薄壳**——所有数据安全逻辑（两阶段同步、断路器、守卫）留在守护进程；GUI 只读状态、只经 CLI 契约写配置。GUI 自身**不需要**完全磁盘访问权限（文件选择走 NSOpenPanel 的用户显式授权；读 `~/Library` 下的 state.json 无需 TCC）。

**技术选型**（已实测）：SwiftUI，CLT 自带 swiftc 直编（swift 6.1 / MacOSX SDK 已验证 BUILD_OK），零第三方依赖，ad-hoc 签名，构建脚本手工组 .app bundle。不用 Xcode 工程、不用 SPM。

### 12.1 守护进程侧新能力

1. **单文件同步源**：`folders` 键接受文件路径；`FolderSpec` 增加 `is_file`；文件用 `-ltD`（无 `-r`）的 Phase 1 变体同步到其镜像目录；本地删除该文件 → 经 Phase 2 归档进回收站（断路器照常计数）。校验：已配置目录内的文件拒绝（被目录覆盖）、包含已配置文件的目录拒绝其一。
2. **可配置目标基目录** `[backup] dest_rel`（默认 `MyCloud`，相对共享根）：布局为 `<dest_rel>/data/<名称>/`、`<dest_rel>/.trash/`、`<dest_rel>/last_sync.json`。校验：相对路径、无 `..`、非空、非 `.`。变更 dest_rel 时：旧位置数据**原样保留**（标记 orphaned，绝不移动/删除），通知用户。
3. **实时进度**：engine phase1 增加 `on_progress(files_done, current_relpath)` 回调（按 itemize 传输行触发）；daemon 节流（≥2s）写入 state.json：`progress = {active, folder, files_done, total_files(可空), current, started_at}`；轮结束清除。初始同步的 total 来自预跑 dry-run 统计。

### 12.2 GUI ↔ 后端 JSON 契约（mycloudctl 新增/扩展子命令，全部 JSON 出）

| 命令 | 行为 |
|---|---|
| `status --json` | 已有；补充 `watcher_degraded`、`progress` 透传 |
| `config-get --json` | 输出完整配置：server 三元组、dest_rel、sources `[{path,is_file,name}]`、excludes、全部数值项、warnings |
| `config-add-source <path>` | 校验后追加；输出更新后配置 JSON；若守护进程在运行则 SIGUSR2 |
| `config-remove-source <path>` | 移除；提示 NAS 镜像将保留为 orphaned；SIGUSR2 |
| `config-set-dest <rel>` | 校验 + 写入 + 旧位置 orphaned 提示；SIGUSR2 |
| `config-set <key> <value>` | 数值项/watch_mode 白名单校验写入；SIGUSR2 |
| `approve-deletions --yes` | 非交互放行（GUI 弹确认框后调用） |
| `doctor --json` | `[{id,name,ok,detail,hint}]` |
| `mount` | 未挂载则按配置挂载（供 GUI 浏览目标目录），输出挂载点 |

### 12.3 应用结构（中文界面）

- **主窗口** NavigationSplitView 四区：**概览**（状态徽章、上次成功时间、进行中进度条、最近同步流水、立即同步/暂停/恢复按钮）、**同步项**（源列表增删——NSOpenPanel 可选文件或目录；目标目录选择——未挂载先经 `mount` 再浏览挂载点算相对路径，亦可手输；变更前风险提示）、**问题**（HELD 删除清单 + 批准按钮带确认框、永久失败文件具名清单、orphaned 列表、能力指纹告警、watcher 降级、一键运行诊断表格）、**设置**（保留期/容量上限/阈值/扫描间隔/watch_mode、守护进程安装状态引导）。
- **菜单栏** MenuBarExtra：状态图标 + 快捷操作（立即同步/打开主窗口）。
- 数据流：1s 轮询读 state.json + journal 尾部；写操作全部经 12.2 契约调用 `/usr/bin/python3 <APP>/mycloudctl.py`（开发态经环境变量指向仓库 src）。
- 未安装守护进程时显示引导页（指向 install.sh 与 README）。

### 12.4 任务拆解（Opus 4.8）

| 任务 | 内容 | 依赖 |
|---|---|---|
| G1 | config.py：文件源 + dest_rel + 校验/迁移；paths 派生函数；单测 | — |
| G2 | engine 文件源变体 + 进度回调；daemon dest_rel 布局/文件源监测/进度节流写入；scanner/kq 文件父目录监听；单测 | G1 |
| G3 | mycloudctl 全部新子命令（12.2 契约）+ 单测 | G1 |
| G4 | Swift：Bridge（Process 调 ctl + JSON Codable 模型 + state 轮询）、App 骨架、build_app.sh（swiftc → dist/MyCloud.app + ad-hoc 签名）、Info.plist | G3 |
| G5 | Swift：四区视图 + MenuBarExtra + NSOpenPanel 流程（中文文案） | G4 |
| G6 | 验证收口：Python 全量回归（含不变量）；构建 .app；启动冒烟（进程存活 + 基本 UI 存在）；契约一致性测试 | 全部 |

**验收**：全量测试绿；`bash gui/build_app.sh` 产出可启动的 dist/MyCloud.app；在 GUI 中添加/移除源和改目标目录后 `config-get` 与 config.ini 一致且守护进程收到 SIGUSR2；进度字段在同步中每 ≤5s 刷新；HELD 场景在问题页可见且可批准。

## 14. 阶段三：同步引擎 v2 —— 本地清单数据库（2026-07-19 深夜，用户要求对标 iCloud/群晖）

**问题**：v1 每轮全量校验都让 rsync 重新遍历本地 31.6 万文件并对 NAS 镜像逐文件远程 stat——廉价路由器 SMB 上这是十几分钟到小时级的固定开销，且被中断即全部重来。成熟产品（Synology Drive、Dropbox、iCloud、Time Machine）从不这样做：它们的共同架构是**本地状态数据库 + 事件驱动 + 只传增量 + 罕见的深度校验**。

**v2 核心机制**（NAS 镜像是单写者——只有本守护进程写它——因此本地清单可以完全代替远程 stat）：

1. **清单数据库** `RUNTIME/manifest.db`（stdlib sqlite3，WAL 模式）：`files(folder, relpath, size, mtime_ns, inode, synced_at, pending)` + `meta(schema_version, last_deep_verify_at)`。31.6 万行对 SQLite 是小菜。
2. **增量管线**（每轮）：本地快速遍历（纯本地元数据，31.6 万文件约数秒）→ 与清单 diff → 得到 {新增/修改集, 删除集, 重命名集, 待下载 iCloud 集} → **零远程 I/O 的变更计算**。
3. **批量传输**：上传集按 ≤500 文件 / ≤2GB 一批，经 rsync `--files-from` 分批执行；**每批成功后提交清单** → 精确进度百分比、中断只损失一批、重启原地续传（治好"扫描 15 分钟被打断全重来"）。
4. **删除**：由清单 diff 计算（不再远程 dry-run 全树），执行仍走现有 Phase-2 安全机器（断路器/回收站/realpath 校验，安全不变量零改动）。
5. **重命名识别**（对标群晖/Dropbox）：删除+新增中 inode/size/mtime 三元组吻合者判定为重命名 → NAS 侧直接 `os.rename`（镜像根内、realpath 校验），免去整棵子树重传+回收站拷贝；存疑即回退为上传+删除。
6. **深度校验降频**：每周一次 + `mycloudctl verify` / GUI 按钮手动触发。⚠️ 机制修正（2026-07-20 实测）：openrsync 的 `-n` dry-run 会 mmap 源文件并**触发 iCloud 物化下载**，故深度校验不采用 rsync 源侧遍历，改为**清单 vs NAS 挂载点的 os.walk 对账**（只读 NAS 侧，绝不触碰本地源）；缺失/漂移→标记 pending 重传，NAS 多余→走既有 Phase-2 断路器/回收站通道。
7. **iCloud「下载后备份」管线**（用户已选）：待下载集分批执行 **下载(读触发) → 备份该批 → 校验入清单 → `brctl evict` 释放**（只 evict 我们下载前就是 dataless 的文件，绝不动用户自己实体化的文件）——45GB 全程只占用批大小级别的本地磁盘（默认 `icloud_evict_after_backup = true`，设置可关）。⚠️ 实测（2026-07-20）：macOS 26.6 的 brctl 无 evict/download 子命令 → 自动释放降级为一次性日志提示 + 状态标记 `icloud_evict_available=false`，下载的文件将留存本地（用户可在 Finder 手动「移除下载项」）；空间守卫 ≥3× 批体积仍生效。
8. 首次引导：清单空 → 全部入 pending → 批量管线自然完成初备（可断点续传）；现有 NAS 镜像内容极少（Desktop 24MB），直接重传，不做迁移逻辑。

**不变**：挂载四层守卫、0500 裸目录、两阶段删除安全、断路器、回收站保留、能力自探针全部保留。变更检测仍是 30s 目录 mtime 扫描 + kqueue（预算内自动启用）。

## 15. 阶段四：传输性能改造决策（2026-07-21，基于市场研究 + 本机实测）

**背景**：首轮全量实测单流 3 文件/秒、2.8MB/s——瓶颈为每文件 SMB 往返固定成本（~0.3s/文件，与大小无关），31.6 万文件中 70% 为小文件，属最坏工况。

**研究输入**（两份代理报告，来源含 Dropbox/Microsoft/Apple 工程博客、restic/borg/Kopia/Arq 设计文档、rclone 文档与论坛）：
1. 并行文件流是行业统一解法：robocopy /MT 默认 8 路、OneDrive 客户端 8 线程、rclone 小文件建议 --transfers 32；微软官方口径 2-4×。**本机实测（上传并发进行时）：1/2/4 路 = 5.2/9.3/17.9 文件/秒，近线性**。
2. 每文件往返可瘦身：时间戳 SET_INFO 一次、temp+rename 的 open/rename/close 一轮均可省（真值已在本地清单）；SMB 签名在弱 CPU 服务器上是显著开销（微软/TrueNAS 数据），macOS 客户端可请求关闭；AppleDouble ._ 文件会使小文件操作翻倍（需核查排除）。
3. 凡以哑存储为目标的成熟工具（Time Machine 稀疏束 band、restic 16MiB pack、Kopia 20-40MB pack、Arq ≤256KB 进 pack、git packfile）全部选择**小文件打包**——千倍级操作数削减；纯镜像的快只存在于有服务端代理的方案（群晖 Drive，路由器无法运行，已排除）。
4. 路由器仅有 SMB 通道（本机端口探测：无 FTP/NFS/WebDAV）——换协议路线排除；用户态 SMB2 compounding 客户端违反零依赖约束，排除。

**决策**：
- **P0（立即实施）**：①并行上传 worker 池，`upload_concurrency` 默认 4（部署时实测 6/8 找拐点封顶）；manifest 提交加锁、进度聚合、watchdog 兼容进程组击杀。②每文件操作瘦身：--inplace（省改名往返；崩溃安全由清单批次重传+深度校验兜底）与时间戳策略（去 -t 或 --ignore-times，以微基准择优）作为配置项默认开启；核查并排除 ._ AppleDouble。③部署窗口顺带做 nsmb.conf 关签名实验（statshares 验证 + 微基准，服务器强制则自动无效）。
- **P1（首备完成后，可选功能，默认关，需用户在 GUI 明确开启）**：混合打包模式——冷·海量小文件子树（node_modules/.git/objects/venv/缓存/数据集；名单+阈值启发式，UI 逐目录可控）打包为 **zip64 分卷**（32-64MiB、哈希命名、临时名原子发布、pack 尾自带索引、epoch 粒度整组替换清理），镜像树放 marker 说明文件；zip 格式保住"任何设备可打开"的弱浏览性；恢复经 App 对用户透明。深度校验对 pack 走顺序读重哈希（快几个量级）。
- **明确不做**：全打包存储（牺牲可浏览性这一产品差异点）；用户态 SMB2 客户端（依赖与工程量）；内容分块去重（规模不值）；双端代理架构（硬件不可行）。

### 15.1 P0 实施记录（2026-07-21 完成，517 测试全绿）

**决定性实测（本机 openrsync）**：`--inplace` 单独工作正常（真原地写，inode 不变）；但与 `-b --backup-dir` **组合是破坏性的**——目标文件消失（utimensat ENOENT，rc=1）、新内容错进 backup 目录、旧版本彻底丢失。二者绝不可共存。

由此对 P0-② 设计修正如下：

1. **--inplace 按批自动路由**（非全局开关）：清单天然区分「新文件」（无 manifest 行 → NAS 无镜像条目，`Plan.new_set`）与「更新文件」。新文件批走 `--inplace`（无旧版可保护，零安全损失——首备 31.6 万文件全是此类，收益最大化）；更新文件批保持 `-b` 覆盖版本化（验收场景 2 语义不变）。三重防线：运行时 assert（`_assert_inplace_backup_exclusive`）+ AST 静态不变量（test_invariants）+ 能力探针新指纹 `inplace_updates_ok`（失败则 fail-closed 回退全 `-b`，指纹逐键比较，升级新增探针不再误触发删除冻结）。
2. **时间戳策略取「去 -t」**（`rsync_no_times = true` 默认）：省每文件一次 SET_INFO 往返；NAS mtime 语义变为「备份时间」，真值在本地清单。深度校验联动：no_times 模式只比 size（本地修改仍由清单的本地 mtime quick-check 全量捕获）。**--ignore-times 放弃**：对 --files-from 批次无往返收益（目标 lstat 为 -b/新建判定所必需，省不掉；SET_INFO 由 -t 决定与它无关），且批次文件本就是必传集，quick-check 结果无关紧要。配置注释警告：no_times 用过之后再关回去，下次深度校验会把全部已传文件判 drift 重传一轮（无害但慢）。
3. **P0-① 并行池**：`upload_concurrency = 4` 默认（1–16，config-set 白名单已加）；ThreadPoolExecutor 滑动窗口分发（批生成器 lstat 在主线程，远快于 rsync 批）；Manifest 本就 RLock 串行化，逐批提交/断点续传语义不变；进度聚合 = 已入账批次 + 在飞 itemize 行数（单调不减）；watchdog `_kill_sync_procs` 改两阶段（全部 SIGTERM → 共享宽限窗 → 余者 SIGKILL），避免 N 个 worker 串行等待 N×10s。**并发范围实测限定**：并发 rsync 对**数据树**的 mkdir 竞态可容忍（5 轮 × 8 路压测干净），但对 `-b` 的 **backup 树** mkdir 竞态**不容忍**（`mk_backup_dir: File exists` + 传输中断，实测 2026-07-21）——因此**只有新文件（inplace）批走并行池，更新文件（-b）批严格串行**。增量日常变更批通常 ≤1 批，串行零损失；并行收益全部落在首备（全新文件）场景，正是 P0 的主目标。推论：inplace 被关闭/探针失败时上传整体退化为串行（正确性优先，等同 v2 现状性能）。
4. **顺手修复的既有 bug**：BUILTIN_EXCLUDES 只进了 excludes.txt（engine --exclude-from 路径），而 v2 清单管线（plan_folder/scanner）只收 `cfg.excludes` 用户排除——**内置排除在 v2 上传路径完全失效**（.DS_Store/node_modules/.git 会被备份）。修复为 `Config.effective_excludes`（builtin+user）统一供给所有 walk；内置排除新增 `._*`（AppleDouble sidecar，P0-② 核查项）；深度校验的 NAS walk 同样应用排除——Finder 浏览共享产生的 .DS_Store/._* 不再被当 extra 每周反复归档进回收站。
5. 单文件源（phase1_file）与全树 phase1 不参与 inplace/no_times（它们依赖 rsync 自身 quick-check 的 mtime 增量语义），保持 `-rltD -b` 不变。
6. GUI：config-get 已透出三个新键（Swift Codable 忽略未知键，无兼容问题）；设置页界面项留待 P1 与打包开关一并做。

### 15.2 真机部署实测与修复（2026-07-28，装机后连续运行数日的产出）

**并发拐点实测（PLAN 15 P0 遗留的待验项,答案与预期相反）**:

| upload_concurrency | 有效吞吐 | 稳定性 |
|---|---|---|
| 4（原默认） | **0.7 文件/秒** | 反复掉线:rsync 撞自身 180s I/O 超时（rc=20 `poll: timeout`）→ 判失败 → 击杀/重挂/整批重传 |
| **2（新默认）** | **4.8 文件/秒** | 19 分钟零掉线,全程 transferring |

结论:并发的收益在这台廉价路由器上**到 2 就见顶**,4 路把 SMB 服务端压过临界点后,恢复循环吃掉的时间远超并行收益——**名义并行 4 路比单路还慢 7 倍**。默认值已改为 2,template 记录了判据(日志出现 `poll: timeout` 就降)。

**修掉的四个真 bug**(均为真机才暴露,全部有回归测试):

1. **`_mirror_count` 用 os.walk 数 NAS 镜像** → 每 folder 每轮在 SMB 上遍历整棵镜像树。真机上 `data/Documents/` 有 7.1 万个历史文件,遍历一次 10+ 分钟,期间不写进度,表现为"卡在 planning"(反复误判为死锁)。改为读 **manifest 行数**(单写者 ⇒ 清单即镜像真相,一次 SQL COUNT);仅断路器路径在清单为空时允许一次**上限 2 万文件**的回退遍历。展示路径零远程 I/O。**启动到开始传输:11 分钟 → 2.5 分钟**。附带更安全:os.walk 会把 Finder 的 `.DS_Store`/`._*` 垃圾计入分母,虚高后反而放宽大规模删除阈值。
2. **`_estimate_initial` 双倍扫描** → 初始 pass 前对每个源做完整 walk(iCloud 29 万文件 10+ 分钟)、不写进度,紧接着 full_pass 又扫同一棵树,只为一个展示用总量。清单非空时**跳过**;真首次运行时**期间写进度**。
3. **watchdog 启动过晚** → 线程只在 `_main_loop` 启动,而最长最危险的**初始 pass 在它之前**,全程无保护。提前到初始 pass 前启动(`_start_watchdog`,幂等)。
4. **传输停滞死局** → 挂载守卫可以全绿（哨兵可读、statshares 存活）而 rsync 子进程卡在内核不可中断 I/O(实测 state `UN`、CPU 0%、14 分钟零字节),此时所有基于守卫的规则每分钟把失败计数归零,**无人干预**,主线程在 `_upload_relpaths` 永久等待。新增 `_TRANSFER_STALL_KILL = 1200s`:有活 rsync 且上传零进展超阈值 → 击杀进程组 + 触发 recovery(未提交批次不进清单,`--partial` 保留已传字节)。阈值刻意保守,因为单个巨大文件只输出一行 itemize。

**可观测性/可用性修复**:
- **`SIGINFO` 栈转储**(`faulthandler.register`):`kill -INFO <pid>` 即得所有线程的 **Python** 栈。上面第 1、4 项都是靠它一次定位的——此前只能用 `sample` 看 C 帧,反复猜错。**以后遇到"卡住"先发这个信号**。
- **进度可见性**:`files_done` 因 rsync 管道块缓冲呈跳跃式前进(可冻结几十秒),用户据此误判暂停。新增 `last_transfer_at`(最近一次传输的 wall-clock)+ `batches_done/total`;GUI 进度条改按**字节**(31 万文件大小跨数个量级,按文件数的百分比严重失真),并显示"最近传输 X 秒前"(超 3 分钟变橙)。
- **`sync-now` 说真话**:SIGUSR1 只在两轮之间被读取,长 pass 期间点它毫无反应,而命令却回"已发送信号"。现在会报"已在同步中"+ 字节百分比/批次/最近传输,并说明"本轮结束后才会再跑一轮"。
- **doctor/status 汇总排除已移除的源**:`state.folders` 保留每个曾同步过的 folder 且计数冻结在移除时刻,导致 iCloud 云端文件数报 **316275**(真实 **969**)。汇总只算当前配置中的源;孤儿仍单独列出并标 orphaned。
- **通知中文乱码**:`json.dumps` 的 `ensure_ascii` 把中文转成 AppleScript 不认的 `\uXXXX`,每条中文通知都 syntax error。改用 `osascript ... on run argv` 传参。
- **`build_app.sh` 在 iCloud Drive 下签名失败**:仓库位于 iCloud Drive,复制进 bundle 的文件带 iCloud/Finder xattr,`codesign` 直接拒绝("resource fork ... not allowed")。组装后签名前加 `xattr -cr`。
- **GUI 编译前提**:新版 SwiftUI 的 `@State` 是宏,需要 `libSwiftUIMacros.dylib`,**只随完整 Xcode 分发**,纯 CLT 编不过。需装 Xcode + `sudo xcode-select -s` + `sudo xcodebuild -license accept`。

**运行环境注意**:
- launchd 的**钥匙串访问不可靠**:稳定运行一天后 `mount_smbfs -N` 突然 rc=77(终端同命令正常),`nsmb.conf` 明文回退是必备保险,实测它自动救回(`mounted via embedded-credential fallback`)。
- **同一 share 勿挂两次**:用户在 Finder 挂 `/Volumes/SHARE` 与守护进程的私有挂载点并存,会加重廉价路由器 SMB 负担;已卸掉 Finder 那份。
- **fileproviderd 让 iCloud 树的本地扫描极慢**:29 万文件每轮 planning 约 5–10 分钟属正常,不是卡死;判断"卡住"前先看 manifest 是否分批跳、或直接发 SIGINFO。

**部署待办（下次上线窗口）**：真实 NAS 上按 4/6/8 实测 upload_concurrency 拐点后封顶；顺带做 nsmb.conf 关签名实验（statshares 验证 + 微基准，服务器强制则自动无效）。

## 13. 待用户确认事项（不阻塞开发，默认值如下）

1. **备份哪些文件夹？** 默认 `~/Documents` + `~/Desktop`（配置文件随时可改）。
2. **NAS 端存放位置**：默认共享根 `SHARE/MyCloud/`；也可放入现有 `SHARE/Backup/MyCloud/`。
3. **回收站保留期**：默认 30 天，上限 50 GB。
4. **大规模删除审批阈值**：默认 >200 个文件或 >20% 即冻结待确认。
5. 是否需要菜单栏状态图标（v1 预留接口未实现，可作 v2）。


## 16. 阶段五：数据可信度与稳健性（2026-07-30，Fable 5 复审 → Opus 实施）

**复审输入**：真机连续运行 5 天的全部日志/状态 + 两次抽样审计（1,200 尺寸比对 + 20 个 SHA256 逐字节比对）。

**审计事实**：NAS 上已存在的文件与本地**逐字节一致**（0 差异）；但清单（manifest）记录为"已备份"的文件中约 **1.3%（推算 ~4,200 个）在 NAS 上并不存在**——含真实用户文件（照片/工作文档），不只开发垃圾。**清单在说谎，这是当前系统最大的问题。**

### 16.1 根因清单（按危害排序，全部已在源码定位）

1. **部分失败批次的提交依赖 stderr 解析**（`engine._parse_failed_paths` 尽力而为、路径形态不归一）：rc=23/24 时未被解析出的失败文件被**当成功提交进清单**。rc=24（源文件传输中消失）同样把从未上过 NAS 的文件记为已同步。→ 清单说谎的直接来源。
2. **本地 walk 的目录级 OSError 被静默吞掉**（`manifest._walk_scope` 四处 `except OSError: continue/return`）：fileproviderd 抖动时一个 scandir 失败 = 整棵子树"消失"→ 被计划为**删除**。断路器挡住了 ≥阈值 的（本次 1000 HELD 即此），但 **<200 个的会被直接执行**（进回收站，可恢复但错误）。
3. **深度校验（修复工具本身）不健壮**：主线程对 NAS 的 os.walk 无 `_stop`/守卫检查（挂载死亡 = 僵死或吐出残缺结果，残缺结果仍会 `mark_pending` → 伪重传风暴）；`_verifying` 期间进度心跳不刷新（GUI 误报"无响应"）。
4. **瞬时 rc=20**（rsync 自身 180s I/O 超时,弱路由器偶发）**直接升级 SyncFailure → 全量恢复流程**，对偶发抖动过重。
5. **开发垃圾（.venv/__pycache__/*.pyc 等）约占备份量 1/3** 且是失败高发源；但直接加排除会让既有清单行变"vanished→删除"，**必须配套清单 forget 迁移**。
6. **967 个仍在云端的文件在 skip 策略下永不备份**，launchd 下载限制已实证且已持久化——缺一个**用户上下文**的物化工具闭环（用户终端可以下载，实测多次验证）。

### 16.2 P0 实施项（本轮，Opus 编码，A→B→C 串行）

- **T-A1 验证式提交**：partial（rc 23/24）批次不再信任 stderr 解析——对批内每文件在 NAS 上 `os.lstat` + size 比对，**仅提交验证通过者**；未通过者自然留待下轮重传；同一文件连续 ≥3 次验证失败 → 进 `failed_files` 具名披露（每 pass 记一次）。stderr 解析降级为日志参考。验证过程任何 OSError = 不通过（fail-safe）。
- **T-A2 rc=20 单次重试**：批次 rc=20 时原地重试一次（短暂等待后），仍失败才升级 SyncFailure。信号死亡（rc<0，watchdog 击杀）不重试。
- **T-B1 删除抑制守卫**：`plan_folder` 统计 `walk_errors`（scandir/lstat OSError 计数，进 Plan）；>0 时该文件夹本轮**抑制删除与重命名传播**（两者都由 vanished 派生），上传照常，log+journal 记录。
- **T-B2 内置排除扩充 + 清单迁移**：`BUILTIN_EXCLUDES` += `__pycache__`、`*.pyc`、`.venv`、`.pytest_cache`、`.mypy_cache`、`.tox`、`.ruff_cache`（保守：不加 `venv`/`target` 等易误伤的通用名）。守护进程启动与 reload 时调用 `manifest.forget_excluded(folder, patterns)`（按路径组件 fnmatch，语义与 walk 一致）删除匹配行——否则既有被跟踪的垃圾会整体变"删除"。NAS 侧既有垃圾**原样保留**为孤儿 litter（本地文件存在 ⇒ 深度校验不会将其判为 extra；显式清理命令留 P2）。
- **T-C1 深度校验健壮化**：NAS walk 每 ~256 目录或 ≥5s 检查一次 `_stop` 与 `mount.is_mounted()`，异常即整体中止且**不产生任何 mark_pending/extra 动作**；中止时不更新 `last_deep_verify_at`（周期照常重试）；`_verifying` 纳入进度心跳条件；verify 摘要增加 `aborted` 字段。
- **T-C2 `mycloudctl materialize`**：用户上下文批量物化云端文件——扫描配置的目录源（lstat-only）收集 SF_DATALESS 文件，磁盘水位守卫（可用 ≥2× 待下载体积），逐文件限时物化（复用 icloud 模块的子进程读触发），实时进度输出；物化后守护进程按既有节奏自动纳入备份。闭环 launchd 限制。

### 16.3 验收与修复顺序

1. A→B→C 串行实施（无 git、同树），每步全量 `unittest` 绿（基线 556）。
2. 部署重启后触发一次 deep verify 全量对账。预期终态：~4.2k missing → pending → 自动重传补齐；1000 HELD 在下一轮完整 pass 自动清零（文件均在本地，已核实）；`mycloudctl materialize` 后云端 967 归零。
3. doctor 仅应剩已知系统限制行。

### 16.4 P2（暂缓，须用户另行决定）

`clean-excluded` 显式清理 NAS 上被排除的垃圾（动 NAS 数据，必须用户确认）；GUI 设置页暴露性能旋钮与 failed_files 明细；doctor 措辞打磨（skip 策略下 prefetch-blocked 降为信息行）；daemon.py（~3,700 行）模块化拆分。


## 17. 阶段六：常开 Mac 备份网关部署方案（2026-08-01，Fable 5 方案）

**用户目标**：MyCloud 迁至一台常开 Mac；其他电脑/手机往 iCloud 写入 → iCloud 自动同步到常开 Mac → 守护进程随时镜像到 NAS。本机（笔记本）不再承担备份职责。

### 17.1 两个成败关键（均为本项目真机实证）

1. **必须关闭「优化 Mac 储存空间」**（系统设置 → Apple ID → iCloud → iCloud 云盘）。已实证：launchd 后台进程**无法**触发 iCloud 下载（fileproviderd 拒绝，每文件卡满 120s），守护进程对 dataless 占位文件无能为力。关闭优化存储后由系统自己保持全量本地化（新文件自动物化），守护进程只管镜像——这是整个网关架构的前提。磁盘要求：iCloud 全量（当前 ~70GB）+ 增长余量，建议 ≥150GB 空闲。漏网 dataless 的兜底：`mycloudctl materialize`（必须用户终端跑，launchd 上下文无效）+ doctor 的 dataless 计数监控。
2. **单写者铁律**：同一 `dest_rel` 同时只允许一台机器的守护进程写。v2 清单架构假设"NAS 镜像只有本进程写"；两台并行时,A 机新传的文件在 B 机清单中无行,B 的深度校验会把它判为 extra → 归档进回收站——**互相拆台**。切换必须是迁移（旧机 uninstall 或永久 pause），不是共存。

### 17.2 清单迁移（省 ~68GB 首轮重传）

新机全新 manifest 会把 26.7 万文件全部视为"新文件"重传。优化：**把旧机 `runtime/manifest.db` 拷到新机同路径**（安装后、守护进程首启前）。行数据中 size+mtime_ns 在 iCloud 跨设备同步中保留 → quick-check 命中,绝大多数文件不重传；inode 为本机值、跨机失效 → 仅 rename 检测退化为"上传+删除"（可接受,新机首轮后自愈）。注意:拷贝前旧机守护进程必须已停（WAL 一致性,连同 -wal/-shm 一起拷或先干净关闭）。

### 17.3 新机部署清单（浓缩本项目全部踩坑）

1. 系统:仅装 **CLT**(`xcode-select --install`),**不要**把 xcode-select 指向 Xcode（守护进程 Python 真身会被换掉,§15.2/记忆#12 事故）;GUI 无需编译,直接拷 `dist/MyCloud.app`（新机首次运行右键打开,或 `codesign -s - --force` 重签——codesign 属 CLT）。
2. iCloud:同一 Apple ID,iCloud Drive 开启,**关闭优化存储**,等首次全量下载完成再装 MyCloud（`brctl status` / Finder 观察）。
3. `bash install/install.sh`（交互输密码）;装完**必写 nsmb.conf 常驻回退**（launchd 钥匙串跨重启不可靠,实证 rc=77 事故——见 17.4 安装器改进）。
4. FDA:系统设置 → 完全磁盘访问 → `+` → `/usr/bin/python3`。「本地网络」面板无需也无法操作（代码已经 SMB 代理绕过）。
5. config.ini:源按需（网关机通常只配 iCloud 根;若新机也开「桌面与文稿」iCloud 同步,注意 ~/Desktop、~/Documents 与 iCloud 树的重叠,避免双份）;`upload_concurrency=2`（路由器实测拐点）。
6. 拷贝 manifest.db（17.2）→ 启动 → `mycloudctl doctor` 全绿 → 首轮增量 pass → `mycloudctl verify` 对账确认。
7. 常开设置:系统设置 → 能源 → 防止自动睡眠 / caffeinate 常驻不需要（首轮有 caffeinate,常态靠系统设置）;建议接市电、合盖运行需外接显示器或 `pmset` 调整。
8. 旧机退役:`bash install/uninstall.sh`（不碰 NAS 数据）。

### 17.4 部署前的安装器改进（本轮可做）

- install.sh:验证挂载成功后**默认建议**写 nsmb.conf 常驻回退（保留知情同意文案）——现行为仅在 -N 失败时才写,导致"装时正常、数日后钥匙串失效停摆"的实证事故。
- 打包 `deploy/` 说明或一键脚本:上述清单的可执行版。

### 17.4b 目标机为 macOS 12（Monterey）的兼容性核查（2026-08-01）

后端全兼容（无 py3.9-only 特性、launchd/mount_smbfs/smbutil/FDA/SF_DATALESS/SIGINFO 均支持，推荐 CLT 14.2=python 3.9.6）；macOS 12 无「本地网络」TCC（15+ 才有），直连探测畅通；`/usr/bin/rsync` 为 samba 2.6.9 真身而非 openrsync——能力探针首启自适应记录指纹、写模式永不带 --delete，两实现均安全，openrsync-quirk 回归测试在该机可能红（预期）；**GUI 不可用**（MenuBarExtra/NavigationSplitView 需 13+），网关机 headless 运行，跨设备看 NAS 心跳 last_sync.json。详见 deploy/DEPLOY-GATEWAY.md 首节。

### 17.4c 网关部署实战修正（2026-08-01，网关机/macOS 12 首台实测）

1. **doctor 能力指纹误报(已修)**:旧检查要求全部探针 True,真 rsync 2.6.9 的 `delete_suppressed_with_backup: false` 被误判 FAIL。改为:关键行为(`backup_moves_overwritten` + `deleting_line_format_ok`)必须 True;已存指纹**逐键比对**(与守护进程同语义,新增探针键不算变化);`delete_suppressed_with_backup=False` 仅附说明不判 FAIL(写模式静态不变量保证从不携带删除旗标)。测试 +4(611 绿)。
2. **清单导出的 checkpoint 竞态(已写入指南)**:`pause` 返回时守护进程可能仍在收尾,WAL 未清空就 `cp` 会拷出缺尾事务的 .db(实测少 283 行)。导出前必须确认 `manifest.db-wal` 为 0 字节。
3. **macOS 12 无 .ini 关联应用**:`open config.ini` 报 kLSApplicationNotFoundErr,指南改用 `open -e`。
4. 实测印证审查结论:TCP 445 直连可达(无「本地网络」TCC);守护进程首启正确记录 2.6.9 指纹,未误触发 fail-closed。
5. watchdog.sh 复核:纯通知(state.json 超 48h 提醒),从不重启 agent——旧机长期 pause 不会被它拉起,单写者安全;副作用是暂停 48h 后每天 10:00 弹一次提醒,退役卸载即消。
6. **resume 误报失败(已修)**:agent 是 RunAtLoad,`bootstrap` 本身就启动守护进程,旧 resume 紧接的 `kickstart -k` 会把刚启动的守护进程**杀掉重启**;网关机首启慢(迁移来的 161MB 清单),杀+重启超过 launchctl 30s 预算 → TimeoutExpired → 报"启动失败"而守护进程实际健康(doctor 显示 state.json 7s 前刷新)。修复:bootstrap 后先轮询 daemon.lock flock(`_wait_daemon_alive`,10s×0.5s),活了即成功不再 kickstart;兜底 kickstart 不带 `-k`(没有可杀的对象);任何 kickstart 超时/非零都先探活再定性;doctor 的"重启守护进程"修复动作同样处理。测试 +3(614 绿)。

7. **哨兵层 TCC 实锤(2026-08-01 21:07,观测性补丁首战)**:新机守护进程 80 分钟卡 OFFLINE 的根因由新日志行直接定案——`mount guard failing at layer 'sentinel' (cat rc=1: cat: …/.mycloud_sentinel: Operation not permitted)`。同一文件用户终端 cat 正常 → launchd 上下文的 python3(及其子进程)缺 TCC 授权,**网络卷(SMB 挂载)的文件读取也在 TCC 管辖**,FDA 覆盖之;修复=系统偏好设置给 `/usr/bin/python3` 授 FDA 后重启守护进程。**已实测闭环(2026-08-02)**:授权+重启后哨兵层立即通过,守护进程进入首轮 pass(SYNCING),假设-修复-验证链完整。macOS 12 无「前往文件夹」快捷键不熟时,`open -R /usr/bin/python3` 弹出 Finder 选中后拖入面板最省事(已入指南)。**重要教训:doctor 全绿 ≠ 守护进程正常——doctor 跑在终端的 TCC 上下文里,天然比 launchd 乐观**。遗留改进(P2):doctor 增加 launchd 一次性任务探针,在守护进程的真实上下文里读哨兵+源目录。
8. **GUI macOS 12 移植(gui-macos12 分支,2026-08-01)**:见 §18。

### 18. GUI macOS 12 移植（gui-macos12 分支）

仓库自 2026-08-01 起为 git 仓库(此前无版本控制;基线 commit = 部署日全量,619 测试绿;**git 操作只在开发机做**,iCloud 同步 .git 的双机并发提交会出冲突副本)。分支 `gui-macos12`:

- **构建**:deployment target 13.0→12.0,双架构 universal(arm64+x86_64,防网关机是 Intel),LSMinimumSystemVersion=12.0。
- **13+ API 全部替换**(编译器在 12.0 target 下枚举,零 error 收敛):
  - `MenuBarExtra` → AppKit `NSStatusItem` + `NSPopover` + `NSHostingController`(StatusItem.swift,12→26 单一代码路径,不留版本分叉);图标随 AppState 变化仅在符号变化时刷新。
  - `@Environment(\.openWindow)`/`WindowGroup(id:)` → `openMainWindow()`:窗口查找 + `NSWorkspace.open(bundleURL)` reopen 兜底(公开 API,全版本一致)。
  - `.defaultSize` → ContentView `.frame(minWidth: 720, minHeight: 520)`。
  - `NavigationSplitView` → `#available` 分叉,12 走双列 `NavigationView`(唯一保留的分叉,视觉等价)。
  - List 非 Optional selection(13+)→ Optional selection(10.15+)+ nil 兜底概览。
  - `.formStyle(.grouped)` → `groupedFormCompat()`(13+ 保持 grouped,12 默认样式);`LabeledContent` → 自绘 `LabeledRow`(单路径)。
- AppState 改由 AppDelegate 持有(@NSApplicationDelegateAdaptor),窗口与状态栏共享同一实例。
- 冒烟:macOS 26 实机通过;**macOS 12 实机待 网关机 验证**(dist/ 经 iCloud 自动分发,右键→打开绕过 Gatekeeper)。
- 教训:对含 shim 的文件做全文字符串替换会把 shim 自身实现也替换掉(`self.formStyle(.grouped)` 被换成自调用 → 13+ 无限递归),编译不报错、运行必炸;已修并留此记录。

### 17.6 迁移收官（2026-08-02,全链路完成）

网关迁移正式完成,时间线:FDA 授权解除 TCC 卡点 → 首轮增量 pass(清单命中,零重传)→ 深度校验第一轮 `missing_uploaded=61 / drift=0 / extra=0 / errors=[]`(41 分钟/26.7 万行;61 个 NAS 缺失行当场补传成功——高度吻合旧机那批 openrsync 永远传不动的悬空符号链接,真 rsync 2.6.9 投递 symlink 无碍,新机终态**严格优于**旧机终态)→ 第二轮校验全过(用户确认 all check passed)→ 旧机 `uninstall.sh` 退役(agents/plists/挂载/Application Support 全清,config+logs 备份于 `MyCloud.bak-20260802`;钥匙串与 nsmb.conf 按默认保留,属用户通用 NAS 凭据)→ 迁移临时清单 manifest-migrate.db 已从 iCloud 删除。**当前唯一写者 = 网关机 网关**。遗留给用户的常开化三件套:确认「优化 Mac 储存空间」关闭、防睡眠、自动登录;GUI 实机 14 项清单(gui/DEVLOG.md)可择期过。

### 17.5 架构语义与残余风险（如实告知）

- 备份新鲜度受 iCloud 同步延迟支配（分钟级正常）;NAS 端 last_sync.json 心跳可跨设备核查。
- 其他设备的删除会经 iCloud 传导至 NAS（回收站 30 天可反悔）;大规模误删由断路器拦截（>200 HOLD）。
- macOS 大版本升级可能重开「优化存储」/换 CLT 指纹——doctor + 48h 看门狗覆盖;升级后跑一次 doctor。
- 「本地网络」「launchd iCloud 下载」两个系统限制在新机同样存在,代码均已绕过/降级,无需人工干预。

### 18.1 云盘连接契约:`mycloudctl server-set`（2026-09-12,经监督审查两轮）

GUI 设置页「云盘连接」的后端。语义(顺序即守卫):①校验输入与候选配置(零副作用);②守护进程在跑且(换目标 或 提供了新密码)→ bootout、等卸载与 daemon.lock 释放(45s,超时中止且不动清单/配置)、释放私有挂载点(旧共享留着会让新守护进程 rc=64 永久 OFFLINE);③临时挂载点(realpath)用**候选凭据**实测——有密码走嵌入 URL 不走 -N(钥匙串旧密码/会话复用会"代考"),并要求 `is_mounted()` 能匹配挂载表行;哨兵读错误≠不存在(TCC 提示),外来哨兵须显式 `--adopt`(GUI 二次确认),新哨兵以 `x` 模式写入;④验证通过后才写钥匙串 + nsmb.conf(600),匿名模式删 `[host]`/`[host:*]`;目标或共享身份变了就把 manifest.db 转存 .bak;⑤finally 必 resume,再等 60s 看 state.json 是否上线——**GUI 上下文能连通 ≠ launchd 守护进程能连通**,回执明说。密码全链路只走 stdin。挂载表匹配改为逐字段(guest 挂载显示为 `//GUEST:@…`)。审查抓出并修掉 5 个 P0(详见 gui/DEVLOG.md 迭代 3b);测试 651。

### 19. 重构总纲:对标成熟同步产品（2026-09-12 启动）

> 用户指令:参考群晖 Drive / iCloud 的同步模式重构,提升稳定性与可用性。本节是路线图与执行协议;每阶段按 §18 建立的监督循环(审查代理 + 四门自测 + DEVLOG)推进。

#### 19.1 现状诊断（2026-09-12 量测）

- 体量:`daemon.py` **3,996 行**、`mycloudctl.py` 2,647 行、engine 829、manifest 569、config 423、mount 390、kq 366;GUI 6 文件约 3k 行。
- `Daemon` 单类 ~116 个方法:传输/进度 797 行、守卫/挂载/看门狗 458、生命周期编排 381、深度校验 227、规划/清单 219、iCloud 物化 107、另有 1,226 行未能按名字归类——**编排、策略、I/O 三层耦合在一个类里**,这是每次修 bug 都要通读的根因。
- 变更检测:每轮对 29 万文件全量 lstat 走盘(分钟级),kqueue 只做"有动静"触发,不产出增量;fileproviderd 抖动靠 walk_errors 兜底。
- 状态:`state.json` 字符串状态 + `manifest.db`;事件是半结构化 `journal.ndjson`;GUI 靠 1s 轮询文件;ctl 靠子进程 JSON 契约。
- 必须保留的强项:写模式永不 `--delete` 的静态不变量、验证式提交、删除断路器、哨兵守卫、SIGINFO 诊断、密码不进 argv、单写者、四门自测。

#### 19.2 对标:可借鉴的成熟模式

| # | 模式 | 群晖 Drive / iCloud 的做法 | MyCloud 现状 | 借鉴方式 |
|---|---|---|---|---|
| 1 | 事件驱动增量 + 变更日志 | iCloud:变更令牌/FileProvider 枚举;群晖:客户端 DB + 文件系统事件 | 每轮全量走盘 | FSEvents 事件流 → 本地"脏集合"增量规划;全量走盘退化为每日校正 |
| 2 | 任务抽象 | 每个同步任务独立配置/暂停/状态/错误 | 多源共享全局状态 | 每源 `SyncTask`:独立状态机、暂停、节流、错误 |
| 3 | 显式状态机 | syncing / paused / error / up-to-date 有限状态 + 转移规则 | 自由字符串 | 枚举状态 + 允许转移表 + 转移日志;GUI 直接映射 |
| 4 | 传输策略 | 限速、时段计划、断点续传 | 并发可配、无限速/时段 | 限速 + 静默时段(工作时限速、夜间全速);批级续传已有 |
| 5 | 冲突与版本 | iCloud 冲突副本;群晖版本控制 | 单向备份 + 回收站版本化 | 保持单向;GUI 增加回收站/版本浏览与恢复 |
| 6 | 健康中心与自愈 | 客户端诊断、静默重试退避 | doctor(终端上下文)+ 守卫 | doctor 增 launchd 上下文探针;错误分类(瞬时/配置/权限/远端)+ 分类退避与可执行提示 |
| 7 | 活动流与通知 | 活动日志、状态栏聚合 | journal.ndjson + 系统通知 | 版本化事件 schema(JSON lines)+ GUI 活动页(可筛选) |
| 8 | 连接与凭据 | 一处连接设置、凭据保险箱 | 本轮已加「云盘连接」(匿名/注册用户) | 评估用户会话辅助进程代持钥匙串,摆脱 nsmb.conf 明文回退 |
| 9 | 可观测性 | 结构化日志 + 指标 | 半结构化日志 | 统一事件 schema + 指标(吞吐、失败率、每轮时长)供 GUI 图表 |
| 10 | 分层架构 | core / engine / transport / storage / ui | daemon.py 巨石 | 分层拆分(阶段 A) |

#### 19.3 路线图（每阶段独立可交付;顺序按"先稳后快"）

- **阶段 A(P0,基础,零行为变更)— daemon.py 分层拆分**:`daemon.py` 只留编排;拆出 `sync/planner.py`(规划/清单)、`sync/transfer.py`(批传输、并发、验证式提交、停滞击杀)、`guard/mount_guard.py`(守卫 + 看门狗 + 上线/离线)、`icloud/materialize.py`、`verify/deep_verify.py`、`core/lifecycle.py`(信号、pass 调度、reload)。验收:**635 测试不改断言全绿**、`integration.sh` 通过、ctl/GUI 契约(state.json 字段、JSON)零变化;一个阶段一个提交,可整体回滚。
- **阶段 B(P0,可用性)— 显式状态机 + 结构化事件**:`DaemonState`/`FolderState` 枚举与转移表,所有 state.json 写入经状态机;事件 JSON lines(schema v1)替代 journal.ndjson;GUI 活动页消费。验收:非法转移在测试中被拒;GUI 状态文案由枚举驱动。
- **阶段 C(P1,性能/稳定)— 事件驱动增量**:FSEvents 变更集合 → 增量规划,全量走盘降为日校正;walk_errors 保护迁入增量层。目标:常态 pass 从"分钟级走盘"到"秒级"。验收:注入变更后 ≤N 秒进入传输;每日校正与全量结果一致。
- **阶段 D(P1,可用性)— 任务化 + 传输策略**:每源 SyncTask 独立暂停/状态/错误;限速与静默时段;GUI 同步项每项可暂停。
- **阶段 E(P2,运维)— 健康中心**:doctor 的 launchd 上下文探针(终结"doctor 全绿 ≠ 守护进程正常");错误分类退避;GUI 问题页按类别给可执行动作。
- **阶段 F(P2)— 回收站/版本浏览与恢复 GUI;凭据代持方案评估。**

#### 19.4 执行协议（不变量）

- 每阶段一个分支 `refactor/<phase>`,审查代理 + 四门自测 + DEVLOG;合并前在网关机灰度(部署前备份 Application Support,git tag 回滚点)。
- 静态不变量不动:写模式永不 `--delete`;密码不进 argv;单写者;哨兵守卫。
- 阶段 A 不改任何对外契约;之后每个契约变更同时改 GUI 解码并加测试。
- 任何"看似卡死"先 `kill -INFO` 取栈,再动代码。

#### 19.5 首个动作
阶段 A 从 `Daemon` 方法归类表开始(上表数据即基线),先切最独立的两块:守卫/看门狗(458 行)与深度校验(227 行),验证拆分手法与测试兼容性,再拆传输与规划。

### 20. GUI 全流程:安装、连接、授权、卸载全部在应用内完成（2026-09-12）

> 用户要求:"所有的操作都应该在 GUI 上全部完成"——引导页不能再把人推回终端跑 install.sh。

- **应用自带后端**:`build_app.sh` 把 `src/mycloudd.py`、`src/mycloudctl.py`、`src/mycloud/`(去 `__pycache__`)与 `install/` 的 4 个模板打进 `MyCloud.app/Contents/Resources/backend/`。MyCloud.app 成为唯一分发单元:拖进「应用程序」→ 打开 → 向导。
- **`mycloudctl install --from <backend>`**(install.sh 的 Python 移植,幂等):预检 rsync → 停旧守护进程(不在运行中的进程脚下换 .py)→ 布局 `<APP>`(mnt 0500)→ 复制后端 → 首装渲染 config.ini(服务器身份 + 指定 folders;重装保留 [backup])→ **复用 server-set 契约**做凭据/验证挂载/哨兵(同样的守卫、同样的接管确认)→ 渲染 watchdog.sh 与两个 plist、CLI shim → bootstrap 看门狗 + `resume` 守护进程 → 等 45s 看 state.json 是否上线;未上线则回执 `fda_hint`。
- **`mycloudctl uninstall --yes`**(uninstall.sh 移植):bootout 两 agent → 等停 → 释放挂载点 → **硬闸门**:挂载仍在就拒绝 rm -rf(绝不波及 NAS)→ 删 plist/CLI 链接 → 备份 config+logs 到 `<APP>.bak-日期` → 删 `<APP>`;钥匙串/nsmb 默认保留。
- **守护进程把守卫失败机制写进 state.json**(`guard: {layer, detail, soft_pass}`,上线即清):GUI 由此把"OFFLINE"变成可执行指引——`Operation not permitted` ⇒ 缺「完全磁盘访问」。
- **引导页 = 安装向导**(单页三步):①环境(`xcode-select -p`;缺则一键弹系统的 CLT 安装对话框)②云盘连接(与设置页同一套字段,密码只走 stdin)③备份内容(默认 iCloud 根,可增删)→「安装并启动」→ 接管确认弹窗复用 → 成功后主界面出现。
- **FDA 引导横幅**(概览页;守卫被 TCC 拒绝或安装后未上线时出现):「打开完全磁盘访问设置」(`x-apple.systempreferences:…Privacy_AllFiles`)、「在访达中显示 python3」(选中供拖拽,等价于 `open -R`)、「重启守护进程」。macOS 不允许程序代授 TCC,这是 GUI 能做到的极限,并如实说明。
- **设置 → 守护进程 → 卸载**(二次确认;说明 NAS 数据不动)。
- 不变量沿用:密码全链路 stdin;写模式永不 `--delete`;单写者(接管须确认);NAS 数据永不被卸载触碰。测试 651→658+;版本号 1.1.0。

### 21. 全量代码审查:"守护进程时常失败导致无法同步"(2026-09-25/26,v1.3.0)

> 用户指令:review 每一个代码,找出所有 bug 并修复;已知问题是守护进程时常失败导致无法同步。四路审查(生命周期/挂载与 rsync 引擎/ctl 与安装/GUI)+ 同步管线自审,逐条源码核对、能实验的都实验,修复后逐条加回归测试。提交 6758dfa → 2049653(9 个)。

#### 21.1 "无法同步"的根因(按影响排序,全部已修)

| # | 根因 | 表现 | 修复 |
|---|---|---|---|
| 1 | rsync 客户端按自身超时退出,fork 出的子进程卡在 SMB 不可中断 I/O 里仍握着 stderr;同步线程死等,看门狗用 `getpgid` 杀不到、用 `poll` 判不出停滞 | 一次掉线后整个守护进程停摆 | 进程组以 pgid=pid 击杀/探活,流读取有界等待并标记 wedged,`--progress` 刷新计入活性 |
| 2 | 2.6.9(网关真 rsync)退出码语义与 openrsync 相反(30=超时,20=SIGTERM) | 被看门狗杀的批次被当成超时重试,撞死挂载 | 按实现分类 `kind`,只重试真正的 I/O 超时 |
| 3 | `mount()` 非幂等、EEXIST 不收敛、rc77 回退路径 EEXIST 永久 OFFLINE | "File exists" 后永远离线 | 以挂载表为准的三态判断、EEXIST 分辨自家残留、回退路径同样收敛 |
| 4 | 对已死挂载启动整轮 pass;主线程直接对 SMB 做 mkdir/statvfs | 在半死会话上不可中断卡死 | pass 前置守卫(子进程计时)、表驱动裸目录检查、限时 mkdir |
| 5 | LaunchAgent `Background`+`LowPriorityIO` 把 I/O(含 SMB)压到最低档 | 慢到超时 | `ProcessType Standard`、去掉 LowPriorityIO,加 `MaterializeDatalessFiles` |
| 6 | 网关笔记本空闲睡眠 | pass 中途会话死 | 每轮 pass/校验持 `caffeinate -i -w`;doctor 新增电源行 |
| 7 | 在线 iCloud 下载阶段整批 500 个文件超时后才判定"被系统限制"(120s/文件、4 并发 ≈ 4 小时),且只看停止信号 | 旧机实证:174 分钟无响应,看门狗每分钟喊"挂载没了"无人理会 | 4 文件试探即判定;下载阶段对挂载丢失同样停止;离线预下载被放弃时 worker 同步停止;"被限制"锁存 24 小时过期重探 |
| 8 | 看门狗的恢复请求只有 rsync 阶段(靠失败)能感知;`recovery()` 不消费触发它的请求 | pass 在死挂载上继续做下载/重命名/删除/NAS 校验;每次恢复后下一拍再无故卸载新挂载重来一遍 | 文件夹/批次/阶段边界检查并结束 pass;部分批次的 NAS 校验遇慢 lstat 即停;recovery 入口清请求 |
| 9 | 超过 20 分钟的单文件被当成停滞杀掉、从零重来 | 大文件永远传不完 | `--progress` 活性;GUI 的"最近传输"随之刷新 |
| 10 | 文件名以 `#`/`;` 开头时 `--files-from` 静默丢弃 | 个别文件永不备份 | 条目统一加 `./` 前缀 |
| 11 | 热扫描只容忍三类 errno;一个目录的 EIO/EDEADLK 让整次快照作废 | 热路径长期失明(只剩 15 分钟全量) | 任何 OSError 按子树记录后继续 |
| 12 | StateStore 并发写丢更新;配置错误只进 launchd.err.log | 状态错乱;崩溃循环时 GUI 显示旧状态 | RLock;配置错误写进 state.json 的 `last_error` |

#### 21.2 数据安全(已修)

- 回收站容量上限可能**在同一轮结束时删掉刚产生的批次**(刚删除文件的唯一副本)→ 24 小时内的批次不受容量规则删除,超限记 `cap_unmet`,由 NAS 空间闸门兜底。
- 卸载:fd 级 `remove_local_tree` + 挂载闸门 + `rm -rfx`,任何路径都不可能清空 NAS 共享。
- 深度校验:`os.walk` 静默跳过不可读目录 → 其中文件被判"缺失"引发重传风暴;现在任一遍历错误作废该文件夹结论(真正缺失的镜像根仍会重传)。
- 部署指南的清单迁移会把新机空库的 `-wal` 回放到迁移库上写坏它 → `sqlite3 .backup` 导出、删 `-wal/-shm`、改名换入、`integrity_check`。

#### 21.3 GUI(让用户看得见"没在同步")

守护进程死掉时菜单栏仍是绿勾(默认 32 分钟、full_interval=86400 时约 48 小时)→ 读 state.json 里的 `daemon_pid` 探活,约 10 秒内显示「守护进程无响应」;按真实写入节奏定预算(有进度 30s / 安静的同步与启动 600s / 空闲 300s,暂停永不超时);传输 25 分钟无进展显示「同步停滞」;`stopped` 但 launchd 仍加载时显示「守护进程已退出」+ 退出码;冻结删除/失败文件时「空闲」改为「需要处理」。Bridge 以两管道 EOF 为完成条件(原实现约 1‰ 丢掉成功调用的全部输出)、超时 SIGTERM 后给 45 秒收尾;「确认接管」按钮原本因弹窗关闭先清空请求而**从未生效**;首次安装失败不再把用户困在读不到配置的主界面;busy 计数化;配置失败静默重试;版本只把更旧的后端判为过旧。

#### 21.4 验证与现状

- Python **786** 测试全绿;新增 `tests/test_pipeline_review.py` 等,核心修复做了变异检验(回退即失败)。
- GUI:本机 Xcode 27 许可未同意,完整 `gui/selftest.sh` 与 `build_app.sh` 暂不能跑;改用 CLT 验证(`gui/tools/clt_check.sh`,Xcode 不可用时的替代门):双架构类型检查 0 错、符号门、渲染冒烟、新增 `gui/tests/LogicTests.swift` 87 项(已并入 selftest 第 4 门)。2026-09-28 用户同意 Xcode 许可后:完整 `gui/selftest.sh` 五门全绿(真 Xcode 工具链,双架构编译+链接,0 警告),`build_app.sh` 打包 1.3.0(universal、minos 12.0、ad-hoc 签名干净副本校验通过、包内后端与仓库一致、启动/退出正常)。注意:许可要用 `sudo /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild -license`(全路径),**不要**把 xcode-select 切到 Xcode。
- 待办:网关机替换 App 并「更新后端」(或在网关仓库目录 `bash install/install.sh --refresh`)→ 观察 `mycloud.log` / `launchd.err.log`;网关按 DEPLOY §8 设置防睡眠与自动登录。

### 22. 网关两个月未上传的真因与 1.3.1(2026-10-02/03)

> 用户在网关上跑了诊断命令(`pmset -g custom`、`pmset -g log`、mycloud.log 等),据此查清"总是同步失败、经常掉线"。

#### 22.1 真因(网关实测确认)

- 网关后端仍是 **1.2.0**。≤1.2.0 的每个写入 rsync 都带 `--contimeout=15`;macOS 12 的 `/usr/bin/rsync` 是 Apple rsync-55(samba 2.6.9)**没有这个选项**(rsync 3.0 才加入)→ `rsync: --contimeout=15: unknown option`,exit 1,在传任何文件之前就失败。用户在网关上执行 `/usr/bin/rsync --contimeout=15 --version` 得到同样的报错,rc=1,确认无误;审查代理也用 Apple 源码编译出 rsync-55 复现了网关日志里的原句。
- 每次失败都走 `recovery()`:卸载 → 「离线」→ 重挂 → 立刻再规划一轮 → 再失败。用户看到的"掉线"就是这个循环,一天几十次。
- **自 2026-08-02 网关接管起,基本没有上传过新文件或改动过的文件。**8 月的"迁移成功"是误读:`verify` 打印的 "missing re-uploaded: 61" 只是标记为待传的行数;"all checks passed" 是 doctor 横幅;doctor 和启动能力探针都用各自的 argv,不带这个选项,所以一直是绿的。开发机是 openrsync,接受 `--contimeout`,所以 786 个测试都在开发机上跑,永远抓不到。1.3.0 已经删掉了这个选项(当时只当无用清理),部署 1.3.0 后,网关在 10-02 恢复正常备份。
- 其他实测:合盖、接电时并没有真睡,而是停在 DarkWake(CPU 和网络都开着),守护进程照常运行;阻止真睡眠的是某个只在接电时生效的断言,持有者未知。只有 10-02 拔电后真睡了 4 小时。醒来后的挂载错误 `0xC0000225` 实际是**路由器拒绝了到 SHARE 的 TREE CONNECT**(登录本身成功,错误码是从 DFS 回退里泄漏出来的),客户端修不了。macOS 12 的 mount_smbfs 根本不读钥匙串,`-N` 的 rc=77 是本地合成的噪音,真正生效的认证是 nsmb.conf 里的凭据。

#### 22.2 1.3.1 改动

| # | 改动 | 防的是什么 |
|---|---|---|
| 1 | 写入 rsync 的参数白名单(`engine._PORTABLE_LONG/_SHORT`,只放 2.6.9 和 openrsync 都有的选项),每个写入命令构造器都 assert;测试覆盖全部构造器 | 再出现"开发机能用、网关不认"的参数 |
| 2 | `SyncEngine.batch_selftest`:守护进程启动时和 doctor 新增的 `rsync_upload` 行,都用**真实上传参数**在本机临时目录试传;失败时写入 `last_error` | 探针和 doctor 绿着,而真实上传全失败 |
| 3 | 失败分类新增 `usage`(按 stderr 文本判断,不看 rc);`SyncFailure.kind` + `_pass_failed`:只有传输类失败(被杀、io_timeout、看门狗请求)或守卫失败才 recovery;其他失败保留挂载、写 `last_error`、计划中的同步按 5 分钟 → 1 小时退避;一轮成功即清除 | 卸载重挂循环和无休止的重新规划;界面看不到原因 |
| 4 | io_timeout 重试 `-b` 批次时换用新的回收站时间戳目录(`_retry_backup_dir`) | 重试用新副本覆盖回收站里刚保存的旧版本(2.6.9 与 openrsync 均已复现) |
| 5 | 只剩已披露的长期失败文件(连续 3 次失败、已列入 failed_files)时,本轮仍算成功 | 一个 NAS 永远写不进的文件,冻结「上次成功同步」、每天弹 48 小时告警 |
| 6 | macOS ≤13 的 `.X.icloud` 存根:清单遍历时把 X 记为仅在云端(dataless),存根本身不备份;深度校验判断"多余"时也认存根(`_source_has`) | 被移出本地的文件被当成已删除:NAS 副本进回收站,30 天后清除 |
| 7 | mount_smbfs 错误按 stderr 分类(eexist/auth/refused/other);"路由器拒绝共享"给出明确原因(含 NTSTATUS),不再走 EEXIST 处理;`-N` 被拒、nsmb.conf 凭据成功之后,本进程后续挂载直接用凭据,`-N` 降为 INFO | 误导性的报错和日志噪音 |
| 8 | 唤醒时记录日志并立即写状态,守卫最多重试 4 次、每次间隔 15 秒,让网络稳定下来再决定是否 recovery;GUI 唤醒后给 120 秒宽限 | 唤醒时误报「无响应」;对其实很快就好的挂载做卸载重挂和全量规划 |
| 9 | verify 文案改为 "queued for re-upload",GUI 改为「缺失…已排队重传」;部署指南的云端文件计数也数 `.icloud` 存根 | 8 月的误读 |

验证:Python 817 个测试 + GUI 五门全绿(真实 Xcode 工具链);核心改动做了变异检验。

#### 22.3 对 1.3.1 的对抗式审查与修复(2026-10-03)

24 个审查代理核对后确认 16 项、驳回 4 项。全部修复,每项都有回归测试,并逐项做了变异检验(把修复撤掉,必须有测试失败):

| # | 问题 | 修复 |
|---|---|---|
| 1 | 不走 recovery 的失败(usage、守卫正常时的 error)让状态停在 SYNCING、文件夹停在「同步中」:界面显示同步正常,而每次上传都在失败 | `full_pass` 没跑完时,在 finally 里把仍为 syncing 的文件夹改成 degraded,状态写回 MOUNTED_IDLE;GUI 中 `last_error` 非空即显示「需要处理」,菜单栏显示感叹号 |
| 2 | `mkdir -p` 超时(共享卡死)归为 error:守卫正常时不重挂,只退避 | 新增失败类型 `dest_timeout`,归入传输类;io_timeout 重试后不论因何再失败(usage 除外),都按传输类处理;看门狗杀掉的进程不论退出码,一律按传输类(killed)处理 |
| 3 | 没有任何上传的局部同步也会清除错误和退避:错误提示一闪而过,退避永远升不上去,「上次成功同步」照常刷新 | 只有能证明故障已消除的同步才清除:全量同步,或真正成功上传了至少一批的同步;usage 错误只能由一次真实上传清除。局部同步没有上传时,不刷新「上次成功同步」 |
| 4 | 只要有文件夹被暂停(删除待确认)、缺失或无权限,退避和错误就永远不清,后续偶发失败直接退避 1 小时 | 清除不再要求每个文件夹都正常:同步跑完、没有抛出 SyncFailure 即可;只清除 `_note_pass_failure` 自己写的那条消息,别人的消息原样保留 |
| 5 | 退避用 monotonic 时钟,睡眠时间不计入:一夜合盖后,唤醒那次同步被跳过 | 检测到唤醒时,把退避截止时间减去睡眠时长 |
| 6 | `_last_full_duration` 把 recovery 的离线等待也算进去:断网 6 小时后,12 小时内都不跑安全网全量同步 | 只计 `full_pass` 本身的耗时;失败路由放到计时之后 |
| 7 | ≤1.3.0 把 `.X.icloud` 存根当普通文件备份并记了清单行;1.3.1 不再记录存根,这些行每轮都被规划为删除、又因存根还在而跳过,永远删不掉;超过阈值就每轮暂停,批准也只放行一轮 | 遍历时记下被识别为存根的路径,其旧行只从清单删除(`Plan.forget`),不动 NAS;深度校验仍认存根,文件重新下载后再按多余文件处理 |
| 8 | 被移出本地的包(.rtfd/.key/.pages,整个目录变成一个存根):包内文件被当作已删除,进回收站,30 天后清除 | 存根目标之下的清单行视为仅在云端;phase2 删除前和深度校验都检查祖先目录有没有存根 |
| 9 | 单文件来源被移出本地后,NAS 副本被归档进回收站 | `_sync_file`/`_phase2_file` 识别存根:不归档、不算已清除;NAS 副本大小与存根记录的真实大小一致时不报「未备份」;download 策略下用 brctl 下载 |
| 10 | 在 Finder 里移动/改名已移出本地的文件:inode 变了,无法配对为改名,旧路径的 NAS 副本进回收站 | 按(文件名, 存根 plist 里的真实大小)一对一配对为改名,在 NAS 上改名;无法证明的,暂缓删除(`Plan.deferred`),不进回收站 |
| 11 | download 策略根本下载不了存根文件;离线预下载把不存在的路径算作"已下载"并发出「下载完成」通知;已备份的存根文件被报成「未备份」 | `materialize_batch` 只承认存在且非 dataless 的路径;存根目标用 `brctl download` 触发下载;存根记录的大小与清单一致时视为未变;`mycloudctl materialize` / GUI「立即下载到本机」也能找到存根 |
| 12 | 存根与同名目录并存时(包下载到一半),目录被当成单个 dataless 文件:rsync 递归进去,清单留下一行目录记录,每轮删除、每周校验报缺失 | 目标已存在(文件或目录)时忽略存根;`...icloud`/`....icloud` 这类退化名按普通文件处理;上传批次永远不收目录 |
| 13 | 启动自检失败写入的 `last_error`,被随后一轮无上传的同步立即清掉 | 自检失败记为"上传参数不可用",只有一次真实上传成功才清除 |
| 14 | rc=64 的所有 "mount error" 都说成"路由器拒绝共享",掩盖了本地或网络原因 | 只有服务器端原因(NTSTATUS、No such file or directory、Permission denied)才判为 refused;其余保留原始报错;拒绝提示后附 mount_smbfs 原文 |
| 15 | usage 正则会匹配到报错行里的文件名(如 "invalid option notes.bin"),NAS 写满被说成"请更新后端" | 正则锚定到 rsync 自己的用法报错行格式,并要求 rc==1 |
| 16 | 传输中途合盖睡眠,唤醒后可能误报「传输停滞」 | `noteSystemWake` 同时重置停滞计时 |

另:下载后立即驱逐只针对已确认上传到 NAS 的文件(以前连失败批次、待逐个上传的包目录也驱逐,导致每轮重复下载);改名回退时,如果新路径只是存根,不删除旧副本。

验证:27 处修复逐一变异检验,全部被测试抓到。

#### 22.4 第二轮复审(42 个代理)与修复

对上面的修复再做对抗式复核:确认 21 项(去重后 14 项,多数是 P3 边角情况)、驳回 13 项。全部修复:

| 问题 | 修复 |
|---|---|
| 移动/改名已移出本地的**包**,或改名它上层的文件夹:包内文件仍进回收站 | 按包名一对一配对整个目录(`_detect_package_moves`),逐个文件在 NAS 上改名;配不上的(同名包同时出现两个、原地改名)暂缓删除 |
| Finder「替换」:把移出本地的文件拖到已有同名文件的位置,旧路径的副本进回收站 | 落在已有清单行位置、且存根大小与该行不同的,也算"可能移来的",参与暂缓判断 |
| 暂缓删除太宽:只要有一个从没下载的存根与被删文件同名或同大小,真实删除就永远暂缓 | 收窄为:同名且同大小(或存根大小不可读)、同目录且同大小、或已消失的上层包目录同名/同目录同扩展名;暂缓数写入文件夹状态,`mycloudctl status` 与 GUI「问题」页显示「暂缓的删除」 |
| 深度校验发现 NAS 缺失的行(pending)被配对改名,改名回退时形成死循环,新路径既不报告也不下载 | pending 行不参与配对;它的到达项按 dataless 报告/下载 |
| 部分备份过的包被移出本地后显示为已备份,失败文件从列表消失 | 包内有 pending 行时包仍算 dataless;`_resolved` 用 `_source_has`,包内失败文件留在 failed_files,并把包本身加入待下载 |
| download 策略下载回来的包:当轮不上传、永不驱逐,而且报成已完成 | 下载回来是目录的,当批展开为文件上传(跳过清单已一致的文件);全部落地才算完成并驱逐 |
| 单文件来源已备份,download 策略下仍每轮下载再驱逐(驱逐改写父目录,kqueue 又触发一轮,循环) | 先判断"NAS 副本大小 = 存根记录大小",已备份就不下载;离线预下载只取上次在线时报告为未备份的 |
| 计划阶段的字节数用的是存根自身大小(几百字节),进度条和待下载量错几个数量级 | 存根目标按 plist 里的真实大小计 |
| 下载中途路径消失(文件夹被移走)或 brctl 直接拒绝的存根,被当作超时,触发 24 小时"iCloud 下载被系统限制" | `materialize_batch` 把消失的路径记为 gone(不算成功也不算失败)、brctl 立即拒绝的记为 rejected;两者都不进入限制判定 |
| recovery 后的那轮同步再失败时,不检查守卫就退避并显示「NAS 挂载正常」 | 先检查守卫;守卫失败就交给主循环下一拍重新 recovery |
| 未变化的单文件来源被 rsync 空跑一次也算"成功上传",清除了别处的错误 | 只有真正复制了文件、且不处于"上传参数被拒"状态时才算 |
| 别的子目录的局部同步上传成功,就清除了某个文件反复失败的错误(提示闪烁、退避升不上去) | 非 usage 错误只由全量同步清除;同时,非 usage 退避期间不再挡住局部同步(否则一个 NAS 写不进的文件会让所有编辑最多等 1 小时) |
| `_end_interrupted_pass` 抹掉看门狗刚保存的守卫失败原因 | 只写状态,不清 guard |
| GUI:只有 last_error 时提醒行只显示一个「。」;睡前已显示的「传输停滞」在短暂睡眠后消失 25 分钟 | 提醒文字加上"守护进程报告了错误",永远不会是空句;停滞计时跨睡眠暂停(记录 willSleep 时间),而不是清零 |

另修一个旧问题:要删除的文件在 NAS 上已经不存在时,清单行以前永远删不掉(每轮都计划删除、计入熔断阈值),现在直接视为删除完成(仅当镜像根目录存在时)。

仍未解决、记录在案:单个文件导致 rsync 2.6.9 致命失败(例如 FAT32 上超过 4 GB 的文件,`write failed`,exit 11)时,每次全量同步都在那一批中止,后面的批次和文件夹本轮都传不了。要隔离它,得把失败批次拆开重试;但 `rsync_no_times` 下每次重试都会重传并在回收站里多存一份,先不做。包的"云端已修改"(存根大小与清单行合计不一致)未检测,因为包占位文件里的 NSURLFileSizeKey 语义没有实机确认;移动的同时又改名的单个存根文件仍可能被当作删除(1.3.0 起就有,发生条件很窄)。

验证:第二轮 22 处修复逐一变异检验,全部被测试抓到(一处冗余判断因此删除)。

#### 22.5 第三轮复核(27 个代理)与收敛

确认 18 项(4 项 P2)、驳回 5 项。改用更一般、更保守的规则,而不是继续加特例:

- **有占位文件的文件夹里,局部同步不再执行删除**,删除一律等全量同步(最多 full_interval,网关为 15 分钟)。局部同步只看得到自己范围内的占位文件,分不清"删除"和"移出本地的文件被移到别处"。由清单 meta `placeholders:<文件夹>` 标记;全量遍历没再见到占位文件时清除。(P2:局部同步把暂缓的删除送进回收站)
- **包的同名判定看所有占位目标**,包括已有清单行的(Finder「替换」到已备份的同名包、移动到一半中断);**绝不把包配对改名到已有清单行的目标上**,NAS 上的改名也绝不覆盖已存在的文件(回退)。(P2 ×1,P3 ×2)
- **Finder 的「保留两者」/复制命名**(「report 2.pdf」「report copy.pdf」「报告 副本.pdf」)按原名参与暂缓判定。(P2)
- **失败计数的指数封顶**:连续失败超过 1024 次时 `2 ** 1024` 会溢出,异常逃出主循环,导致每 35 秒重跑一次全量规划。(P2)
- 改名保留 `pending`;包移动时 pending 行不参与配对;下载回来的包只有在"下一轮会被视为已备份"时才驱逐(有行、无 pending、无未落地的失败文件),否则保留在本地,避免下载→驱逐循环;包内指向目录的符号链接按文件上传;失败文件对应的包遵守排除规则;展开的包计入进度总数;`ensure_online` 自己的重连成功后清除 recovery 请求(避免启动时二次卸载重挂);暂缓删除数只由全量同步写入;`mycloudctl status` 不统计已移除来源。
- 非 usage 退避期间,局部同步恢复为等待(第二轮放开后,每次编辑都会重试失败的大文件并把退避推到上限)。
- GUI:停滞基线在状态文件只是陈旧(唤醒后守护进程尚未写入)时保留,不再清零;睡眠时长从 willSleep 算起,但基线不会超过唤醒时刻。

仍未处理(已评估,风险低或会引入更多误判):两个从没下载的同扩展名包,删除其中一个旁边的包会一直暂缓(可在「问题」页看到);包在云端被修改后不会被识别;移动的同时又改名的单个存根文件仍按删除处理;单个文件让 rsync 2.6.9 致命失败时每次全量同步在该批中止(见 22.4)。

验证:Python 886 个测试 + GUI 五门全绿(LogicTests 107 项);第三轮修复逐一变异检验(Python 12 处、GUI 3 处),全部被测试抓到,两处冗余判断因此删除。

#### 22.6 1.3.2:失效的符号链接按链接备份(2026-10-04)

用户在网关上查「永久失败清单」,里面主要是**失效的符号链接**,而且几乎都是"指向另一台 Mac 的路径":开发机上用绝对路径(`/Users/<开发机用户名>/…`)建的链接,经 iCloud 同步到网关,网关上没有那条路径。它们在开发机上是好的。

自 2026-08-01 起,守护进程把所有失效链接都挡在上传之外:openrsync 按 stat()(跟随链接)处理每个 --files-from 条目,遇到失效链接就悄悄丢掉同批其余文件(exit 23),当时 61 个这样的链接卡住了 911 个正常文件。代价是网关上这些链接永远备份不了,也永远留在清单里。而网关的 rsync 2.6.9 按 lstat() 处理条目,链接本身照常复制:用 Apple rsync-55 源码编译的二进制实测,两种上传方式都把链接作为链接复制,同批后面的文件也正常传,exit 0;目标改变时,旧链接进回收站。

修复:`SyncEngine.dangling_link_selftest`,启动时用**真实上传参数**、两种方式在本机临时目录试传 `[a.txt, 失效链接, z.txt]`。只有链接按原目标被复制、且 z.txt 也到了,才判定可以带失效链接(`_dangling_links_ok`),否则维持原来的排除。按实际行为判定,不看版本号。开发机(openrsync)结论为否,行为不变;网关(2.6.9)结论为是,链接正常备份,清单里这一类会在下一轮自动移出。

验证:Python 895 个测试 + GUI 五门全绿(其中 2 个测试用真实 rsync 2.6.9,设置 `MYCLOUD_RSYNC269` 时运行,本机已用 rsync-55 构建跑过);4 处修改逐一变异检验,全部被测试抓到。
