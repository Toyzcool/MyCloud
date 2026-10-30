# 常开 Mac 备份网关部署指南

> 对应 PLAN §17。目标:一台常开 Mac 作为 iCloud → NAS 的备份网关——其他电脑/手机往 iCloud 写入,常开 Mac 自动镜像到 NAS。
> 本指南浓缩了首台机器部署时踩过的**全部**坑;按顺序做,每步都有"为什么"。

## ⚠️ 目标机是 macOS 12(Monterey)——先读这一节

开发与实测都在 macOS 26 上完成;针对 macOS 12 已逐项核查(2026-08-01):

**✅ 后端完全兼容,可直接部署**
- 守护进程/CLI 代码无 Python 3.9 独有特性(已全量扫描),CLT 自带 python3 ≥3.8 即可运行;**推荐装 CLT 14.2**(Monterey 可装的最新版,自带 python 3.9.6,与开发机完全同版):`/usr/bin/python3 -V` 检查。
- launchd(bootstrap/bootout/kickstart)、mount_smbfs 全部选项、smbutil statshares、FDA、SF_DATALESS 判定、SIGINFO 栈转储、sqlite WAL——macOS 12 全部支持。
- iCloud Drive 自 12.1 起就是 FileProvider 架构,与开发机一致;**请先把系统升到 12.7.x 最新小版本**。

**✅ 反而少一个坑**
- macOS 12 没有「本地网络」TCC 拦截(macOS 15 才引入)——python3 直连 445 探测畅通,不再依赖 smbutil 兜底(代码自适应,两种环境同一套代码)。

**⚠️ rsync 是另一个实现(需要知道,但设计已自适应)**
- macOS 12 的 `/usr/bin/rsync` 是 samba rsync 2.6.9 真身(openrsync 是 macOS 15+ 才默认)。两者对 `-b`+`--delete` 组合、毒丸条目等行为可能不同。
- 系统性保障:①能力探针**首次启动自动记录该机指纹**(首跑不会误触发 fail-closed);②写模式 rsync 永不携带 `--delete*`(静态不变量),无论哪种行为都安全;③失效 symlink 一律不进批(两种实现下都正确)。
- 首启后在 GUI「问题 → 运行诊断」(或 `mycloudctl.py doctor`)确认「rsync 能力探测指纹」行 OK。真 rsync 的 `delete_suppressed_with_backup: false` 是实现差异而非故障,doctor 会附说明并判 OK(2026-08-01 前的旧版 doctor 曾把它误报 FAIL;若见到该误报,用仓库最新 `src/mycloudctl.py` 覆盖 `~/Library/Application Support/MyCloud/mycloudctl.py` 即可)。
- 若在新机跑测试套件:`OpenrsyncQuirkRegressionTest` 可能因实现差异而红,**属预期**,不影响部署(部署不要求跑测试)。

**✅ GUI 应用已支持 macOS 12(2026-08-01 gui-macos12 分支)**
- MyCloud.app 现为 universal(Intel+Apple Silicon)、最低系统 12.0:菜单栏改用 AppKit NSStatusItem,主窗口在 12 上走双列 NavigationView(13+ 仍是 NavigationSplitView),功能一致。
- `dist/MyCloud.app` 随 iCloud 同步到网关机,首次运行**右键 → 打开**(绕过 Gatekeeper 对 ad-hoc 签名的拦截)。
- headless 运维照旧可用:`/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" status`(`doctor` / `logs` 同理);备份新鲜度也可在任何设备看 NAS 上的 `MyCloud/last_sync.json`。

**其它**
- 「优化 Mac 储存空间」在 Monterey 的位置:**系统偏好设置 → Apple ID → iCloud**,取消勾选列表下方的「优化 Mac 储存空间」。
- Intel 或 Apple Silicon 均可(后端全部走系统自带二进制,无架构依赖)。

---

## 0. 前提确认(开始前逐项打勾)

- [ ] 新机磁盘空闲 **≥150 GB**(iCloud 全量约 70 GB + 增长余量)
- [ ] 新机与 NAS(192.168.5.1)在**同一局域网**,`smbutil view -N //admin@192.168.5.1` 能列出 SHARE
- [ ] 新机登录**同一个 Apple ID**,iCloud 云盘已开启
- [ ] 旧机(笔记本)上的 MyCloud 仍在正常运行(迁移最后一步才停它)

## 1. 系统准备

```bash
xcode-select --install
```

装 **Command Line Tools 即可,不要装 Xcode**(提示 already installed 说明已装,直接继续)。然后检查两项:

```bash
/usr/bin/python3 -V
```

≥3.8 即可运行(3.9.6 最佳;若是 3.8 且想对齐开发机,可从 developer.apple.com 下载 CLT 14.2 安装包升级)。若新机已有 Xcode,确认:

```bash
xcode-select -p
```

输出必须是 `/Library/Developer/CommandLineTools`。

> **为什么**:守护进程跑在 `/usr/bin/python3` shim 上,它跟着 xcode-select 走。指向 Xcode 会把 Python 真身换成 Xcode 内置版,FDA 授权失配,重启后备份静默停摆(2026-08-01 实证事故,PLAN §15.2)。

## 2. iCloud:关闭「优化储存空间」并等全量下载 ⚠️ 成败关键

macOS 12:**系统偏好设置** → Apple ID → iCloud → 取消勾选「优化 Mac 储存空间」。(macOS 13+ 在:系统设置 → Apple ID → iCloud → iCloud 云盘。)

> **为什么**:launchd 后台进程曾**无法**触发 iCloud 下载(fileproviderd 拒绝,每文件卡 120 秒,实证;LaunchAgent 现已声明 `MaterializeDatalessFiles`,守护进程会先用 4 个文件试探、每天重试一次,但不要依赖它)。关闭优化后由系统自动保持全量本地化,新文件自动物化——这是整个网关架构的前提。

然后**等待 iCloud 全量下载完成**(首次可能数小时)。检查还剩多少云端占位文件:

```bash
/usr/bin/python3 -c "
import os
SF=0x40000000; n=0
for dp,dd,fs in os.walk(os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs')):
    for f in fs:
        if f.startswith('.') and f.endswith('.icloud'): n+=1; continue
        try:
            if os.lstat(os.path.join(dp,f)).st_flags & SF: n+=1
        except OSError: pass
print('仍在云端:', n)"
```

归零(或只剩个位数)后继续。两种云端占位都会被计入:macOS 14 及以后是"无数据"文件(SF_DATALESS);**macOS 12/13 是隐藏的 `.文件名.icloud` 小文件**——早先的版本只数前一种,在 Monterey 上永远显示 0。

## 3. 安装 MyCloud

**推荐:GUI 安装(2026-09-12 起)**。从 [Releases](https://github.com/Toyzcool/MyCloud/releases) 下载最新的 `MyCloud-x.y.z.zip`,解压后把 `MyCloud.app` 拷到「应用程序」,首次打开见 README §3.0,按引导页的三步向导操作(环境检查 → 云盘连接 → 备份内容 → 安装并启动)。应用自带后端,不需要 clone 仓库或开终端。

终端方式仍然可用(先把仓库 clone 到 iCloud 云盘以外的位置,例如 `~/src/MyCloud`):

```bash
cd ~/src/MyCloud
bash install/install.sh
```

交互要点:服务器/共享/连接模式/用户名回车接受默认(192.168.5.1 / SHARE / user / admin),确认一次连接地址,再输入密码(不回显)。安装程序会先用这个密码**实测挂载**,通过后才把它存入登录钥匙串和 `nsmb.conf`(权限 600;launchd 的钥匙串访问数日后/重启后会失效(rc=77),nsmb.conf 是必备保险,已自动写入,无需再选)。共享上若已有旧网关的哨兵,会要求输入 `adopt` 明确接管。

> 刚装完时守护进程通常处于「离线」:还没有「完全磁盘访问」,它连 NAS 上的哨兵文件都读不了(见第 5 步)。这正好——**先做第 4 步的清单迁移,再授权**,守护进程就不会在空清单上开始全量重传。

## 4. 清单迁移(省 ~68 GB 首轮重传;在授予完全磁盘访问之前做)

**旧机**:先停守护进程,再用 SQLite 自己的备份命令导出一致的副本(它会把 WAL 里尚未合并的事务一并带上;直接 `cp manifest.db` 在 WAL 非空时会丢最后一批事务——2026-08-01 实证,当时少了 283 行):

```bash
/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" pause
/usr/bin/sqlite3 "$HOME/Library/Application Support/MyCloud/runtime/manifest.db" \
  ".backup '$HOME/Library/Mobile Documents/com~apple~CloudDocs/manifest-migrate.db'"
```

**新机**(等上面的文件出现在 iCloud 后)。**必须删掉新机自己的 `-wal` / `-shm`**:它们属于新机刚建的空库,留着会在下次打开时被"回放"到迁移来的库上,把它写坏:

```bash
RT="$HOME/Library/Application Support/MyCloud/runtime"
/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" pause
rm -f "$RT/manifest.db-wal" "$RT/manifest.db-shm"
cp "$HOME/Library/Mobile Documents/com~apple~CloudDocs/manifest-migrate.db" "$RT/manifest.db.new"
mv -f "$RT/manifest.db.new" "$RT/manifest.db"
/usr/bin/sqlite3 "$RT/manifest.db" "PRAGMA integrity_check; SELECT COUNT(*) FROM files;"
```

(先拷成 `.new` 再 `mv`:换上的是新文件而不是覆盖写同一个文件,万一还有进程开着旧库也不会被写坏。)

第一行应输出 `ok`,第二行是文件数(应与旧机相当)。然后进入第 5 步授权,授权后再 `resume`。

> **为什么可行**:清单按 size+mtime 做快速比对,这两项在 iCloud 跨设备同步中保留 → 绝大多数文件命中、不重传。inode 是本机值、跨机失效,只影响重命名检测精度(首轮退化为"上传+删除",之后自愈)。
> 迁移完成后删掉 iCloud 里的 `manifest-migrate.db`。

## 5. 权限

- **完全磁盘访问**(必须,⚠️ 漏掉的后果不是"部分不可读"而是**彻底上不了线**)。GUI 概览页有一键按钮(打开设置面板 + 在访达中选中 python3);手动:macOS 12 **系统偏好设置 → 安全性与隐私 → 「隐私」标签 → 完全磁盘访问** → 点左下角🔒解锁 → `+` → 按 `⌘⇧G` 输入 `/usr/bin/python3` → 添加并勾选。(macOS 13+ 在:系统设置 → 隐私与安全性 → 完全磁盘访问。)
  - **为什么必须**:TCC 连**网络卷(SMB 挂载)上的文件读取**都管——没有 FDA 的 launchd python3 连挂载点里的哨兵文件都读不了(`Operation not permitted`),守护进程永远卡在 OFFLINE 的哨兵层(2026-08-01 网关部署实证,卡了 80 分钟)。
  - 授权后必须重启守护进程(TCC 按进程生效):GUI 点「重启守护进程」,或

```bash
/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" resume
```

  - **怎么验证真的生效了**:`tail -n 5 ~/Library/Logs/MyCloud/mycloud.log` 不再出现 `failing at layer 'sentinel'`,GUI 概览变为「空闲/同步中」。**doctor 全绿不算数**:doctor 跑在终端的 TCC 上下文里,天然比 launchd 乐观。
- **「本地网络」不用管**:macOS 12 根本没有该权限面板(15+ 才引入);即便在新系统上,面板也不会出现 python3 条目——代码已经 SMB 代理探测自动绕过,两种系统同一套代码。

## 6. 配置检查

GUI「同步项」页管理备份源,「设置」页管理其余选项;命令行查看:

```bash
open -e "$HOME/Library/Application Support/MyCloud/config.ini"
```

(`-e` 强制用「文本编辑」打开。macOS 12 没有应用声明 .ini 类型,裸 `open` 会报 kLSApplicationNotFoundErr——2026-08-01 网关部署实证。)

- `folders`:网关机通常**只保留 iCloud 根**一行:
  `~/Library/Mobile Documents/com~apple~CloudDocs`
  (若新机开了「桌面与文稿」iCloud 同步,不要再单独加 ~/Desktop、~/Documents,避免与 iCloud 树重复备份)
- `upload_concurrency = 2`(默认已是;路由器实测拐点,4 路反而更慢且掉线)
- 手改后:`/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" reload`
- **NAS 连接(服务器/共享/匿名或注册用户)不要手改 config.ini**:用 GUI 设置页「云盘连接」→ 测试并保存;或命令行。密码只经 stdin,且**不要把密码写进命令行**(会留在 shell 历史里)——先无回显地读入变量:

```bash
read -rs P && printf '%s\n' "$P" | /usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" server-set --host 192.168.5.1 --share SHARE --mode user --username admin --password-stdin; unset P
```

  匿名模式:`--mode anonymous`(不需要用户名密码;NAS 需允许 guest 访问该共享)。它会用临时挂载点实测连接、接管或初始化共享上的哨兵、更换服务器/共享时把旧清单转存为 .bak 并重启守护进程。

## 7. 验证

GUI:概览应为「空闲」或「同步中」(进度条显示增量而非全量),「问题」页运行诊断。终端依次执行(doctor 目标:除已知系统限制行外全绿;sync-now 应显示增量而非全量重传;verify 在首轮跑完后做全量对账):

```bash
CTL="$HOME/Library/Application Support/MyCloud/mycloudctl.py"
/usr/bin/python3 "$CTL" doctor
/usr/bin/python3 "$CTL" sync-now
/usr/bin/python3 "$CTL" verify
```

首轮增量 pass 预期:planning 数分钟(29 万文件的本地扫描,**不是卡死**——观察「最近传输」时间戳),之后只传旧机停机后 iCloud 新增的部分。

## 8. 常开设置

- **防睡眠**:macOS 12 **系统偏好设置 → 电池 → 电源适配器**:勾选「显示器关闭时,防止电脑自动进入睡眠」(台式机在「节能」里)。合盖运行的笔记本还需(需要管理员密码,请自己在终端执行):

```bash
sudo pmset -c sleep 0 disablesleep 1
```

- **自动登录**:守护进程是「登录项」级别的 LaunchAgent,只在有人登录的图形会话里运行——停电/更新重启后若停在登录窗口,备份就不会恢复。**系统偏好设置 → 用户与群组 → 登录选项 → 自动登录**选这个用户(开了 FileVault 时 macOS 不允许自动登录,需要权衡)。
- 验证:GUI「问题 → 运行诊断」或 `doctor` 的「power_sleep」行。守护进程在每轮同步期间会自己阻止空闲睡眠,但**空闲的网关**睡着后就不会发现新文件,这只能靠上面的系统设置。

## 9. 旧机退役(新机 verify 通过后再做)

```bash
cd ~/src/MyCloud
bash install/uninstall.sh
```

(uninstall.sh 不碰 NAS 上任何备份数据。)

> **单写者铁律**:同一 NAS 目录同时只能有一台机器的守护进程在写。两台并行会互相把对方新传的文件当"多余"归档(深度校验打架)。所以必须先确认新机接管成功,再卸载旧机——期间旧机保持 pause 即可。

## 10. 更新

- **GUI 自带后端**:用新版 `dist/MyCloud.app` 替换「应用程序」里的旧版,打开后到「设置 → 守护进程」点「更新后端」(只换程序文件并重启守护进程,连接设置、备份源、清单都不动)。应用发现已装后端比自己旧时会提示。
- **没有新 App 时**:在新机的仓库目录执行 `bash install/install.sh --refresh`,效果相同。

## 故障速查(全部为首台机器实证)

| 症状 | 原因与处理 |
|---|---|
| 状态 OFFLINE,日志 `rc=77 Authentication` | launchd 钥匙串失效 → nsmb.conf 回退应自动接管(安装时已写入);重启守护进程;仍失败就在 GUI「云盘连接」重新输入密码保存 |
| 日志 `rc=20 poll: timeout` 反复出现 | 路由器 SMB 过载 → 降 `upload_concurrency` |
| 三个源全部 sourceMissing | 曾是设备号漂移 bug,已修(源可读非空会自动收养新设备号);若再现,先 `kill -INFO <pid>` 取栈 |
| 看似卡死 | `kill -INFO $(launchctl list | awk '/mycloud.agent/{print $1}')` → `launchd.err.log` 出全线程 Python 栈 |
| doctor 报 N 个云端文件未备份 | 「优化存储」被系统重新打开或漏网 → 关掉它,并在 GUI「问题」页点「立即下载到本机」(或终端 `/usr/bin/python3 "$HOME/Library/Application Support/MyCloud/mycloudctl.py" materialize`) |
| GUI 显示「守护进程已退出」 | launchd 仍加载着它但它不在运行(崩溃循环或配置错误)。概览会显示上次退出码和错误;打开日志文件夹看 `mycloud.log` / `launchd.err.log` |
| GUI 显示「同步停滞」 | 正在备份,但 25 分钟没有任何传输进展(守护进程自己的 20 分钟击杀也没救回)→ 点「重启守护进程」;反复出现请检查 NAS/路由器 |
