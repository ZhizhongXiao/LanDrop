# 第八阶段详细计划：安装器、卸载器与分发

状态：**Phase 8A～8E 开发实现、自动化回归和本机真实安装生命周期开发验收通过；RC1、RC2 已被取代，RC3 已冻结；Phase 8F 干净 Windows 11 最终发布验收尚未完成（2026-09-26）**。

实施进度（2026-09-26）：安装契约、生命周期锁、权威状态、首次安装、事务升级/回滚、`pending_cleanup`、临时 Uninstall 迁出、绑定 request、精确系统对象移除及用户数据白名单清理均已落地。实机暴露的 onefile TEMP 自清理时序、`StartupApproved` 派生状态残留和临时卸载页本地资源定位问题均已完成针对性修复。当前完整自动化回归为 182 项且全部通过；Python 3.12 x64 + PyInstaller 6.22.3 生成的主程序 `--onedir`、Uninstall `--onefile --windowed` 和 Setup `--onefile --windowed` 均通过自检。本机已完整走通 A 首装与双向传输、A→B 事务升级、B 双向传输、保留数据卸载、重新安装和删除应用数据卸载；最终无程序根、系统集成、进程、TCP 8000 或 `uninstall-*` TEMP 残留，Shared/Received 文件保持大小与 SHA-256 不变。RC1、RC2 均未进入最终发布验收即被后续界面与行为收口取代；RC3 已从 `ede7d1a` 构建并冻结，Phase 8F 只测试 RC3。Phase 8 development acceptance 为 PASS；Phase 8F final release acceptance 仍为 NOT YET COMPLETE。

> 0.6.0-rc1 was superseded before final release acceptance by post-RC1 UI/behavior changes.

前置条件：第七阶段开发验收已通过并冻结为第八阶段基线。当前已有 Python 3.12 x64 + PyInstaller 6.22.3 `--onedir` 主程序构建、统一资源定位、单实例、WebView2 Runtime 检查、滚动日志、托盘/Toast、Private/Public 网络边界及 94 项自动化回归证据。第七阶段正式无 Python/无源码干净 Windows 11 验收因测试机暂不可用而延期，必须在第八阶段最终发布验收中一并补齐。

本阶段不得重新设计核心 HTTP 传输协议、配对模型、endpoint 冻结、生命周期或 Public 网络边界。重点只处理**安装生命周期与分发**。

## 目标

把第七阶段已稳定的 `--onedir` 主程序推进为当前用户范围的正式 Windows 安装版：用户只需获得一个单文件 `LanDrop-Setup.exe`，安装后通过开始菜单、可选桌面快捷方式、托盘或自启动进入稳定的 `app\LanDrop.exe`；新版安装能够事务升级并清理旧 payload；卸载能够从 Windows 设置/控制面板进入，并在不误删用户收发文件的前提下完整移除程序和由用户选择的数据。

本阶段固定顺序：

```text
冻结安装契约与目录
→ 安装状态 / 安装历史
→ Setup payload 与首次安装
→ HKCU 系统集成
→ 自启动 / AUMID / 快捷方式
→ 运行中阻断与托盘退出确认
→ 事务升级 / 回滚 / 旧 payload 清理
→ 临时卸载器 / 用户数据选择
→ 自动化与本机完整生命周期回归
→ 干净 Windows 11 最终发布验收
```

## 一、已冻结的核心决策

### P8-D01：构建与分发形态

正式方案固定为：

- 主程序：Python 3.12 x64 + PyInstaller 6.22.3 `--onedir`；
- Setup：Python + PyInstaller `--onefile --windowed`；
- Uninstall：Python + PyInstaller `--onefile --windowed`；
- Setup 单文件内部携带完整主程序 onedir payload、独立 `Uninstall.exe` 和 payload 构建清单；
- 第七阶段 `LanDrop.spec` 和既有 onedir 构建继续作为主程序正式 payload 基线，不为了安装器改成主程序 onefile；
- 是否另行提供免安装单 EXE Portable 版本不属于第八阶段硬门槛。

当前主路线不使用 Inno Setup、NSIS 或 MSI；已有 `ui/setup/`、`ui/uninstall/`、`ui/wizard/` 继续作为正式安装/卸载 UI 资源。

### P8-D02：当前用户固定安装布局

固定程序根：

```text
%LOCALAPPDATA%\Programs\LanDrop\
├─ app\
│  ├─ LanDrop.exe
│  └─ _internal\
├─ maintenance\
│  └─ Uninstall.exe
└─ metadata\
   ├─ install.json
   └─ transaction.json   # 仅事务进行中存在
```

固定数据根继续为：

```text
%LOCALAPPDATA%\LanDrop\
```

默认用户文件继续位于 Windows Known Folder Downloads 下：

```text
Downloads\LanDrop\Shared
Downloads\LanDrop\Received
```

用户可自定义共享/接收目录，但这些目录中的文件始终视为用户内容，不属于卸载器程序清理范围。

所有长期系统入口固定指向：

```text
%LOCALAPPDATA%\Programs\LanDrop\app\LanDrop.exe
```

不得把版本号目录、`.staging-*`、`.rollback-*` 或构建机路径写入开始菜单、自启动、`DisplayIcon` 等长期入口。

### P8-D03：权限与系统配置边界

正式安装采用当前用户范围，正常安装、升级和卸载不以管理员权限为前提。

Setup、LanDrop、Uninstall 均继续遵循：

- 不创建、修改、删除 Windows 防火墙规则；
- 不自动更改 Private/Public；
- 不修改代理、VPN、静态 IP；
- 不通过关闭防火墙解决可达性；
- 只检测、解释并提供 Windows 官方设置入口。

首次 `LanDrop.exe` 监听 TCP 8000 时，Windows 是否弹出原生授权提示取决于实际系统环境和策略；程序不得依赖“一定会弹”。

### P8-D04：WebView2 前置条件

WebView2 Runtime 不随 LanDrop 安装包携带，不由 Setup 在线下载，也不要求 Setup 联网。

要求：

- `README-release` 明确声明 Microsoft Edge WebView2 Runtime 为 GUI 前置条件；
- Setup、LanDrop、Uninstall 都在导入/初始化 pywebview 之前做本地 Runtime 检查；
- 缺失时用原生 `MessageBoxW` 明确提示并安全退出；
- Setup 必须在任何产品写入前阻断；
- 不新增 Uninstall 的无 WebView2 降级卸载 UI，接受三个 GUI 可执行文件共同依赖 WebView2 的产品边界；
- Uninstall 的原生提示必须说明“未检测到 Microsoft Edge WebView2 Runtime；请重新安装或修复 WebView2 Runtime 后再次卸载 LanDrop”，不得无反馈失败。

### P8-D05：运行中的 LanDrop 不由安装器强制关闭

不新增升级/卸载 IPC。

Setup 或 Uninstall 发现正式 `app\LanDrop.exe` 仍在运行时：

```text
停止当前安装/升级/卸载提交
→ 提示用户从系统托盘退出 LanDrop
→ 用户处理后重新检测
```

禁止：

- 自动 Kill；
- 把 `WM_CLOSE` 当作退出；
- 在 LanDrop 仍运行时覆盖 `app`；
- 安装器自行推断并中断活动传输。

LanDrop 自身“托盘 → 退出”若检测到活动上传/下载，必须明确提示退出会中断传输；只有用户确认后才调用既有 `request_exit()`。取消则保持服务和传输不变。

### P8-D06：事务级版本隔离，不长期保留多版本

正常状态只有一个正式 `app`。版本隔离只存在于升级事务：

```text
.staging-<new-version>-<transaction>
.rollback-<old-version>-<transaction>
```

禁止长期维护：

```text
versions\0.7.0\
versions\0.8.0\
versions\0.9.0\
```

升级成功后旧 payload 必须清理。版本留痕使用 JSON/JSONL，不用旧程序目录或空版本目录承担历史记录。

### P8-D07：安装状态与安装历史双轨制

权威当前状态：

```text
%LOCALAPPDATA%\Programs\LanDrop\metadata\install.json
```

可读历史：

```text
%LOCALAPPDATA%\LanDrop\logs\install-history.jsonl
```

`install.json` 只表示已经完成最终提交的当前有效安装；未完成事务不得提前写入或覆盖正式 `install.json`。首次安装在最终提交前不存在正式 `install.json`，升级期间旧版 `install.json` 保持不动。安装/升级中间状态另由 `metadata\transaction.json` 记录；`install-history.jsonl` 只做追加式审计和诊断，不得反向驱动安装事务。

建议事件至少覆盖：

```text
installed
upgrade_started
upgrade_committed
rollback_started
rollback_completed
old_payload_removed
cleanup_pending
cleanup_completed
uninstall_started
uninstall_completed
```

其中卸载完成事件按日志保留选择处理：保留日志时持久记录 `uninstall_started/uninstall_completed`；删除日志时不承诺永久保存 `uninstall_completed`，本次结果由临时卸载日志和完成页表达。

### P8-D08：独立事务记录与安装生命周期互斥

安装生命周期必须同时使用：

- 独立、原子写入的 `metadata\transaction.json`，至少记录事务 ID、类型、源/目标版本、时间和 `prepared → app_switched → integration_written → integration_verified` 阶段；
- 独立于主程序单实例锁的 per-user 安装生命周期锁。

Setup 和临时 Uninstall 在整个目录/注册表修改事务期间独占该锁，并在取得后再次确认正式 LanDrop 未运行。LanDrop 普通启动从进程入口开始取得该锁；若正在安装、升级或卸载，则以原生提示说明“LanDrop 正在维护，请稍后重试”并退出。若取得成功，普通启动必须先检查安装根是否存在有效未完成或无法安全解释的 `transaction.json`；存在时提示“检测到未完成的安装/升级，请重新运行 LanDrop Setup 完成恢复”并退出，不得启动 GUI、托盘或服务。只有没有未完成事务时，才继续建立主程序单实例所有权和可供 Setup 检测的正式进程身份，然后释放生命周期锁。LanDrop 执行 `pending_cleanup` 前同样必须重新取得该锁。仅检查进程是否存在不能替代该锁，因为双方的检查与 app 切换/应用启动之间都存在 TOCTOU 竞态。

`LanDrop.exe --self-check` 是严格限定的无副作用维护验证模式，也是生命周期锁入口规则的唯一例外。它不进入普通 LanDrop 启动路径，不取得主程序单实例，不启动 GUI、托盘或服务，不执行 `pending_cleanup`，也不因生命周期锁已经由父 Setup 持有而失败。Setup 必须持续持有生命周期锁执行 self-check，禁止为了验证临时释放锁。

`install.json` 的最终发布是事务 commit 点。若在此之前中断，下次 Setup 根据 `transaction.json` 和磁盘事实恢复或回滚，不得把尚未完成系统集成的新版本视为当前有效安装。提交后若仅旧 payload 清理失败，可以再原子更新 `install.json.pending_cleanup`。

## 二、安装基础设施

### P8-S01：定义安装契约与路径模块

建立独立安装生命周期模块，集中定义：

- 产品 ID / DisplayName / Publisher；
- AUMID `LanDrop.Desktop`；
- 安装根、`app`、`maintenance`、`metadata` 路径；
- `%LOCALAPPDATA%\LanDrop` 数据根；
- 开始菜单与桌面快捷方式位置；
- HKCU Run 与 Uninstall 注册表位置；
- per-user 安装生命周期锁名称与取得/释放规则；
- 普通启动事务残留阻断与 `--self-check` 维护模式入口分流；
- `transaction.json` schema、阶段和恢复判定；
- 系统集成对象清单及其所有权；
- `.staging-*` / `.rollback-*` / `%TEMP%\LanDrop\uninstall-*` 严格命名规则；
- 所有递归删除允许的产品根边界。

任何删除、移动或回滚逻辑不得自行拼接未验证的任意路径。

### P8-S02：payload 清单和完整性验证

Setup 构建时生成 payload manifest，至少包含：

- 相对路径；
- 文件大小；
- SHA-256；
- 主程序版本/构建标识；
- 清单格式版本。

安装前流程：

```text
Setup 内嵌 payload
→ 解到 .staging
→ 检查所有路径仍在 staging 根内
→ 文件数 / 大小 / SHA-256 全部匹配
→ 才允许进入提交
```

缺文件、多文件、哈希错误、路径越界均必须阻止正式安装。

### P8-S03：实现 `install.json` 与 `transaction.json`

建议至少记录：

- schema version；
- product/version/build id；
- `installed_at`；
- `updated_at`；
- 固定安装根；
- 当前有效 app 相对位置；
- `pending_cleanup` 列表（如有）。

写入必须使用临时文件 + flush/fsync + 原子替换，避免断电/强杀留下半写 JSON。

损坏或与固定安装根不一致时，Setup 不得盲目删除/覆盖未知目录，应安全失败并记录诊断。

`transaction.json` 独立记录未完成事务，至少包含：

- schema、transaction id、类型；
- 源版本/构建、目标版本/构建；
- 创建时间和更新时间；
- 当前阶段：`prepared`、`app_switched`、`integration_written`、`integration_verified`；
- staging/rollback 受控相对路径；
- 恢复所需的系统集成快照。

该文件同样原子写入，但不是当前有效安装的权威状态；成功提交后必须删除。

### P8-S04：实现 `install-history.jsonl`

写入 `%LOCALAPPDATA%\LanDrop\logs\install-history.jsonl`，沿用现有日志目录。

要求：

- 一行一个 JSON 对象；
- 每条有时间、事件、版本和结果；
- 升级事件记录 from/to；
- cleanup 记录目标版本/事务 ID，不记录敏感凭据；
- 写日志失败不能改变权威安装状态，但必须在 `application.log` 或 Setup 自身日志中留下可见诊断。
- 选择删除日志卸载时，允许 `uninstall_completed` 只存在于临时卸载日志和完成页，不承诺写入随后将被删除的历史文件。

## 三、首次安装

### P8-S05：构建单文件 Setup

建立独立 Setup spec/build script，要求：

- `LanDrop-Setup.exe` 单文件启动；
- 不依赖源码、venv 或独立 Python；
- 包含完整 onedir 主程序 payload、`Uninstall.exe`、manifest 和安装 UI；
- 从非构建 cwd、中文/空格路径运行正常；
- WebView2 缺失时任何产品写入前退出；
- Setup 自身异常写入可诊断日志，不依赖控制台。

### P8-S06：首次安装事务

无既有安装时：

```text
取得安装生命周期锁
→ 确认固定安装根状态可接受
→ 创建受控 metadata 目录和 transaction.json
→ 创建 staging
→ 解出并验证 payload
→ 建立 app / maintenance
→ transaction.json = app_switched
→ 从正式 app 路径执行 LanDrop.exe --self-check
→ 建立 Windows 当前用户系统集成
→ transaction.json = integration_written
→ 逐项读回验证
→ transaction.json = integration_verified
→ 原子发布 install.json
→ 写 installed 历史
→ 删除 transaction.json
→ 清理 staging
```

任一步失败应尽量撤销本次新建的程序文件和系统项，不得留下“Windows 显示已安装但 app 不存在”的半安装状态。

## 四、Windows 当前用户系统集成

### P8-S07：开始菜单与桌面快捷方式

要求：

- 当前用户开始菜单快捷方式必建；
- 桌面快捷方式可选，默认不勾选；
- 快捷方式始终指向稳定 `app\LanDrop.exe`；
- 图标来源稳定；
- 快捷方式身份与 `LanDrop.Desktop` AUMID 保持一致；
- 升级时不因版本号变化重建为新的目标路径。

### P8-S08：登录启动

注册：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

命令目标：

```text
"...\app\LanDrop.exe" --startup
```

验收语义：

- Windows 登录后只启动 LanDrop 桌面/托盘；
- 服务默认 OFF；
- TCP 8000 不监听；
- 在任务管理器“启动应用”/Windows 启动应用页可见并可禁用；
- 用户在 Windows 中禁用后，LanDrop 不得自行重新启用；
- 升级默认保留当前自启动状态，不写或重置 Windows `StartupApproved`；
- Setup 首次安装不创建或写入 `StartupApproved`；完整卸载时只清除 `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run` 中精确名为 `LanDrop`、类型为 `REG_BINARY` 的 Windows 派生状态。该 value 不存在视为正常；类型异常时保留并报告残留；
- 稳定路径没有变化时不重写 Run。若用户已经禁用或主动删除 Run，Setup 不得偷偷恢复，除非用户在 Setup 中明确重新选择启用；
- 若用户从应用内提供“打开启动应用设置”，只打开系统页面，不代改系统状态。

### P8-S09：Windows“已安装的应用”与控制面板登记

写入当前用户：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LanDrop
```

至少包括：

- `DisplayName`；
- `DisplayVersion`；
- `Publisher`；
- `DisplayIcon`；
- `InstallLocation`；
- `UninstallString`；
- `EstimatedSize`；
- `NoModify = 1`；
- `NoRepair = 1`。

必须读回验证，并在实机确认：

- Windows 设置 → 应用 → 已安装的应用可见；
- 控制面板 → 程序和功能可见；
- 两处卸载入口都调用安装目录中的同一个 `maintenance\Uninstall.exe`。

### P8-S10：AUMID / Toast 身份回归

编码前先形成可机读或集中定义的“系统集成对象清单”。当前确定的对象为：

- 当前用户开始菜单 `LanDrop.lnk`；
- 可选的当前用户桌面 `LanDrop.lnk`；
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中 LanDrop 自有 value；
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LanDrop`；
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run` 中精确名为 `LanDrop` 的 `REG_BINARY` value——仅卸载清理的 Windows 管理派生状态，Setup 不创建，升级不修改。

`SetCurrentProcessExplicitAppUserModelID` 属于运行时调用，不是卸载对象。若最终 Windows-Toasts 实现还需要快捷方式 Property Store 属性、注册表或 COM 激活登记，必须以实际代码创建的精确对象为准追加到清单，并分别定义创建、读回和删除；禁止保留泛化的“通知身份相关安装项”让卸载器猜测。

安装后重新验证：

- 主程序启动时显式 AUMID 正确；
- 开始菜单快捷方式身份与主进程一致；
- Windows-Toasts 仍显示为 LanDrop；
- 即将到期、自动关闭、点击正文唤起 GUI 等既有语义不变；
- 升级后通知身份不因为版本变化产生第二个产品身份。

## 五、防火墙与 WebView2 发布边界

### P8-S11：防火墙写入零实现验证

静态和实机都要证明 Setup / LanDrop / Uninstall 没有：

- `New-NetFirewallRule`；
- `Set-NetFirewallRule`；
- `Remove-NetFirewallRule`；
- `netsh advfirewall` 写入；
- 其他管理员提权后改规则的替代路径。

只允许既有诊断读取。

### P8-S12：WebView2 前置检查与发布说明

`README-release` 至少说明：

- 支持 Windows 11；
- GUI 需要 Microsoft Edge WebView2 Runtime；
- 正常 Windows 11 通常已具备；
- LanDrop 安装包不附带、不下载 Runtime；
- 缺失时需要用户自行安装后重试；
- Uninstall 无法建立 pywebview UI 时会先用原生 `MessageBoxW` 提示重新安装或修复 WebView2 Runtime，然后再次卸载 LanDrop。

Setup 无网络环境仍应能够完成全部本地安装工作，只要系统已有 WebView2。

## 六、运行中阻断和安全退出确认

### P8-S13：Setup / Uninstall 进程检测

只按正式安装路径识别运行中的 `app\LanDrop.exe`，避免误把开发目录或另一个同名 EXE 当作正式实例。

进程检测只是用户提示与状态核对，不是并发安全边界。Setup/Uninstall 在进入任何目录或注册表修改前必须独占 per-user 安装生命周期锁，并持续持有至事务完成或回滚；取得锁后还要再次检查正式 LanDrop 进程。LanDrop 从进程入口取得同一把锁，维护进行中则提示后退出；无维护时持有至主程序单实例所有权和正式进程身份已经建立，再释放该锁。

普通 LanDrop 在锁取得后、创建主程序单实例及任何 GUI/托盘/服务对象之前检查 `transaction.json`。文件存在、损坏或无法与当前安装状态安全对应时均按“未完成维护”失败关闭，只允许用户重新运行 Setup 恢复。`--self-check` 在参数解析后直接进入独立维护验证分支，不执行上述普通启动流程，但仍必须保持无副作用。

检测到运行时：

- 显示明确提示；
- 不进入文件替换或删除；
- 提供“重新检测/重试”；
- 用户取消则安全退出 Setup/Uninstall。

### P8-S14：托盘退出活动传输确认

修改 LanDrop 自身托盘“退出”路径：

- 无活动传输：沿既有 `request_exit()` 正常退出；
- 有活动上传/下载：明确显示活动任务数量或至少说明存在活动传输；
- 用户确认：按既有停止/清理语义中断任务并退出；
- 用户取消：保持当前 session 和传输，不改变倒计时或服务状态。

该确认只用于**整个应用退出**；普通“停止服务”仍沿现有语义处理，不重新设计生命周期。

## 七、事务升级与旧 payload 清理

### P8-S15：升级前检查

Setup 识别既有 `install.json` 后：

- 验证安装根等于固定产品路径；
- 验证当前版本和 app 基本完整；
- 拒绝在无法信任的安装记录上盲目覆盖；
- 不支持自动降级，除非未来另行明确设计；
- 同版本且 build id 相同才视为“已经安装”；同版本但 build id 不同应拒绝覆盖，并明确提示不支持 Repair/Reinstall；
- 发现未完成 staging/rollback 时先进入恢复/清理判断，不能直接开始下一次升级；
- LanDrop 正在运行时阻止升级。

### P8-S16：升级提交

成功路径：

```text
取得安装生命周期锁
→ 创建 transaction.json（prepared）
→ 新 payload → .staging-<new>-<tx>
→ 完整校验
→ 确认 LanDrop 未运行
→ current app + maintenance → .rollback-<old>-<tx>
→ staging 中的 app + maintenance → 正式路径
→ transaction.json = app_switched
→ 从正式 app 路径执行 LanDrop.exe --self-check
→ 更新 DisplayVersion / EstimatedSize 等必要登记（保持用户现有 Run/StartupApproved 状态）
→ transaction.json = integration_written
→ 读回 app 和系统集成
→ transaction.json = integration_verified
→ 原子发布新的 install.json
→ 写 upgrade_committed / installed 历史
→ 删除 staging，并尝试删除 rollback
→ rollback 已不存在，或已原子登记到 install.json.pending_cleanup
→ 最后删除 transaction.json
→ 写 old_payload_removed
```

稳定系统入口仍指向 `app\LanDrop.exe`，不因版本变化修改目标位置。`maintenance\Uninstall.exe` 属于随版本升级的正式 payload，必须与 `app` 在同一事务中切换；commit 前失败时两者必须共同恢复到 A，不允许形成“app B + Uninstall A”。

### P8-S17：升级失败回滚

至少对以下失败点做可恢复设计：

- staging 校验失败；
- app / maintenance 切换失败；
- 新版基础自检失败；
- 卸载登记/快捷方式读回失败；
- `install.json` 最终原子发布失败。

只要旧 `.rollback-*` 仍完整，就应恢复旧 `app` 和可恢复的系统登记，并写入 rollback 历史。失败后不得同时留下两个“都看似正式”的 app。

### P8-S18：`pending_cleanup`

升级已经成功提交，但删除旧 `.rollback-*` 因文件锁等原因失败时：

- 不把旧目录视为可运行版本；
- `install.json` 写入 `pending_cleanup`；
- `install-history.jsonl` 写 `cleanup_pending`；
- Setup 下一次启动时重试；
- LanDrop 在取得主程序单实例所有权且另行取得安装生命周期锁后也可做低风险重试；
- 成功后删除记录并写 `cleanup_completed`。

`transaction.json` 到 `install.json.pending_cleanup` 的所有权交接必须无空窗：只有 `.rollback-*` 已经不存在，或其路径已成功原子写入并读回确认在 `pending_cleanup` 中，才允许删除 `transaction.json`。如果 pending 写入失败，事务文件必须保留，供下次 Setup 按“B 已提交、旧 payload 待收尾”恢复，不能产生无人解释的 rollback 目录。

安全规则：

- 只处理固定安装根的直接受控子目录；
- 目录名必须严格匹配自身事务格式；
- 拒绝 symlink、junction、reparse point；
- 禁止跟随链接；
- 禁止删除 `app`、`maintenance`、`metadata` 或安装根以外路径。

## 八、卸载生命周期

### P8-S19：单文件 Uninstall 构建

`maintenance\Uninstall.exe` 是 onefile、windowed，并使用既有 `ui/uninstall/` / `ui/wizard/`。

启动后先：

- 检查 WebView2；
- 验证自己位于固定安装结构；
- 读取 `install.json`；
- 检查 LanDrop 是否运行；
- 展示程序删除与用户数据选择。

### P8-S20：卸载器迁出安装目录

正式删除前：

```text
maintenance\Uninstall.exe
→ 复制到 %TEMP%\LanDrop\uninstall-<random-id>\Uninstall.exe
→ 保存规范化 request JSON
→ 通过命令行独立传入 nonce + expected_request_sha256
→ 启动临时副本并传入原卸载器 PID
→ 原卸载器退出
```

request 至少包含：创建时间、过期时间、原卸载器 PID、固定安装根、当前版本/build id、用户数据选择和源卸载器 SHA-256。请求内容和期望哈希不得同时只依赖同一个可修改 JSON；nonce 与 `expected_request_sha256` 必须通过启动参数独立交接。

临时副本必须确认：

- 自身确实位于预期 `%TEMP%\LanDrop\uninstall-*`；
- 请求的安装根等于固定 `%LOCALAPPDATA%\Programs\LanDrop`；
- 原安装目录卸载器已经退出；
- request 未过期，nonce 与 expected SHA-256 匹配；
- 临时副本自身 SHA-256 与源卸载器哈希一致；
- 已独占 per-user 安装生命周期锁；
- 取得锁后重新读取当前权威 `install.json`，确认 product id、version/build id 和固定安装根与 request 完全一致；若安装状态已变化、`transaction.json` 存在或状态无法安全解释，则拒绝旧 request，并要求用户从当前 Windows 卸载入口重新启动 Uninstall；
- 删除范围没有越界。

该绑定主要防止陈旧请求、路径错配和异常交接，无需升级成抵御恶意本机用户的复杂认证协议。

### P8-S21：卸载系统项和程序根

建议顺序：

```text
1. 删除 HKCU Run 自启动项
2. 删除 StartupApproved\Run 中精确的 LanDrop REG_BINARY 派生状态；不存在视为正常，类型异常则保留并报告残留
3. 删除开始菜单快捷方式
4. 删除桌面快捷方式（如存在）
5. 按系统集成对象清单删除 LanDrop 实际创建且读回匹配的 Toast/AUMID 附加对象（如最终实现确有这些对象）
6. 删除 HKCU Uninstall 登记
7. 删除整个 %LOCALAPPDATA%\Programs\LanDrop
8. 按用户选择处理 %LOCALAPPDATA%\LanDrop 数据
9. 若日志保留，持久记录 uninstall_completed；若日志删除，仅由临时卸载日志和完成页表达结果
10. 启动系统现成的无窗口自清理步骤
11. 临时 Uninstall 退出后删除 %TEMP%\LanDrop\uninstall-* 目录
```

若某一步失败，不得虚报“已完整卸载”；应明确残留项并允许用户重试或查看日志。

程序根、事务目录、临时卸载目录和用户数据清理统一使用同一套边界：canonical path 必须位于预期产品根；不得跟随 symlink、junction 或 reparse point；遇到未知 reparse object 必须终止对应递归清理并报告残留。

临时目录自清理器必须在待删除目录之外启动并使用外部工作目录（例如 `%TEMP%`），同时等待临时 Uninstall 的 Python 子进程和 PyInstaller onefile 父 bootloader 进程全部退出。删除采用约 10～15 秒的短间隔有限重试，并在每次尝试后确认目标目录是否仍存在；只有目标确实消失时清理器才返回成功。结果页在进程仍存活时只能显示“等待最终清理”，不得把成功调度等同于完整清理；最终重试仍失败时必须给出原生警告，不得静默吞掉。目标必须仍通过严格 `%TEMP%\LanDrop\uninstall-*` 边界验证，禁止删除该根以外对象。

### P8-S22：用户数据清理

卸载 UI 至少让用户明确选择是否删除：

- 配置；
- 日志（包含 `application.log`、`sessions.jsonl`、`install-history.jsonl`）；
- 可信客户机数据。

清理使用明确白名单；未知数据在没有选择整根删除时保留。每个白名单对象仍需通过 canonical path 和 reparse object 检查；不得因为路径位于 `%LOCALAPPDATA%\LanDrop` 字符串前缀下就直接递归删除。

永远不得删除：

- `Downloads\LanDrop\Shared`；
- `Downloads\LanDrop\Received`；
- 用户后来选择的其他共享目录；
- 用户后来选择的其他接收目录；
- 这些目录中的任何正常用户文件。

P8-S19～S22 实现结果（2026-09-25）：正式 `Uninstall.exe` 已替换第八阶段早期安全占位入口。安装目录进程生成规范化 request 和独立命令行 nonce/expected hash，复制自身到严格命名的 `%TEMP%\LanDrop\uninstall-*`；临时副本等待原 PyInstaller 父/子进程完全退出，取得生命周期锁后复核当前 `install.json`、版本/build、事务状态和正式进程，再按五对象清单移除仍精确匹配的系统入口及 cleanup-only 派生状态。程序根删除和三类用户数据清理均先完成 canonical/reparse 预检；配置和可信客户机使用精确文件白名单，日志使用精确名称白名单，未知数据保留，所有收发目录始终不参与清理。实机发现的一次性清理调度不足已修正为外部 cwd、等待 onefile 父/子进程、有限重试并确认目录消失。

## 九、自动化与本机构建回归

### P8-T01：安装契约单元测试

至少覆盖：

- 固定路径解析；
- 安装根越界拒绝；
- staging/rollback/uninstall-temp 名称合法性；
- reparse point / symlink 拒绝；
- registry value 生成；
- Run 命令参数和引号；
- 安装生命周期锁的互斥、超时和异常释放；
- LanDrop 在维护锁占用期间拒绝启动；
- 普通启动发现有效、损坏或无法安全解释的 `transaction.json` 时拒绝运行；
- `--self-check` 在 Setup 持有生命周期锁时仍可成功，且不创建单实例、GUI、托盘、服务或 pending cleanup 副作用；
- 系统集成对象清单只包含 LanDrop 明确拥有的对象；
- 卸载清理白名单；
- 用户收发目录永不进入删除计划。

### P8-T02：manifest / 状态文件测试

至少覆盖：

- manifest 缺文件/多文件/哈希错误；
- `install.json` 原子写入；
- 首次安装提交前不存在正式 `install.json`；
- 升级最终提交前旧 `install.json` 保持不变；
- `transaction.json` 各阶段原子推进、中断恢复和成功删除；
- 未完成事务存在时普通 LanDrop 启动被阻止；
- schema/version 不兼容；
- 损坏 JSON；
- `install-history.jsonl` 追加；
- 日志写失败不改变权威状态；
- `pending_cleanup` 写入、恢复和完成。

### P8-T03：Setup/Uninstall 打包自检

对两个 onefile 构建验证：

- 无 Python/venv 依赖；
- 中文/空格路径；
- 非构建 cwd；
- UI 资源完整；
- WebView2 本地检测；
- build-info / SHA-256 与实际文件一致；
- 无控制台依赖；
- Setup 持有生命周期锁时，从正式 app 路径执行 `LanDrop.exe --self-check` 仍成功且无运行期副作用。

### P8-T04：本机首次安装

从未安装状态使用单个 `LanDrop-Setup.exe`：

- 安装到固定 LocalAppData 路径；
- app onedir 完整；
- maintenance/uninstall 完整；
- metadata 正常；
- 系统集成读回一致；
- Setup 退出后无 staging 残留；
- LanDrop 从开始菜单正常启动。

### P8-T05：安装后主程序回归

至少覆盖：

- 默认服务 OFF；
- Private 服务启动；
- QR/8 位码；
- Android/Windows 浏览器下载；
- 原始流上传；
- Range；
- 生命周期/重置；
- 托盘隐藏/恢复；
- Toast；
- 单实例；
- Public 拒绝；
- endpoint 变化停服；
- 日志/诊断入口；
- 配置和可信客户机数据仍位于 `%LOCALAPPDATA%\LanDrop`。

### P8-T06：登录启动与任务管理器控制

验证：

1. 安装后登录启动项存在；
2. Windows 重新登录后 LanDrop 进入托盘；
3. 服务保持 OFF、TCP 8000 不监听；
4. 任务管理器/Windows 启动应用页可以禁用 LanDrop；
5. 再登录后 LanDrop 不自启；
6. 手动启动 LanDrop 不重新启用被用户关闭的自启动状态；
7. 升级不写或重置 `StartupApproved`，被禁用状态保持不变；
8. 用户主动删除 Run 后，普通升级不偷偷恢复；只有 Setup 中明确重新选择启用才重建。

### P8-T07：运行中升级/卸载阻断

分别在以下状态启动 Setup/Uninstall：

- LanDrop 空闲运行；
- 服务运行但无传输；
- 有活动上传；
- 有活动下载；
- GUI 隐藏到托盘。

都必须阻止覆盖/删除并要求用户从托盘退出。活动传输时从托盘退出必须出现确认；取消后 Setup/Uninstall 仍不得继续。

另做 TOCTOU 回归：Setup 完成进程检查并取得维护锁后，立即尝试从快捷方式启动 LanDrop；新进程必须提示维护进行中并退出，不能进入托盘或读取正在切换的 app。

### P8-T08：A → B 正常升级

准备两个可识别构建 A/B：

- A 安装后建立配置、可信客户机和日志；
- 安全退出 A；
- 使用 B Setup 升级；
- `app` 仅包含 B；
- 系统入口路径完全不变；
- `DisplayVersion` 更新；
- 正式 app 路径的 `--self-check` 在 `install.json` 提交前通过；
- `install.json` 只在 app 与系统集成验证完成后切换到 B；
- `transaction.json` 提交后不存在；
- 配置/可信客户机/日志保留；
- `.staging-*` / `.rollback-*` 最终均不存在；
- `install-history.jsonl` 顺序合理；
- B 核心传输回归通过。

### P8-T09：升级失败与回滚

至少人为制造：

- payload 校验失败；
- 提交中断；
- 系统登记写入或读回失败；
- 新版基础自检失败。

分别在 `prepared`、`app_switched`、`integration_written`、`integration_verified` 阶段强制结束 Setup。每个阶段都先从开始菜单或正式路径尝试启动 LanDrop：普通启动必须检测事务残留并提示重新运行 Setup，不得出现 GUI、托盘、服务或 TCP 8000 监听。随后重新运行 Setup，确认它能根据事务记录恢复或回滚；最终 commit 前权威 `install.json` 始终仍指向旧 A。确认旧 A 可恢复运行，系统入口仍可用，不存在半新半旧 `_internal`。

### P8-T10：旧 payload 删除失败 / pending cleanup

人为占用 rollback 中一个文件，验证：

- 新版已正确提交；
- 旧目录无法删除时记录 pending；
- 不把旧目录当作第二个版本；
- 释放文件锁后，下一次 Setup 或 LanDrop 启动能清理；
- 多次升级不会累积历史 payload。

### P8-T11：卸载——保留用户数据

验证：

- 从 Windows 设置和控制面板两个入口都能启动卸载器；
- Uninstall 迁到 `%TEMP%` 后原安装目录可完整删除；
- request nonce、独立 expected SHA-256、过期时间、原 PID 和临时副本自身哈希均通过；篡改/陈旧 request 被拒绝；
- 原卸载确认页保持打开期间完成 A→B 升级后，旧临时卸载 request 即使随后取得生命周期锁，也会因当前 `install.json` 的 version/build id 不匹配而被拒绝；从当前 Windows 卸载入口重新启动后方可继续；
- Run、精确 `StartupApproved\Run\LanDrop` 派生状态、快捷方式和 Uninstall 登记消失，其他 `StartupApproved` value 不变；
- `%LOCALAPPDATA%\LanDrop` 按选择保留；
- Shared/Received 与自定义用户目录完整；
- 清理器 cwd 不位于目标目录内，等待 onefile 父/子进程、有限重试并确认临时卸载目录最终消失；不得越出严格的卸载临时根。

### P8-T12：卸载——删除用户数据

重新安装后选择删除配置/日志/可信客户机：

- 对应 LocalAppData 产品数据被按白名单清理；
- Shared/Received 和自定义收发目录仍保留；
- 程序根和数据根都不跟随 symlink/junction/reparse point 删除外部目录，未知 reparse object 会停止并报告残留；
- 选择删除日志时不承诺持久保存 `uninstall_completed`，但临时日志和完成页准确表达本次结果；
- 系统项和程序根无残留。

### P8-T13：重复生命周期

建议至少执行：

```text
安装 → 卸载
安装 → A→B 升级 → 卸载
安装 → 异常中断 Setup → 恢复/重试
安装 → 升级 rollback → 再正常升级 → 卸载
```

检查：

- 注册表无重复；
- 快捷方式无重复；
- Run 项单一；
- 无长期 staging/rollback；
- 无残留卸载 temp；
- 程序根可重新安装。

### P8-T14：Phase 8E 本机真实安装生命周期开发验收

2026-09-26 已在真实 `%LOCALAPPDATA%\Programs\LanDrop`、真实 HKCU、开始菜单、启动项和 Windows 卸载入口完成：

```text
A 0.5.9 首次安装
→ A 启动与双向传输
→ Windows 禁用自启动
→ B 0.6.0 事务升级
→ B 启动与双向传输
→ 保留应用数据卸载
→ 重新安装 B
→ C 0.6.1 卸载修复构建事务升级
→ 删除配置、日志和可信客户机卸载
```

实测结论：

- A/B 的 `install.json`、payload、maintenance、Run、StartupApproved、快捷方式和 HKCU Uninstall 在各断点与预期一致；
- 升级未重置 Windows 已禁用的自启动状态，未出现 staging/rollback/pending 残留或新旧 onedir 混合；
- 保留数据卸载后配置、日志和可信客户机仍在；删除数据卸载后这三类白名单数据均不存在；
- 两轮卸载均未删除 Shared/Received，最终 7 个验收文件大小与 SHA-256 一致；
- 最终安装根、Run、精确 StartupApproved、HKCU Uninstall、快捷方式、进程、TCP 8000 和 `uninstall-*` TEMP 均无残留；
- 实机发现并修复 onefile 父/子进程清理时序、StartupApproved 派生状态和 `file:` URL 查询串导致的临时页“找不到文件”；修复后临时页正常显示“执行清理”并完成自清理；
- 运行期网络类别热修复采用冻结 InterfaceIndex 单接口查询、4 秒上限及二次确认；idle 与上传后多轮检查未再发生误停。

P8-T04～T13 收口记录：

| 测试项 | 开发验收结果 | 证据与 Phase 8F 边界 |
| --- | --- | --- |
| P8-T04 本机首次安装 | PASS | 真实 LocalAppData 安装根、onedir app、onefile Uninstall、metadata、Run、开始菜单和 HKCU Uninstall 均核对；无 staging/transaction 残留 |
| P8-T05 安装后主程序回归 | PASS | A、B 均完成 GUI/托盘与真实双向传输；QR、Private/Public、诊断等沿用第七阶段冻结回归，干净机再做最小发布链路 |
| P8-T06 登录启动与任务管理器控制 | PASS（开发） | Windows 启动应用页禁用成功，A→B 后 StartupApproved 原值保持；真实注销/重登后服务 OFF 留待 P8-R04 |
| P8-T07 运行中升级/卸载阻断 | PASS（自动化/既有人工） | 生命周期锁、运行中阻断、不 Kill 与活动传输退出语义已有自动化和桌面人工证据；干净机只做发布冒烟复核 |
| P8-T08 A→B 正常升级 | PASS | `0.5.9 → 0.6.0` 真实事务升级，app/maintenance 同步切换，install.json 最终提交，Run/StartupApproved 保持且无 payload 混合 |
| P8-T09 升级失败与回滚 | PASS（故障注入） | staging、目录切换、self-check、系统集成、commit 及四阶段事务残留恢复均通过自动化，不在正式根重复破坏性注入 |
| P8-T10 pending cleanup | PASS（故障注入） | commit 前后所有权交接、多 pending 部分成功、状态写入失败和后续幂等重试均通过自动化 |
| P8-T11 保留用户数据卸载 | PASS | Windows 设置真实卸载；程序根、系统入口与 TEMP 清理，配置/日志/可信客户机及 Shared/Received 均保留 |
| P8-T12 删除用户数据卸载 | PASS | 修复临时执行页后从 Windows 设置真实卸载；配置、日志、可信客户机删除，Shared/Received 的 7 个文件大小与 SHA-256 不变 |
| P8-T13 重复生命周期 | PASS | 真实首装/升级/两种卸载与自动化异常中断/rollback 组合共同覆盖；最终可重新安装且无系统对象、进程、监听或卸载 TEMP 残留 |

Phase 8E 最终功能基线提交：`22d77ebe4e251aeeb6cd97a734ccb2978ac853e2`（`Separate uninstall execution UI`）。后续 Release Candidate 必须从包含本节文档收口的干净提交重新构建，并单独记录其源码 commit、build id、大小和 SHA-256。

结论：**Phase 8 development acceptance：PASS。** 后续只进入发布验证和真实缺陷修复，不主动扩展安装/卸载架构。

## 十、Phase 8F：干净 Windows 11 最终发布验收

冻结候选：`0.6.0-rc3`，源码提交 `ede7d1a080e8bda6fbef8f5eb4b01b6a10c282e2`，Build ID `0.6.0-ede7d1a-20260926073141`。P8-R01～R04 只允许使用 `C:\Projects\LanDrop-ReleaseCandidates\0.6.0-rc3-ede7d1a` 中的 RC3 产物和随目录冻结的 A 升级基线，不再使用 RC1 或 RC2。

### P8-R01：测试机要求

正式发布验收机：

- Windows 11；
- 不复制源码；
- 不复制 venv；
- 不单独安装 Python；
- 防火墙保持开启；
- 无旧 LanDrop 安装；
- 尽量无旧 `LanDrop.exe` / Python HTTP 8000 放行规则，以便观察首次运行行为；
- 至少具备一种真实 `Private` WLAN 或 Ethernet 客户机传输环境。

### P8-R02：WebView2 和离线安装边界

若系统已有 WebView2：

- 断网或不依赖 WAN 的条件下 Setup 可以正常安装；
- 不出现 Runtime 下载行为。

若能够构造无 WebView2 环境：

- Setup 在任何产品写入前明确阻断；
- 不自动联网下载；
- 用户安装 Runtime 后可重新运行 Setup。

### P8-R03：Windows 原生防火墙行为

在无既有 LanDrop/Python 规则的条件下首次启动 Private 服务：

- 记录 Windows 是否弹出原生授权提示；
- 如出现，选择 Private 允许后验证客户机可达，并检查系统实际生成规则的程序路径/Profile/方向等事实；
- 如拒绝、取消或系统策略不弹提示，验证 LanDrop 只读诊断和防火墙设置入口能够解释不可达状态；
- Setup/LanDrop/Uninstall 前后对比确认产品自身没有主动写规则。

该测试用于了解 Windows 行为，不把某一种弹窗结果写成所有 Windows 11 必然行为。

### P8-R04：完整发布生命周期

在同一台干净机完成：

```text
LanDrop-Setup.exe
→ 首次安装
→ 从开始菜单启动
→ Private 实际上传/下载
→ QR / 可信客户机
→ 退出并重新登录 Windows
→ 确认自启动进入托盘但服务 OFF
→ Public 拒绝（可安全构造时）
→ A→B 升级
→ 升级后再次传输
→ Windows 设置卸载
→ 按另一轮测试验证用户数据删除/保留
→ 检查系统状态恢复
```

至少验证：

- 程序目录；
- `%LOCALAPPDATA%\LanDrop`；
- Run；
- 开始菜单/桌面快捷方式；
- HKCU Uninstall；
- Toast/AUMID；
- TCP 8000；
- 进程/托盘；
- staging/rollback/temp；
- Windows 防火墙规则未被 LanDrop 主动创建/删除；
- Shared/Received 用户文件完整。

## 十一、阶段验收标准

第八阶段开发验收至少需要：

- [x] 主程序正式保持 `--onedir`，Setup/Uninstall 均可重复生成 onefile；
- [x] 当前用户固定安装路径和稳定 `app\LanDrop.exe` 入口通过自动化及正式根实机验证；
- [x] `install.json` 权威状态与 `install-history.jsonl` 历史落地；
- [x] 独立 `transaction.json`、最终提交顺序和中断恢复通过；
- [x] Setup/Uninstall/LanDrop 启动/pending cleanup 共用安装生命周期锁，TOCTOU 回归通过；
- [x] Setup 持锁时 `--self-check` 无副作用通过，四个事务阶段强杀后的普通 LanDrop 启动均被阻止；
- [x] payload manifest/SHA-256 验证通过；
- [x] 开始菜单、默认不勾选的可选桌面快捷方式和 HKCU Run 在真实安装中正常；AUMID/通知身份留待干净机发布回归；
- [x] 任务管理器/Windows 启动应用页可禁用自启动，升级不会自动恢复；真实重新登录行为留待 Phase 8F；
- [x] 升级保留 Run/StartupApproved 当前状态，实机已验证禁用状态逐字节保持；用户删除 Run 后的升级行为已由自动化覆盖；
- [x] 完整卸载只删除精确 `StartupApproved\Run\LanDrop` REG_BINARY 派生状态，其他 value 保持不变，异常类型保留并报告；
- [x] Windows 设置可见并可调用卸载；控制面板入口留待 Phase 8F 交叉验证；
- [x] Setup/LanDrop/Uninstall 的系统集成对象清单和实现均不包含防火墙或网络写入；干净系统的原生防火墙实际行为留待 Phase 8F；
- [x] WebView2 不捆绑、不联网安装，缺失时安全阻断；
- [x] 运行中的 LanDrop 会阻止升级/卸载，不自动 Kill；
- [x] 活动传输退出、取消和服务关闭语义已通过既有生命周期自动化与桌面人工回归；
- [x] A→B 事务升级、失败回滚和 `pending_cleanup` 通过自动化；
- [x] 升级成功后旧 payload 不长期累积；
- [x] Uninstall 能在隔离测试根及正式安装根迁出后删除完整程序根；
- [x] 临时卸载 request 绑定、时效、自身哈希、锁后权威 `install.json` 复核与统一 reparse 安全边界通过；
- [x] 系统集成对象清单明确，卸载器只删除自身实际创建且匹配的对象；
- [x] TEMP 自清理器使用目标外 cwd、等待 onefile 父/子进程、有限重试和最终不存在确认；
- [x] 用户数据删除/保留符合选择；
- [x] Shared/Received 和自定义用户文件目录永不删除；
- [x] 既有回归及 Phase 8 新增自动化合计 182 项全部通过；
- [x] 本机安装/升级/卸载完整生命周期通过。

**Phase 8F 最终发布验收**还必须额外满足：

- [ ] 在另一台无源码、无 venv、无独立 Python 的干净 Windows 11 上完成 P8-R01–R04；
- [ ] 补齐第七阶段延期的正式干净环境验证；
- [ ] 安装、升级、卸载前后系统状态可核对且无不可解释残留。

阶段产物：**一个以单文件 Setup 分发、安装后使用稳定 onedir 主程序、支持事务升级和标准 Windows 卸载、并通过完整发布生命周期验收的 LanDrop 正式安装版。**

## 十二、执行规则

- 不在第八阶段重新设计核心传输协议、配对体系、Public 网络支持或自动更新。
- 不为安装便利突破“系统网络与防火墙只读”原则。
- 不以管理员权限解决当前用户安装可以解决的问题。
- 不在 LanDrop 运行中覆盖/删除正式 app；不强杀活动进程。
- 不把新 payload 混合覆盖进旧 `_internal`。
- 不长期保留多版本程序目录；旧 payload 只作为事务回滚材料存在。
- 不让安装历史日志成为第二份权威安装状态。
- 不让 `install.json` 承担未完成事务状态；最终 commit 前只使用独立 `transaction.json`。
- 不允许 Setup、Uninstall、LanDrop 启动或 pending cleanup 绕过共用的安装生命周期锁。
- 不为执行 `--self-check` 释放 Setup 已持有的生命周期锁；self-check 必须走独立无副作用维护入口。
- 不允许存在未完成或无法安全解释的 `transaction.json` 时普通启动 LanDrop。
- 不因为卸载器位于程序根而放弃完整清理；必须先迁出再删根目录。
- 不删除用户收发目录及其中内容。
- 每个系统写入都必须有对应读回验证；每个删除动作都必须先验证边界和对象类型。
- 若发现设计需要新增管理员权限、自动防火墙规则、网络类别修改或后台联网安装依赖，暂停实现并先回写总计划重新评估。
