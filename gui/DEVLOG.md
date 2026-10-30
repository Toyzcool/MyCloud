# gui-macos12 开发日志(监督循环记录)

> 模式:监督者(审查代理 + 本日志)持续盯问题 → 编码修复 → `bash gui/selftest.sh` 自测 → 绿了提交。
> 自测三道门:①双架构 macOS 12.0 编译零错误 ②宿主机全视图**真实渲染**冒烟(带桩数据) ③python 全量回归。

## 迭代 1(2026-08-01)— 自测基建落地

- 背景:GUI 此前唯一的自动化门槛是"能编译"。本分支已发生过一次"编译干净、运行必炸"的事故(全文替换把 `groupedFormCompat()` 的实现替换成自调用 → macOS 13+ 无限递归),暴露了缺口。
- 新增 `gui/selftest.sh` + `gui/tests/RenderSmokeTest.swift`:14 个渲染用例覆盖全部视图(概览/同步项/问题/设置/引导/菜单栏面板/根导航/状态栏控制器生命周期),桩数据覆盖 空闲/同步中/失联横幅/问题面板满载/配置缺失 等分支;崩溃时以最后一行 `RUN:` 定位肇事用例。
- 修自测脚本自身:gate 3 曾用 `tail -3` 展示(被测试里的 argparse 噪音污染)且套件跑两遍;改为单次捕获 + 摘要行过滤。
- **结果:三门全绿**(双 target 0 错误;14/14 渲染 OK;python 619 绿)。

### 已知不可本机验证、留待 macOS 12 实机的点

- NavigationView 双列回退的真实布局(sidebar 选中驱动 detail、标题位置)。
- `groupedFormCompat()` 的 12 分支(默认 Form 样式)观感。
- NSPopover 在 12 上的尺寸自适应与深浅色。
- `openMainWindow()` 全窗口关闭后经 reopen 事件重建窗口的行为。

## 迭代 2(2026-08-01)— 监督审查结论落地:9 项发现,10 处修复,新增第 4 道门

监督代理交付 9 项 findings(3 CONFIRMED + 6 PLAUSIBLE/备忘)。逐条处置:

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 🔴 CONFIRMED | 两个 SF Symbol 是 2023/macOS 14+(`arrow.triangle.2.circlepath.icloud`、`gauge.with.dots…`),12/13 上 `NSImage(systemSymbolName:)` 返回 nil → **同步进行时菜单栏图标全程空白**(概览大徽章、侧栏图标同炸;main 分支在真 13 上其实早已坏,被 macOS 26 烟测机掩盖) | ✅ 换 2019 符号(`arrow.clockwise.icloud`/`gauge`);refreshIcon 加 nil 兜底 `icloud`;**新增 gate 2「符号版本门」**:扫全部字符串字面量,凡是 CoreGlyphs 数据库认识的符号名必须 ≤12.0,负面用例验证会拦截 |
| 2 | 🔴 CONFIRMED | `AppState.start()` 不幂等:每开一扇窗泄漏一个永生 1s Timer(RunLoop 持有),常开机 CPU 阶梯上升 | ✅ `guard timer == nil`;首次 start() 移入 `applicationDidFinishLaunching`(图标刷新不再依赖开窗) |
| 3 | 🔴 CONFIRMED | `openMainWindow()` 两个匹配条件全死(窗口 title 实为 navigationTitle「概览」等;`id:"main"` 前缀本分支已删)→ 每次点击都走 LaunchServices 兜底,最小化不还原,iCloud 换过构建后可能拉起**第二实例** | ✅ 改按 `canBecomeMain` 匹配 + `deminiaturize`;bundle-open 仅零窗口兜底 |
| 4 | 🟠 PLAUSIBLE | Monterey 裸 Form 无滚动容器,设置/同步项超窗即裁死 | ✅ shim else 分支包 ScrollView(实机复核排版) |
| 5 | 🟠 PLAUSIBLE | 12 的 NavigationView 侧栏可拖拽整列收起且无恢复入口 → 导航瘫痪 | ✅ 12 分支工具栏加 `toggleSidebar` 按钮(`sidebar.left`,2019 符号) |
| 6 | ✅ 核查通过 | AppDelegate 时序无缺陷;衍生 UX 坑:关窗后弹窗操作的成功回执滞留 lastError,下次开窗弹陈年 alert | ✅ 成功回执仅在有可见主窗口时写入(失败仍始终保留) |
| 7 | 🟠 PLAUSIBLE | 12 上 NSPopover 不跟随 NSHostingController 高度变化(进度行出现/消失时裁切) | ✅ 进度行常驻占位(高度恒定)+ show 前显式 `contentSize = fittingSize` |
| 8 | 🟠 PLAUSIBLE | transient popover mouseDown 先关、mouseUp 又开 → "点图标关不掉" | ✅ didClose 时间戳 + 0.15s 内 toggle 忽略 |
| 9 | 🟠 PLAUSIBLE | 12 上 detail 列就地换类型有 toolbar 重复/丢失史 | ✅ `DetailPane` 稳定容器(列类型恒定,内部 switch);另加 selection 取消选中自动回弹概览 |

- **自测:四门全绿**(双 target 0 错误 / 符号门通过+负面用例验证 / 14 渲染 OK / python 619 绿);修复版已重建发布 dist/(iCloud 分发),macOS 26 冒烟通过。
- 实机验证清单(网关机,14 项)以监督报告为准,重点:同步中图标、开关窗 5 次 CPU 平稳、「打开 MyCloud」三态、设置页滚动、侧栏收起恢复、popover 尺寸/开关、toolbar 毛刺、深浅色、Intel 双架构。

## 迭代 3(2026-09-12)— 设置页乱版修复 + 「云盘连接」配置

- **用户报告**:网关机(macOS 12)设置页"乱码"。判断为 Monterey 裸 `Form` 的布局失效(迭代 2 的 ScrollView 包裹只解决了裁切,标签列仍乱)。修复:`CompatForm` 容器——13+ 仍是 `Form.formStyle(.grouped)`,12 改用 `List`(自带滚动、Section 头、整行宽度),同步项/设置两页统一切换。**待实机截图确认**;若仍异常,需要看到真实画面再定位。
- **新功能**:设置页「云盘连接」分区——服务器地址、共享名、连接模式(注册用户 / 匿名 guest)、用户名 + 密码(SecureField,留空沿用已保存凭据)、「测试并保存」(忙碌态 + 后端回执);更换服务器/共享名弹确认(清单将重建、旧目标数据保留)。
- **安全边界**:密码经 `Bridge.runCtl(stdin:)` 管道进 ctl(`--password-stdin`),再经 `security -i` 的 stdin 进钥匙串并写入 nsmb.conf(600)——全链路不进 argv、不进日志、不在视图状态里驻留(调用后即清空)。测试断言每一个被 spawn 的 argv 都不含密码。
- **后端契约**:`server-set` 用临时挂载点实测连接(不碰守护进程自己的挂载点),哨兵接管或初始化 MyCloud 目录,换目标时旧清单转存 `.bak`(其行描述的是旧共享),bootout 异步 → 等卸载与锁释放后再动清单/重启。
- **自测**:四门全绿(python 635,+16;渲染冒烟新增匿名模式设置页)。

### 迭代 3b — 监督审查结论落地(20 项发现,P0 全修)

审查代理对 commit 4b577b0 做了对抗性审查并在本机实证了 `security -i` 分词、挂载表 realpath 等行为。处置:

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 🔴 CONFIRMED | 换 host/share 后旧共享仍挂在守护进程挂载点 → 新守护进程 rc=64 File exists、`is_mounted` 永不匹配 → **永久 OFFLINE** | ✅ server-set 停守护进程后 `_release_daemon_mount`(umount→diskutil force,realpath 判定),卸不掉则中止并 resume;mount.py 对"挂载点上是别的挂载"force_unmount 重试一次 |
| 2 | 🟠 PLAUSIBLE-高 | 匿名模式挂载表显示 `//GUEST:@host/share`,前缀匹配 `//guest@` 永不命中 → 守卫层②永远失败,而验证只看 rc | ✅ `is_mounted` 改为正则逐字段匹配(用户去尾冒号、用户/主机/共享不区分大小写、挂载点精确);server-set 验证后加 `is_mounted()` 复核 |
| 3 | 🔴 CONFIRMED | 凭据**先写后验**,验证失败不回滚 → 输错密码直接覆盖生效凭据,下次重挂载 rc=77 | ✅ 顺序重排:验证通过**之后**才写钥匙串/nsmb.conf;MountManager 新增候选 `password`,验证用嵌入 URL 直连(不走 -N,避免钥匙串旧密码/会话复用"代考") |
| 4 | 🟠 PLAUSIBLE-高 | 同 host/同 user 且守护进程有会话时,SMB 会话复用让任何密码"验证通过" | ✅ 提供新密码时先停守护进程并释放挂载点再验证;检测到本机对该服务器还有其他挂载(如访达)时在回执中明示"本次验证可能不反映新密码" |
| 5 | 🔴 CONFIRMED | `_wait_agent_stopped` 15s < launchd ExitTimeOut 20s;超时后仍搬清单、忽略 resume 返回码、谎报"已重启" | ✅ 预算 45s;超时 → 中止、不搬清单不提交;`stopped` 在 bootout 后立即置位,finally 必 resume 并按返回码写回执 |
| 6 | 🔴 CONFIRMED | `_release_temp_mount` 用非 realpath 判"仍挂载"永为假,返回值被忽略 | ✅ mkdtemp 取 realpath;先查挂载表再卸;未释放写入警告回执 |
| 7 | 🔴 CONFIRMED | 哨兵读失败(如 TCC)被当"不存在" → 用 `"w"` 截断覆盖别机哨兵 | ✅ 仅 FileNotFoundError 视为不存在,其他 OSError 报错(含 TCC 提示);写入改 `"x"` 永不截断 |
| 8 | 🟠 设计 | 静默接管外来哨兵削弱单写者铁律 | ✅ 需显式 `--adopt`;否则返回 `needs_confirm=adopt` 信封,GUI 弹「接管现有备份?」二次确认 |
| 9 | 🔴 CONFIRMED | `_validate_candidate` 排在 pause+转存之后 | ✅ 候选配置在任何副作用前先校验 |
| 10 | 🟠 PLAUSIBLE | GUI 120s 超时 vs 最坏路径;SIGTERM 让 finally 不跑 | ✅ ctl 期间 SIGTERM→SystemExit(finally 照跑);GUI 超时 240s |
| 11 | 🔴 CONFIRMED | target_changed 大小写/尾斜杠敏感 → 无谓重扫 | ✅ 两侧归一化(lower + strip /) |
| 12 | 🔴 CONFIRMED | 密码首尾空格:钥匙串保留、nsmb 读取 strip → 后台用另一个密码 | ✅ 拒绝首尾空格密码 |
| 13 | 🔴 CONFIRMED | 用户名未校验(`@ / : \ "` 与空格会错位 URL) | ✅ 拒绝 |
| 14–20 | P2 | 匿名文案不准、`[host:USER]` 段未删、uninstall 对 guest 删钥匙串、哨兵消失不转存、busy 串扰、部署耦合报错不明、Bridge stdin 写法、验证后等待守护进程真上线 | ✅ 全部处理:文案改写;`[host:*]` 段一并删;uninstall 对 guest 跳过;哨兵消失→转存清单;局部 `serverBusy` + 输入禁用 + 提示行常驻(避免 Monterey List 重建丢焦点);decodeFailed 文案指向"后端未更新";`write(contentsOf:)`;新增 `_wait_daemon_online` 60s 等 state.json 上线并写回执(验证证明的是 GUI 上下文能连通,不等于 launchd 守护进程能——回执明说) |

- 结论:审查前的实现有 5 个 P0,任何一个都会在真实换目标/改密码操作里把网关打趴;监督循环第一圈就拦住。新增 15 个测试(python 619→650+),覆盖:验证失败零落盘、新密码先停守护进程再验证、旧共享卸载与卸载失败中止、外来哨兵需确认与显式接管、不可读哨兵不截断、守卫失配拒绝保存、停机超时不动清单、用户名/密码边界、目标归一化、哨兵消失转存、匿名删 `[host:*]`、GUEST: 挂载行匹配、候选密码只走嵌入 URL。
- 实机验证清单(网关机)以审查报告 16 项为准,重点:换共享后旧挂载是否残留、匿名保存后 `mount -t smbfs` 的 from-name 原文、同目标错密码是否被拦、SYNCING 中换目标的停机耗时、校验挂载零残留、List 内文本框首击可输入且不丢焦点。

## 迭代 4(2026-09-12)— 全流程 GUI 化:安装向导 / 卸载 / 授权引导

- **起因**:用户在引导页看到"请在终端运行 install.sh",要求所有操作在 GUI 内完成。
- **后端**:`install`(移植 install.sh,复用 server-set 做凭据/验证/哨兵)与 `uninstall`(移植 uninstall.sh,保留 NAS 硬闸门)两条 ctl 命令;守护进程把守卫失败机制写进 state.json `guard` 字段,上线即清。应用包新增 `Resources/backend/`(后端 + 模板),版本 1.1.0。
- **GUI**:引导页改为单页三步安装向导(环境检查 / 云盘连接 / 备份内容 → 安装并启动);概览页新增 FDA 引导横幅(打开设置面板、访达选中 python3、重启守护进程);设置页守护进程分区新增卸载(确认弹窗);Bridge 泛化为 `runProcess` 支持环境探测与运行包内 ctl。
- **自测**:渲染冒烟新增"权限被拒横幅"用例;install/uninstall 7 项单测(布局/模板渲染/重装保留配置/接管透传/密码 stdin/卸载备份/挂载硬闸门)。launchctl 桩改为按 label 记状态(看门狗与 agent 分开)。
- **待实机**:向导在 macOS 12 上的 GroupBox/ScrollView 排版;FDA 面板 URL 在 Monterey 的实际跳转;`xcode-select --install` 弹窗在 GUI 上下文是否出现;安装后 45s 内守护进程上线与否的回执文案。

### 迭代 4b — 自审(监督代理因组织用量上限中断,由主会话接手)

监督代理在本轮撞上 org 月度用量上限(9pm 重置)未能出报告;主会话自行对安装/卸载的**失败路径**做了审查,发现并修复 4 项:

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 🔴 | 重装时 install 先 `pause` 了正在运行的守护进程,之后任一步失败都直接 return → **机器被留在"守护进程已停、配置还是旧的"状态,备份静默停摆** | ✅ `stopped/completed` 标志 + finally:未完成即 resume(回到旧配置继续备份) |
| 2 | 🟠 | 首次安装验证失败后 config.ini 已落盘,重试时被判为"已有配置"而保留 → 用户在向导里改正的备份源被静默忽略 | ✅ 以 `sentinel_uuid` 是否存在判断"安装是否真正完成";未完成则重新渲染 |
| 3 | 🟠 | `remove_local_tree` 第一遍勘察用 `os.walk` 默认吞掉权限错误 → 不可读子树被跳过,其中的挂载点无法被发现 | ✅ `onerror` 抛出:不可读子树中止整次删除 |
| 4 | 🟠 | 卸载时挂载点释放失败会 return,但两个 agent 已被 bootout → 备份停了而用户只看到报错 | ✅ 失败路径先 resume,错误文案明说"备份已恢复运行" |

- 另做包内后端沙箱冒烟:`Resources/backend/mycloudctl.py` 可独立 import 自带包、argparse 正确、错误以 JSON 返回、`MYCLOUD_APP` 指向临时目录时零副作用,本机真实环境未被触碰。
- 自测:四门全绿(python 662→**666**)。
- **仍待监督代理复审**(用量恢复后):C 类 TCC/权限边界与 D 类 macOS 12 向导控件行为(GroupBox/NSOpenPanel/keyboardShortcut/Picker(Bool))尚未经第三方对抗性审查。

### 迭代 4c — 关闭最后两个"仍需终端"的操作

- **iCloud 云端文件下载**:问题页原来的「下载并纳入备份」只是把 `icloud_policy` 改成 download,**而 launchd 守护进程根本无法触发 iCloud 下载**(fileproviderd 拒绝,每文件卡 120s;PLAN §14 实证)——这个按钮承诺了做不到的事。现在改为「立即下载到本机」,由 **GUI 自己(用户会话上下文)** 运行 `materialize`;`Bridge.runProcess` 新增 `onOutput` 流式回调,把 ctl 的「已下载 N/总数(X GB)」实时显示成进度行。相应删除已失去触发点的旧确认弹窗。
- **日志**:设置 → 守护进程新增「打开日志文件夹」(等价 `mycloudctl logs`)。
- 至此常规运维全部可在 GUI 完成:安装 / 连接 / 授权引导 / 同步项 / 阈值 / 立即同步 / 暂停恢复 / 删除审批 / 深度校验 / 云端文件下载 / 诊断修复 / 日志 / 卸载。仅剩清单迁移(换机)这类一次性运维仍走命令行。
- 自测:四门全绿(666)。

## 迭代 5(2026-09-26)— 运行时缺陷审查修复(1.3.0,PLAN §21)

- **起因**:用户报告"守护进程时常失败导致无法同步",而 GUI 仍显示「空闲✓」——GUI 审查确认 3 个 P0 级问题:守护进程死掉时的健康显示、Bridge 丢输出竞态、首次安装失败被困。
- **健康显示**:`daemon_pid` 探活(kill 0)+ 按写入节奏的预算 + 传输停滞检测 + launchd 加载探测(已暂停 vs 已退出);新增状态文案 启动中/重启中/已暂停/守护进程已退出/同步停滞/需要处理;菜单栏符号白名单化。
- **Bridge**:`PipeSink`(整行回调)+ `RunBox`(单次 resume、释放 Process)+ 两管道 EOF 完成条件 + `PYTHONUNBUFFERED` + SIGTERM 宽限 45s(`CtlRun.timedOut`)+ `F_SETNOSIGPIPE` + `RunHandle`(下载可取消)。
- **流程**:安装完成判据改为 agent plist;接管确认改为根视图单一 alert 且按钮携带请求(原实现从未生效);移除/改落点确认改用 `presenting:`;busy 按操作计数;设置控件在 ctl 运行期间禁用;配置静默重试;通知与错误分两个通道;IntSettingRow 回滚。
- **自测**:本机 Xcode 许可未同意 → CLT 方案:`@State` 换成包装真实 `SwiftUI.State` 的运行期 shim 后,渲染冒烟与新增 `LogicTests.swift`(87 项:Bridge 契约含 SIGTERM 宽限、活性/停滞、busy 重叠、配置重试、接管捕获、模型)都能在 CLT 下编译运行,全部通过;双架构类型检查 0 错;符号门通过。该方案固化为 `gui/tools/clt_check.sh`(Xcode 不可用时跑前四门中的 GUI 部分,不产出 App)。`selftest.sh` 增为 5 门。
- **待实机**:macOS 12 上根视图单一 alert 的三种内容切换;概览新横幅在 Monterey List/ScrollView 中的排版;`launchctl print` 在 12 上的输出格式(退出码行)。

## 迭代 6(2026-10-03)— 1.3.1:唤醒宽限、诊断项与文案(PLAN §22)

- **唤醒宽限**:`AppState` 监听 `NSWorkspace.didWakeNotification`,唤醒后 120 秒内(`LivenessBudget.wakeGrace`)不因 state.json 陈旧而判「守护进程无响应」;守护进程 pid 已死仍立即报。原因:睡眠多久,state.json 就旧多久,直到守护进程醒后第一次写入(它要先重新检查 NAS,最长约一分钟)。LogicTests 新增 3 项检查(共 90 项)。
- **诊断**:新增 doctor 行 `rsync_upload`(真实上传参数本机试传),中文名「rsync 上传命令自检」。
- **文案**:深度校验结果从「补传 N」改为「缺失 N … 已排队重传」。旧文案读起来像"已完成",8 月就是它让人误以为网关迁移成功。
- **对抗式审查后的 GUI 修复**(PLAN §22.3 #1、#16):`needsAttention` 把守护进程的 `last_error` 也算进去(状态显示「需要处理」、菜单栏显示感叹号;正在传输时仍显示同步箭头)。以前一次保留挂载的失败只在状态下方留一行灰字,徽章还是蓝色的「同步中」或绿色的「空闲」。`noteSystemWake` 同时重置停滞基线:守护进程醒来后的心跳会重写睡前的进度快照,睡眠时长不应算作传输没动。LogicTests 新增 10 项检查(共 100 项),包括"唤醒通知晚于第一次轮询"的情况。
- **第二轮复审**(PLAN §22.4):停滞计时改为跨睡眠**暂停**:监听 `willSleepNotification` 记下入睡时间,醒来时把基线顺延睡眠时长,而不是清零。清零会让睡前已经显示的「传输停滞」在短暂睡眠后消失 25 分钟。没收到入睡通知时(暗唤醒等)仍按旧办法从唤醒时刻重新计时。提醒行在只有 last_error 时不再只显示「。」(`attentionSentence`)。「问题」页新增「暂缓的删除」:可能是移动了仅在云端的文件,NAS 副本暂时保留。LogicTests 共 105 项。
- **第三轮复核**(PLAN §22.5):第二轮的停滞修复在真实时序下仍会失效。唤醒后、守护进程第一次写入之前,状态文件看起来已陈旧,`progressLive` 为假,`updateStall` 就把基线清零了。现在只有进度结束或不在传输时才清零;仅仅陈旧时保留基线。睡眠顺延量封顶在唤醒时刻。测试改为生产时序(时钟在唤醒通知时已是唤醒时间,先有一次陈旧的轮询)。LogicTests 共 107 项。
