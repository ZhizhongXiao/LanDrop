# 第五阶段先行技术验证待办

状态：**已完成（2026-09-17）**。
执行前置条件（历史）：第四阶段 `0.4.0` 已完成验收；本验证中的阻塞项通过前，不开始第五阶段正式功能编码。验证通过后，后续实现严格依据正式计划与本待办；若硬约束无法满足，应先更新文档再调整方案。

## 完成摘要

- S01：通过。活动传输中点击 X 只隐藏窗口，传输继续，TCP 8000 保持监听，未产生 `window_closed`。
- S02：通过。连续 20 次隐藏与恢复均返回同一个 pywebview Window，会话、倒计时和服务状态未重建；`show()` + `restore()` 已满足当前验收，无需引入 pywin32。
- S03：通过。自定义 AUMID `LanDrop.Desktop` 的通知动作可从通知中心回送当前进程，窗口动作经统一 GUI 调度路径执行。
- S04：通过。旧 revision、旧 session、并发 reset/stop 和通知清理失败均有自动化覆盖，旧状态修改动作不能影响当前服务；打开窗口仍可执行。
- S05：通过。模拟托盘图标缺失后，点击 X 走统一 `app_exit`，进程与监听端口均关闭，没有不可找回的隐藏进程。
- S06：通过。托盘与 Toast 只进入 `ActionCoordinator`，服务动作仍由 `ServiceController` 执行；实机未出现跨线程异常或 traceback。
- S07：通过。同一 revision 通知去重，重置/停止/到期/启动/退出时清理符合预期；异常终止遗留通知在下一次启动时清除。
- 扩展验收：自动停止后发送无按钮通知，点击正文只打开 GUI；Private 热点启动约 `0.396s`、停止约 `0.173s`；43 项自动化测试全部通过。

本待办现作为第五阶段验收记录保留。后续打包到其他电脑时仍需验证 AUMID、快捷方式和通知身份注册，但不影响源码形态第五阶段收官。

## 目标

验证“关闭主窗口后由托盘找回并继续控制同一服务”的最小技术路径，以及 Windows Toast 动作能否安全回到当前 LanDrop 进程；同时验证托盘失败保护、回调线程边界和旧通知竞态隔离。

本验证不实现完整正式 UI、打包、安装器、单实例或复杂托盘界面。

## 已冻结的技术边界

- 托盘使用 `pystray`；不因托盘引入 `pywin32`。
- pywebview 的 `window.events.closing` 通过返回 `False` 取消默认关闭；托盘可用时关闭请求调用 `window.hide()`，不销毁 Window。
- pywebview 的 `window.show()` 与 `window.restore()` 是首选恢复路径。Windows 原生激活只能作为验证后确有必要的最小“尽力激活”补充，不能绕过 Windows 前台焦点保护。
- Windows-Toasts 的交互通知固定使用自定义 AUMID `LanDrop.Desktop`。本阶段只要求 **LanDrop 进程仍存活时** 通知中心按钮能回送到当前进程；不实现进程退出后由旧通知重新启动应用。
- `ServiceController` 是 `session_id`、`deadline_revision`、截止时间和服务生命周期的唯一权威来源。每次启动服务生成新 `session_id`；每次重置截止时间递增 `deadline_revision`。
- Toast 的 `reset`/`stop` 必须携带创建通知时的 `session_id + deadline_revision`；“校验 + 状态修改”必须由 `ServiceController` 在同一原子控制路径中完成。`open_window` 是无状态 UI 动作，不受旧 session/revision 限制。
- pystray 与 Windows-Toasts 的 callback 不直接操作 pywebview Window；窗口显示/恢复/激活必须经统一 GUI 调度路径。
- 每个 `session_id + deadline_revision` 最多发送一次可操作到期 Toast；重置、停止、到期、新会话和应用退出时应主动清理失效 Toast，但主动清理不能替代 session/revision 校验。
- 正常用户退出入口是托盘“退出 LanDrop”，实际清理由统一 `request_exit()` 协调，不由托盘模块自行复制退出逻辑。

## 阻塞验收项

### P5-S01：关闭窗口仅隐藏

1. 启动 LanDrop 服务并开始一项活动下载或上传。
2. 确认托盘已经进入 `tray_ready` 状态。
3. 点击主窗口右上角 X。
4. 确认主窗口隐藏而非销毁。
5. 确认活动传输继续，服务仍为运行状态，TCP 8000 继续监听。
6. 确认当前 `session_id`、截止时间和传输状态未因隐藏窗口被重建。
7. 确认该操作不产生 `window_closed` 停止原因。

### P5-S02：从托盘重新显示窗口

1. 在窗口已隐藏时点击托盘“打开窗口”。
2. 确认原有 pywebview Window 重新显示，目录、可信设备、会话状态和活动传输均未丢失。
3. 先仅验证 `show()` + `restore()`，连续执行“隐藏 → 打开”至少 20 次。
4. 若窗口可见但焦点行为不可靠，再验证 pywebview Windows 原生后端的最小“尽力激活”补充。
5. 若 Windows 前台保护拒绝抢焦点，窗口仍必须可见，服务和传输不得中断。

硬性通过标准是“窗口可靠重新可见且原状态不丢失”；每次都强制获得键盘焦点不是硬性要求。

### P5-S03：通知中心动作回送

1. 注册/使用冻结的自定义 AUMID `LanDrop.Desktop`。
2. 使用 Windows-Toasts 发送包含测试按钮的交互 Toast。
3. 将通知转入 Windows 通知中心后再点击按钮。
4. 确认动作仍回送到当前 LanDrop 进程。
5. 确认回调只进入统一动作协调层，不从 Toast callback 线程直接操作 pywebview Window 或底层服务器。

### P5-S04：旧通知安全隔离与原子校验

至少验证两组情形：

1. **同一 session 的旧 revision**：发送 `session=A, revision=1` 的 Toast；重置截止时间使当前状态变为 `session=A, revision=2`；点击旧 Toast 的 `reset`/`stop`，必须被拒绝。
2. **旧 session**：发送 Session A 的 Toast；停止 A 并启动 Session B；点击 A 的 `reset`/`stop`，必须被拒绝。
3. 为校验原子性，测试或模拟“动作校验通过的同时另一线程重置/停止会话”的竞态；不得出现先校验旧状态、后修改新状态的窗口。
4. 旧动作不得重置新截止时间、停止新服务或改变 TCP 8000 状态。
5. 点击旧 Toast 的“打开窗口”可以显示当前 LanDrop 窗口，因为该动作不修改服务状态。

### P5-S05：托盘失败保护

1. 模拟 pystray 初始化失败或托盘尚未 `tray_ready`。
2. 点击主窗口 X。
3. 确认 LanDrop **不会**形成“窗口已隐藏且没有托盘入口”的不可找回后台进程。
4. 确认无托盘降级语义固定为：不执行 `hide()`，点击 X 通过统一 `request_exit(reason="app_exit")` 安全退出；能够显示状态时提示“托盘不可用，关闭窗口将退出 LanDrop”。确认服务与 TCP 8000 正确停止/释放。

### P5-S06：回调线程与统一协调层

1. 分别从 pystray 菜单和 Windows-Toasts callback 触发“打开窗口”“重置为 5 分钟”“停止服务”。
2. 确认托盘/Toast callback 只把动作提交给统一 `ActionCoordinator`。
3. 确认 GUI 操作经 GUI 调度路径执行；不得出现跨线程直接调用 pywebview Window 导致的异常或死锁。
4. 确认服务动作仍只由 `ServiceController` 执行，Bottle/WSGI/socket 未被托盘或 Toast 直接调用。

### P5-S07：Toast 去重与清理

1. 同一 `session_id + deadline_revision` 多次触发检查时，只允许生成一个可操作到期 Toast；Toast 使用固定 LanDrop group 和由 `session_id + deadline_revision` 派生的唯一 tag。
2. 重置截止时间后，旧 revision Toast 应主动移除，新 revision 到达触发条件后可以生成新 Toast。
3. 停止、自然到期、启动新会话和应用退出后，当前失效的可操作 Toast 应主动清理；应用启动时还应清理上次异常退出可能遗留的 LanDrop 可操作 Toast。
4. 即使模拟清理失败，旧 Toast 的 `reset`/`stop` 仍必须被 S04 的 session/revision 校验拒绝。

## Spike 通过后的正式实施约束

- `pystray` 只承担托盘图标和最小菜单：打开窗口、只读状态、运行时可用的“重置为 5 分钟”与“停止服务”、退出 LanDrop。托盘不提供启动服务、目录配置、可信设备管理或秒级倒计时。
- `pywebview` 保持唯一主窗口，自应用启动存活到 `request_exit()`。
- `ServiceController` 继续是服务、端口、传输、`session_id` 与 `deadline_revision` 的唯一权威控制器。
- 托盘与 Toast 共用统一动作协调层；两者不得直接触碰 Bottle、WSGI、socket 或计时器。
- `reset`/`stop` 的 expected-session/revision 校验与状态修改必须原子完成。
- 只有 `tray_ready` 后才允许正常的“点击 X → 隐藏到托盘”；托盘不可用时禁用关闭到托盘，点击 X 统一走 `request_exit(reason="app_exit")`，不得产生不可找回隐藏进程。
- Windows-Toasts 只保证当前 LanDrop 进程存活时的动作回调；退出应用后不支持由历史通知重新启动进程。
- 所有退出路径统一调用 `request_exit()`；正常用户入口为托盘“退出 LanDrop”，会话停止原因记录为 `app_exit`。GUI、托盘和 Toast 的普通停止服务动作统一记录为 `manual_stop`。
- 第四阶段的 `window_closed` 行为冻结为历史测试语义；第五阶段回归标准改为“点击 X 隐藏且服务/传输继续”。
- 单实例/重复启动控制不阻塞第五阶段，留到后续稳定性和打包阶段。

## 执行规则

- S01–S07 中任何一个阻塞项失败，均不得直接进入正式第五阶段编码。
- 若失败仅来自可替换的实现细节，可以在不改变上述硬约束的前提下调整实现。
- 若失败意味着必须改变技术栈、窗口生命周期、通知安全模型或 ServiceController 权威边界，应先更新正式计划和本待办，再继续开发。
