# Phase 8F RC11 实机验收检查表

状态：**历史清单，已由用户指定的 LanDrop 0.6.0 正式版交付构建取代；勿用于正式版验收。** 本表的 R01～R04 未完成，结果只对应 RC11。正式版构建身份见 [`0.6.0.md`](../releases/0.6.0.md)；正式版的配对文件哈希和卸载后实机结果仍未采集。

RC11 历史候选记录：[`0.6.0-rc11.md`](../releases/0.6.0-rc11.md)

- 冻结目录：`C:\Project\LanDrop\dist\0.6.0-rc11-99628ea`
- Setup：`LanDrop-Setup-0.6.0-rc11.exe`
- Uninstall：`Uninstall-0.6.0-rc11.exe`
- A 升级基线：`upgrade-baseline-A-0.5.9\LanDrop-Setup-A-0.5.9.exe`
- 冻结 Build ID：`0.6.0-99628ea-20261017050753`
- 产物 SHA-256 以 RC11 冻结记录为准；执行前逐一复核，不以文件名代替身份验证。
- 当前冻结目录未包含 A（0.5.9）升级基线；R04 周期二暂不可执行。补入并复核独立基线文件前，不得从 RC10 记录复制其哈希来代替。
- 对话中已有鼠标激活通过的用户反馈，但测试时使用的 Build ID 未记录；请在 RC11 冻结 Setup 上复测并填入证据。

## 执行规则

- 每个检查点填写 `PASS / FAIL / N/A` 和证据；`N/A` 必须说明环境为何不具备条件，不得计为通过。
- R01、R02（适用分支）、R03 和 R04 均通过后，才可判定 Phase 8F PASS。
- Public 网络拒绝仅在不影响现有网络/系统状态的安全条件下测试；无法安全构造时记为 `N/A`，不得为测试而改动生产网络配置。
- 任一阶段出现安装状态不一致、事务残留、意外删除、无法解释的注册表/防火墙变化、传输异常或卸载失败：立即停止后续安装/卸载/清理。先记录磁盘、注册表、进程、端口和 TEMP 现场，再决定恢复方案。
- 不手工清理现场，不重复运行 Setup/Uninstall“试试看”，不以重新启动/重装覆盖证据。
- 记录开始/结束时间、操作者、Windows build、测试机名、RC11 文件哈希、客户机类型和网络名称。敏感凭据、配对 token、Cookie 不写入记录。

## P8-R01 — 验收环境与安装前基线

验收机应为 Windows 11，未复制源码/venv、未另装 Python、没有旧 LanDrop 安装历史；Windows Defender Firewall 开启，并有真实 Private WLAN 或 Ethernet 和独立浏览器客户机。尽量避免旧 `LanDrop.exe` / Python TCP 8000 规则。


| 检查项                                                                                                             | 结果 / 证据 |
| -------------------------------------------------------------------------------------------------------------------- | ------------- |
| Windows 11 版本/build、设备名、账户权限                                                                            | 待填写 |
| 无 LanDrop 安装、无源码/venv/独立 Python                                                                           | 待填写 |
| WebView2 Runtime 状态及版本（如存在）                                                                              | 待填写 |
| Windows Firewall 各 Profile 已启用                                                                                 | 待填写 |
| 当前网络名称、适配器、InterfaceIndex、类别为 Private                                                               | 待填写 |
| 独立客户机型号/OS/浏览器；两端同一 LAN                                                                             | 待填写 |
| 安装根`%LOCALAPPDATA%\Programs\LanDrop` 不存在                                                                     | 待填写 |
| LanDrop Run、StartupApproved、Uninstall、快捷方式基线                                                              | 待填写 |
| TCP 8000 无监听；LanDrop / Setup / Uninstall 无进程                                                                | 待填写 |
| 既有 LanDrop/Python 防火墙相关规则清单                                                                             | 待填写 |
| `transaction.json`、staging/rollback、旧 uninstall TEMP 无残留                                                     | 待填写 |
| 在测试用户的 Shared、Received 和一个自定义收发目录放置唯一哨兵文件；记录路径、大小与 SHA-256（只用无敏感测试内容） | 待填写 |
| RC11 Setup 的 SHA-256 与冻结记录一致                                                                                | 待填写 |
| RC11 Uninstall 的 SHA-256 与冻结记录一致                                                                            | 待填写 |
| RC11 onedir ZIP 的 SHA-256 与冻结记录一致                                                                           | 待填写 |
| RC11 payload manifest 的 SHA-256 与冻结记录一致                                                                     | 待填写 |
| RC11 Setup build-info 的 SHA-256 与冻结记录一致                                                                     | 待填写 |
| A 0.5.9 Setup 已加入 RC11 包且大小、SHA-256 与 RC11 冻结记录一致（当前缺失，需补齐后方可执行周期二）               | 待填写 |

**R01 判定：** ☐ PASS ☐ FAIL。未通过则停止，不运行 Setup。

## P8-R02 — WebView2 与离线安装边界

### 验收机已有 WebView2（常规分支）

- ☐ 在不连接 WAN 的条件下运行冻结 RC11 Setup；不得要求联网才能完成本地安装。（本轮结果/证据：待填写。）
- ☐ Setup 正常显示；无 WebView2 Runtime 下载、安装器启动或其他隐式下载迹象。（本轮结果/证据：待填写。）
- ☐ 记录离线状态和安装结果。（本轮结果/证据：待填写。）

### 可选分支：在可恢复独立环境验证无 WebView2（非 Phase 8F 强制门槛）

- ☐ 不在主要日用机器上卸载/破坏 WebView2；只在可恢复的独立快照环境验证。
- ☐ 运行 Setup 后得到明确原生提示并阻断。
- ☐ 比较安装根、`%LOCALAPPDATA%\LanDrop`、Run、StartupApproved、Uninstall、快捷方式：无产品写入或半安装残留。
- ☐ Setup 不尝试联网下载 Runtime。
- ☐ 在该环境恢复/安装 WebView2 后重新运行 Setup，能够继续正常安装。

只有现成的可恢复独立环境时才执行此分支；不要求为此卸载日用设备的 Runtime 或专门构造缺失环境。未执行时可记 `N/A`，不阻止 Phase 8F 通过；已有 Runtime 的常规分支仍须按上方项目验收。

常规分支中本次成功安装即作为 R04 周期一的唯一首次安装；如执行无 WebView2 分支，则恢复 Runtime 后首次成功的 Setup 安装作为该次唯一首次安装。R04 不得再次运行 Setup 安装 RC11。

**R02 判定：** ☐ PASS ☐ FAIL；证据：待填写。

## P8-R03 — Windows 原生防火墙行为

在 Setup 前已有规则基线的前提下，启动 RC11 并首次启动 Private 服务：

- ☐ 记录 Windows 是否显示原生防火墙授权对话框；不把“必然弹窗”作为通过条件。（本轮结果/证据：待填写。）
- ☐ 若显示：只按用户意愿允许 Private；记录授权前后规则的名称、程序路径、方向、协议、端口、Profile、来源。（本轮结果/证据：待填写。）
- ☐ 若未显示/被拒绝/被策略抑制：确认 LanDrop 诊断能够说明不可达状态，并且设置入口只打开 Windows 防火墙设置；不创建/修改规则。（本轮结果/证据：待填写；若为 N/A 请说明原因。）
- ☐ 使用独立客户机验证服务访问；记录访问成功或被阻断及对应诊断。（本轮结果/证据：待填写。）
- ☐ 安装、运行、卸载前后对比规则清单，确认 LanDrop 自身没有主动创建、修改或删除 Windows 防火墙规则。（本轮结果/证据：待填写。）

注意：用户通过 Windows 原生授权可能促使 Windows 创建规则；这要与 LanDrop 直接写规则区分，并记录规则实际所有者/创建过程。LanDrop 卸载不得擅自删除该规则。

**R03 判定：** ☐ PASS ☐ FAIL；观察到的 Windows 行为与证据：待填写。

## P8-R04 — 完整发布生命周期

为兼顾“干净机首次安装”和“A→RC11 升级”，本表分成两个测试周期。周期一以 RC11 新装开始，完成首次安装/运行后做保留数据卸载。周期二必须从**没有 LanDrop 安装、没有旧 LanDrop 应用数据**的状态开始，再用冻结 A（0.5.9）安装并升级到 RC11，最后通过正式卸载选择删除应用数据。

目前 RC11 冻结包没有 A 基线文件，因此周期二保持待执行；不能用未随包复核的外部文件开始该周期。

周期一保留的数据不能直接带入旧版 A 基线，否则会把“旧版本读取新版本用户数据”这个额外兼容性场景混进升级验收。两个周期之间优先恢复已记录的干净 Windows VM 快照；若没有可恢复快照，则在确认保留数据成功后，重新安装 RC11 并通过 Windows 卸载入口选择删除应用数据，确认安装与应用数据均已清理，再开始周期二。不得手工删除应用数据目录或系统集成对象。

### 周期一：RC11 首次安装、运行和保留数据卸载

**安装与首次运行**

- ☐ 引用 R02 常规分支完成的冻结 RC11 首次安装；确认唯一安装输入为 `LanDrop-Setup-0.6.0-rc11.exe`，不得重复运行 Setup。
- ☐ 核对 `install.json` 版本/build、app `_internal`、`maintenance\Uninstall.exe`；无 `transaction.json`、staging、rollback。
- ☐ 核对开始菜单快捷方式、桌面快捷方式选择、HKCU Run、HKCU Uninstall、StartupApproved 实际状态。
- ☐ 安装后核对 `app\LanDrop.exe` SHA-256 与 RC11 onedir ZIP 内 `build-info.json` 记录一致；`maintenance\Uninstall.exe` SHA-256 与冻结 Uninstall 一致。
- ☐ 从开始菜单启动；确认 GUI、托盘和 Windows Toast 的应用身份显示为 LanDrop，程序路径为固定安装根。至少收到一次服务到期提醒，并验证点击“打开窗口”能激活现有 GUI；无需为验收逐个触发“重置计时”和“关闭服务”。
- ☐ 服务默认 OFF；显式启动后只绑定已验证 Private endpoint，TCP 8000 状态符合 UI。

**RC11 窗口激活与卸载提示专项**

- ☐ 用 RC11 登录启动路径确认窗口启动时保持隐藏、服务 OFF，不抢用户前台。
- ☐ 从托盘打开后点击 LanDrop 主窗口，确认可把窗口切到 Explorer 上方；隐藏后重复打开仍可鼠标激活。
- ☐ 在两个窗口都未最小化时互相点击切换，确认不要求用 Alt+Tab 或任务栏作为唯一恢复手段。
- ☐ 确认 LanDrop 没有被设为置顶窗口，Explorer 仍按 Windows Shell 默认方式打开。
- ☐ 普通开始菜单启动仍可通过鼠标激活，窗口样式切换只发生在启动后首次主动打开路径。
- ☐ 从正式卸载流程到达完成页，确认防火墙说明卡片和按钮布局正常；点击按钮能打开 Windows Defender 防火墙的“允许应用”页。
- ☐ 记录 Windows 原生授权创建的允许规则在卸载后仍保留；LanDrop 不自动删除规则，用户可在已打开页面检查或手动删除。

**功能与稳定性**

- ☐ 独立客户机完成至少一次上传和一次下载；检查内容/大小或 SHA-256。
- ☐ 配对前建立无敏感测试文件并计算 SHA-256；配对和上传/下载完成后，对接收端文件重算哈希并记录路径、大小、算法、前后值及相等结果。下表填写实测值：

| 传输方向 | 配对前源文件路径 / SHA-256 | 配对后目标文件路径 / SHA-256 | 大小 / 哈希相等 | 证据 |
| --- | --- | --- | --- | --- |
| 客户机 → Shared/Received | 待采集 | 待采集 | 待采集 | 待填写 |
| Shared → 客户机 | 待采集 | 待采集 | 待采集 | 待填写 |

- ☐ 验证 QR 一次性配对和可信浏览器访问（客户机条件具备时）；不在日志/诊断/配置中记录 token/Cookie。
- ☐ 验证可信客户机最多保留 3 台：连续建立 4 个独立可信浏览器身份后，第 4 台能够正常完成配对；最早的第 1 台被移除且旧凭据立即失效，第 2～4 台仍能访问。验证新记录服务端 `expires_at` 约为 `created_at + 5 天`；不通过修改系统时钟强行等待过期。
- ☐ 服务空闲期间观察至少 3 轮 15 秒网络类别检查，不应误停。
- ☐ 完成上传后再观察至少 3 轮 15 秒网络类别检查，不应误停；记录 UI/日志结果。
- ☐ 做正常双向传输的轻量资源边界检查；不做 DoS/压力攻击，不以填满 worker/磁盘为目的。
- ☐ 如客户机文件系统允许，上传 `NUL.tar.gz`，确认服务机保存为 `_NUL.tar.gz` 且内容一致；客户机无法创建该名称时记 `N/A`，由自动化覆盖补足。

**登录启动与网络边界**

- ☐ 注销并重新登录 Windows；LanDrop 自动进入托盘，传输服务仍 OFF。
- ☐ 若条件安全，切换/构造 Public 网络验证拒绝启动服务；恢复 Private 后复核。否则记 `N/A` 并说明原因。

**保留数据卸载**

- ☐ 从 Windows 设置卸载 LanDrop，选择保留配置、日志、可信客户机。
- ☐ 卸载完成后确认安装根、Run、精确 StartupApproved 派生项、Uninstall、快捷方式、LanDrop 进程、TCP 8000、transaction/staging/rollback 和 uninstall TEMP 按契约清理。
- ☐ 在卸载完成页点击“打开允许的应用”后核对 Windows 页面确实打开；记录该规则仍在、未被 LanDrop 自动删除。
- ☐ 确认 `%LOCALAPPDATA%\LanDrop` 中选择保留的应用数据存在。
- ☐ Shared、Received、自定义收发目录及哨兵文件仍存在，SHA-256 与安装前一致。
- ☐ 记录本轮卸载后的检查结果和配对/传输数据哈希：

| 对象 | 卸载前路径 / 状态 / SHA-256 | 卸载后路径 / 状态 / SHA-256 | 结果 / 证据 |
| --- | --- | --- | --- |
| 安装根与 app payload | 待填写 | 待填写 | 待填写 |
| HKCU Run、StartupApproved、Uninstall、快捷方式 | 待填写 | 待填写 | 待填写 |
| LanDrop/Uninstall 进程、TCP 8000、transaction/staging/rollback/TEMP | 待填写 | 待填写 | 待填写 |
| Shared、Received、自定义目录哨兵 | 待填写 | 待填写 | 待填写 |
| `%LOCALAPPDATA%\LanDrop`（按本轮选择保留） | 待填写 | 待填写 | 待填写 |
| Windows 防火墙允许规则 | 待填写 | 待填写 | 待填写 |

### 周期二：A→RC11 升级与删除应用数据卸载

- ☐ 周期二前置基线复核：无 LanDrop 安装、进程、端口、系统集成和旧应用数据；Windows Firewall 保持开启，网络为 Private。周期一由 Windows 原生授权形成的防火墙规则如仍存在，应记录并保留，不要求人为恢复到 R01 的首次授权前规则状态。
- ☐ 用 RC11 冻结目录内 `upgrade-baseline-A-0.5.9\LanDrop-Setup-A-0.5.9.exe` 安装 A；核对 `0.5.9` build 和完整 app/maintenance。
- ☐ 启动 A，服务保持 OFF；为检验升级不偷偷恢复自启动，可通过 Windows“启动应用”先禁用 LanDrop，并记录禁用状态。
- ☐ 使用同一个冻结 RC11 Setup 升级 A→RC11；禁止使用其他目录中的 Setup。
- ☐ 核对 B 的 install.json/build、app 与 Uninstall 版本一致、A-only payload 不残留、无 transaction/staging/rollback。
- ☐ 确认 Run/StartupApproved 的禁用状态未被升级重置；稳定快捷方式未被不当重建/改写。
- ☐ 启动 B，完成一次独立客户机上传及下载；服务/endpoint 正常。
- ☐ 从 Windows 设置卸载，选择删除配置、日志、可信客户机。
- ☐ 确认仅按白名单删除 `%LOCALAPPDATA%\LanDrop` 应用数据；Shared、Received、自定义收发目录永不进入删除计划，哨兵 SHA-256 不变。

### 最终系统恢复核对


| 对象                                        | 预期 / 实际结果                                  |
| --------------------------------------------- | -------------------------------------------------- |
| `%LOCALAPPDATA%\Programs\LanDrop`           | 完整卸载后不存在                                 |
| `%LOCALAPPDATA%\LanDrop`                    | 周期一保留；周期二按选择删除                     |
| HKCU Run`LanDrop`                           | 不存在                                           |
| StartupApproved\Run`LanDrop`                | 精确派生 value 已清除；其他 value 不变           |
| 开始菜单/桌面 LanDrop 快捷方式              | 不存在                                           |
| HKCU Uninstall`LanDrop`                     | 不存在                                           |
| LanDrop / Uninstall 进程、托盘              | 不存在                                           |
| TCP 8000                                    | 无 LanDrop 监听                                  |
| transaction/staging/rollback/uninstall TEMP | 无残留                                           |
| 防火墙规则                                  | 与 Windows 授权行为相符；LanDrop 未主动写/删规则 |
| Shared/Received/自定义目录哨兵              | 存在且 SHA-256 与基线一致                        |

**R04 判定：** ☐ PASS ☐ FAIL；证据/异常：

## 最终记录

- P8-R01：☐ PASS ☐ FAIL
- P8-R02：☐ PASS ☐ FAIL
- P8-R03：☐ PASS ☐ FAIL
- P8-R04：☐ PASS ☐ FAIL
- 未解释残留：☐ 无 ☐ 有（列明对象与现场位置：）
- Phase 8F：☐ PASS ☐ NOT PASS
- 验收日期/测试机/客户机：
- 证据目录（不得包含凭据/token）：

RC11 的 R01～R04 未完成，本表保留为历史记录；不得把其空白项目解释为通过，也不得将 RC11 文件或数据记录并入正式版 0.6.0。正式版实际验收仍按正式 Build ID 单独记录。
