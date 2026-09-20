# 第六阶段先行技术验证待办

状态：**已完成并以 `0.6.0` 收官（2026-09-18）；65 项自动化测试及全部必选人工验收通过**。

前置条件：第五阶段 `0.5.0` 已完成验收，43 项自动化测试全部通过。第六阶段技术验证不得改变既有传输、生命周期、托盘/Toast、旧会话隔离和性能修复语义；若硬约束无法满足，应先更新正式计划再调整实现。

## 目标

验证“启动时完整建立网络基线、运行中轻量匹配当前 endpoint、异常时安全停止并给出可读诊断”的最小技术路径，同时验证分层防火墙诊断、Ethernet 场景、GUI 信息组织和性能回归不会破坏 LanDrop 的轻量化目标。

## 已冻结的产品与技术边界

- 每次启动服务重新完整检测，不保存固定 LAN IPv4。
- 本次进程内 session 的网络事实以 `InterfaceIndex + bound IPv4 + NetworkCategory` 为核心 endpoint；InterfaceIndex 是本次 session 的接口身份，接口别名只用于显示，不跨服务/应用重启持久复用。
- 运行中只验证当前 endpoint，不重新选网；其他 WLAN/Ethernet/TUN 的出现或变化不触发迁移或停服。
- 原接口/原 IPv4 失效时经快速二次确认后以 `network_changed` 停止；明确读取到 Private→Public 时立即停止。GUI 简化说明，终端/调试信息记录具体子类型和旧/新状态。
- 不判断多个接口是否属于“同一个实际网络”；不做跨接口/IP 热迁移。
- DNS、SSID、默认网关、route metric 只用于诊断，不直接停服。
- 运行期采用固定低频轻量检测，不做动态调频；新增独立 endpoint checker，优先使用 `_read_ipv4_table()`，不得直接复用会启动 PowerShell 的完整 `discover_interfaces()`。候选周期通过实测决定。
- 查询失败、暂时为空或接口/地址疑似短暂缺失时二次确认，避免一次瞬时失败误杀；明确读到 Public 不等待二次确认。睡眠恢复继续优先使用 `system_resume`。
- 防火墙分层诊断不进入同步启动硬路径，采用异步深度诊断或短超时；未完成显示“检测中”，失败显示“无法确定”，未发现明确 allow 只警告，不阻止已确认的 Private endpoint 启动。LanDrop 不自动修改 NetworkCategory、防火墙、代理、VPN 或静态 IP。
- TUN/VPN 中性展示；存在本身不是异常。
- 网络列表语义：`127.0.0.1` 是服务机内部回环地址，只供服务机本地访问并排除为 LAN 候选；无 IPv4 的断开适配器（例如 Wi-Fi Direct“本地连接*”和蓝牙 PAN）不进入 IPv4 候选列表。LanDrop 不启用、禁用或修改任何适配器。
- GUI 方向为“主控 / 信息 / 设置”三页；主控页使用“监听网络”等任务语言，并同时显示接口别名、Windows 网络配置文件名称、IPv4 与类别，以帮助区分并存的 WLAN/Ethernet；InterfaceIndex、回环、TUN/VPN 等技术细节放在信息页。浏览器无法可靠读取客户机的 Wi-Fi 名称，服务机网络名称只用于多网络辨认；客户机能够连接即已验证访问路径可达，界面不额外提示人工核对。设置页只提供诊断、重新检测/重试及 Windows 官方设置入口。
- Ethernet 正式支持 PC↔PC 直连静态 IPv4与 WLAN+Ethernet 共存验证；无 WAN 路由器 + DHCP + Ethernet 因关键维度已由家庭 WLAN 与无互联网直连分别覆盖，降为可选硬件兼容性补测。
- SMB 仅为可选 Private LAN 性能基准，不是功能验收门槛。
- 扩展坞实体 Ethernet 在空连接、无 IPv4 状态下的展示属于可选兼容性补测，不阻塞第六阶段收官。

## 阻塞技术验证

### P6-S01：启动网络基线快照

1. 在 Private 手机热点/家庭 WLAN 启动服务。
2. 记录选中 InterfaceIndex、IPv4、NetworkCategory/Profile 与诊断状态；接口别名只作显示。
3. 确认 TCP 8000 实际绑定与快照中的 IPv4 一致。
4. 重启服务后重新检测，不复用上次 InterfaceIndex 或 IPv4 缓存。
5. Public 网络继续拒绝启动。

通过标准：启动检测结果能够明确回答“本次服务绑定在哪个接口、哪个 IPv4、什么网络类别”，并作为后续运行期匹配基线。

### P6-S02：运行期轻量 endpoint 检查与二次确认

1. 分别实测 `2s / 3s / 5s` 等固定周期的轻量检查耗时与后台开销。
2. 新增独立 endpoint checker，优先复用 `_read_ipv4_table()`；不得直接调用 `discover_interfaces()`，不重复执行 PowerShell Profile 扫描、完整防火墙和全网卡候选分析。
3. 模拟单次查询返回空/暂时失败，确认不会立即停止 session。
4. 疑似异常后以较短间隔执行二次确认；恢复匹配时继续运行，连续不匹配时才 `network_changed`。
5. 明确读取到当前 endpoint 为 Public 时立即 `network_changed`，不等待二次确认。
6. 睡眠恢复继续走现有 `system_resume`，不等待网络二次确认。

通过标准：监测不会造成可感知 CPU/GUI/传输负担，且瞬时查询失败不会误杀。

### P6-S03：当前 endpoint 与其他接口变化隔离

至少验证：

1. WLAN endpoint 正常运行时新增 Ethernet，WLAN 原 IP 仍在 → 服务继续。
2. Ethernet endpoint 正常运行时 WLAN 状态变化，但 Ethernet 原 IP 仍在 → 服务继续。
3. 非选中 TUN/VPN 接口出现/变化 → 服务继续。
4. 选中接口断开/消失，二次确认仍异常 → `network_changed`。
5. 选中接口原 IPv4 被替换，二次确认仍异常 → `network_changed`，即使新 IP 仍在同一子网也不热迁移。
6. 明确读到选中连接 Private→Public → 立即 `network_changed`。
7. 网络变化自动停服后，确认端口释放、托盘先更新，并出现无按钮通知；点击正文只打开现有 GUI，不重启或迁移服务。

通过标准：LanDrop 只跟踪当前 session 的 endpoint，不因系统其他网络变化误停，也不在 endpoint 失效后偷偷继续旧 session。

### P6-S04：防火墙分层诊断

深度诊断异步执行或使用短超时；同步启动路径只要求确认 endpoint 为 Private。覆盖至少以下状态：

- 防火墙/Profile 开启；
- 精确 TCP 8000 Private allow；
- 当前程序路径 Private allow；
- 更宽泛 allow；
- 相关显式 block；
- 无明确匹配规则/无法确定；
- 诊断仍在执行时显示“检测中”；查询失败或权限不足时显示“无法确定”；
- 正式诊断不得只按规则显示名称包含 `Python` 进行筛选；应根据启用状态、方向、Profile、Action、Protocol、LocalPort、程序路径及地址/接口过滤器收集相关证据。

通过标准：诊断输出展示“证据 + 风险级别”，未发现明确 allow 不阻止 Private LAN 启动；程序不修改规则。

### P6-S05：GUI 信息结构与恢复支持

验证三页信息边界：

- 主控：启停、倒计时、收发目录、配对、可信客户机；
- 信息：当前 endpoint、其他 LAN、Profile/防火墙/TUN-VPN、监听与传输信息；
- 设置：测试/调试/重新检测/重试，以及打开 Windows 网络、防火墙、代理等官方设置入口。

“恢复”只能恢复 LanDrop 自身能力，例如重新检测、刷新、重试启动；不得自动修改系统配置。

### P6-S06：TUN/VPN 共存

1. 在开发机日常 VPN + TUN 环境运行。
2. 确认真实 LAN 被正确选择，TUN 显示但不被误判为异常。
3. 关闭/重开 TUN，只要当前 endpoint 仍有效就不停止服务。
4. 若无法可靠选择真实 LAN，则明确警告而不是静默绑定虚拟接口。

### P6-S07：Ethernet 测试矩阵

A. 无 WAN 路由器纯 LAN（可选硬件兼容性补测）：
- 路由器 WAN 空置；LAN/DHCP 工作。
- 两台 Windows 设备使用 Ethernet 或 Ethernet+Wi-Fi 完成 LanDrop 双向传输。

B. PC↔PC 网线直连：
- 双方固定设置 RFC1918 静态地址，例如 `192.168.50.1/24` 与 `192.168.50.2/24`，不依赖被现有实现排除的 `169.254.0.0/16` link-local 地址。
- 用户确认两端 Ethernet NetworkCategory 均为 Private 后执行传输；另将测试连接改为 Public，确认 LanDrop 拒绝启动。
- 记录网卡名称、链路速率、NetworkCategory 和实际监听地址。
- 完成下载、上传和完整性验证。

C. WLAN + Ethernet 共存：
- 按 P6-S03 验证当前 endpoint 不受其他接口非关键变化影响。

### P6-S08：性能回归

- 开发机 Private 手机热点：启动 `<=1.0s`、停止 `<=0.5s` 为严格回归线；当前参考基线约 `0.396s / 0.173s`。
- 上述启动线约束同步 endpoint 安全检测；异步防火墙深度诊断不得阻塞端口启动，且应单独记录完成耗时与失败状态。
- 通用受支持 Private LAN：启动达到/超过约 5 秒视为不可接受；停止接近 2 秒应定位。
- 任一单步骤稳定 `4.8–5.2s` 视为 timeout 回归信号。
- 验证第六阶段监测线程停止时不重新引入锁内 `join(timeout=5)` 或 DNS/FQDN 固定等待。

### P6-S09：LanDrop 吞吐上限与 SMB 可选基准

分层测试：

```text
iperf3 TCP
→ 最小裸 Python HTTP
→ LanDrop + curl
→ LanDrop + 浏览器
→ LanDrop 浏览器原始流上传（multipart 仅作兼容回退）
→ SMB（可选横向参照）
```

建议测试 `100 MB / 1 GB / 5 GB` 文件；记录平均/峰值 MB/s、总耗时、Python/总 CPU、网卡利用率、磁盘吞吐和 SHA-256。下载与上传分开测；尽量避免连续读取同一个文件导致缓存掩盖真实瓶颈。

SMB 若配置后可在 Private WLAN/Ethernet 中通过不同目标 IP 轻松切换，可长期保留为开发机基准；不要求 LanDrop 达到 SMB 吞吐，也不因基准测试放宽 Public 网络边界。

## 执行规则

- 除已明确标为可选的无 WAN 路由器组合外，S01–S08 为第六阶段网络/诊断功能的阻塞验证；S09 是正式性能基准计划，可与功能验收并行，但不把 SMB 是否可用作为阻塞项。
- 每个验证只改变一个主要变量，并记录实际接口、IPv4、NetworkCategory、TUN/VPN 状态和防火墙条件。
- 若需要新增 Windows API/PowerShell/CIM 调用，优先验证调用耗时和失败行为，不为诊断功能引入管理员权限或长期高频后台扫描。
- 若任何方案要求热迁移监听器、自动修改系统网络/防火墙、或复制第二套 ServiceController 生命周期，应停止实现并先更新正式计划。

## 可选人工补测（简表）

- 无 WAN 路由器 + DHCP + Ethernet：补充验证经交换机的自动地址场景；不阻塞收官。
- 服务绑定 Ethernet 时直接拔线：硬件断链路径；通用 endpoint 消失路径已经通过。
- `1 GB / 5 GB` 长时传输、并发下载和反向上传加压。
- SMB Private LAN 横向性能参照；不作为 LanDrop 指标目标。
- 交换两台电脑的服务端/客户端角色。

## 自动化实施记录（2026-09-17）

- 已冻结 session endpoint 的 `InterfaceIndex + IPv4 + NetworkCategory`，别名仅用于显示。
- 已新增独立 endpoint checker；地址检查只调用 IP Helper IPv4 表，500 次实测平均约 `2.68 ms/次`。当前使用 3 秒地址检查、15 秒 NetworkCategory 检查和 0.75 秒异常二次确认。
- 已验证其他接口不影响冻结 endpoint、单次缺失恢复后继续、连续缺失以 `network_changed` 停止、明确 Public 无需二次确认立即停止。
- 已将网络详情与防火墙证据查询放入后台并行执行；两项均使用 10 秒硬超时，失败以“无法确定”返回，不阻塞服务启动。
- 已实现防火墙精确端口、当前程序、宽泛 allow 和相关 block 的证据分类；未发现明确 allow 只警告。
- 已实现主控/信息/设置三页基础结构和只读 Windows 设置入口；CSS 仅维持本阶段可用性，整体视觉留待安装/GUI/卸载阶段统一调试。
- 主控页已补充可刷新接口选择，以支持 WLAN 与 Ethernet 同时为 Private 时由用户显式选择；列表不作为缓存，启动仍重新发现并验证选中 IPv4。
- 已将 `network_changed` 接入托盘先刷新、再清理旧通知并发送无按钮关闭通知的既有协调路径。
- Python、PowerShell 脚本语法检查通过，65 项自动化测试通过。原始流上传的超限、错误 CSRF、请求体提前中断、客户端断开分类和临时文件清理已有覆盖；用户环境已验证批量防火墙证据和网络详情能够读取，超时仍会安全终止并降级。显式指定 Public endpoint 的启动前拒绝已由自动化覆盖，并与实机运行期 Public 停服、GUI 禁选共同验证安全边界。
- PC↔PC Ethernet 核心矩阵已完成：静态 RFC1918 地址与两端 Private、显式 Ethernet 选择、实际 TCP 8000 访问、配对、双向传输和 SHA-256、WLAN/Ethernet/TUN 非选中接口隔离、选中地址替换、Private→Public 自动停服、端口释放、通知/托盘反馈及启停耗时均通过。无 WAN 路由器、物理拔线、5 GB/并发、SMB 和交换两机角色继续作为可选补测。
- 收官后的术语与信息密度调整已完成人工回归：主控页能够显示“接口别名 · Windows 网络名称 · IPv4 · 类别”，回环与 TUN/VPN 不进入监听网络下拉框但仍保留在信息页；服务启动、客户机访问、运行状态保留网络名称及停止流程均正常。该调整不改变 endpoint 选择与监测语义。
- Ethernet 分层性能结论：`.2 → .1` iperf3 约 `118.4–118.5 MB/s`，`.1 → .2` 约 `87.4–88.1 MB/s`；最小 Python HTTP 下载约 `87.65–88.77 MB/s`；LanDrop 浏览器下载 `86.25 MB/s` 且 SHA-256 一致，基本触及该方向上限。浏览器原始流上传客户端约 `118 MB/s`、GUI 端到端约 `105 MB/s` 且 SHA-256 一致；GUI 统计包含 `.part` 写入、同步与改名。
- 上传修复说明：Bottle multipart 会在 LanDrop 开始计时前完整解析大文件，旧 GUI 的 `300–450 MB/s` 实为临时文件本地复制速度。正式浏览器路径已改为 XHR `/upload/raw` 直读 WSGI 请求体，并提供页面实时进度；multipart 仅作兼容回退。远端 1 GB 上传途中手动停服已通过：客户端未误报成功，GUI 正确记录 `manual_stop`，日志记录失败时已接收 `407.896064 MB`，无最终不完整文件、无 `.part`、无 TCP 8000 监听且无 traceback。
- 性能推论边界：当前上传/下载差异已由两个方向各自的 TCP 基线及上传持久化开销解释；底层 `948/700 Mbit/s` 方向差异尚不能归因到某个具体网卡或驱动开关，仅记录为当前两机、网卡、驱动和 Windows 网络栈组合的实测基线，不阻塞第六阶段。

## 收官记录（2026-09-18）

1. 本机通过 `127.0.0.1` 将上限设为 `999 MB` 后选择十进制 1 GB 文件，页面按预期返回 HTTP 413，GUI 显示大小限制拒绝，服务可继续使用；没有新增最终文件，绝对路径补查 `.part` 数为 0。
2. 以 Private WLAN `10.146.168.152` 为冻结 endpoint，在运行中重新开启 VPN/TUN；等待超过低频类别复核周期后服务、倒计时和 endpoint 均保持正常，Meta/TUN 只作为未选中诊断接口显示，没有 `network_changed`。
3. 网络设置、防火墙设置和代理设置三个入口均唤起正确的 Windows 页面，没有由 LanDrop 自动修改系统配置。
4. 原始流上传的超限、错误 CSRF、请求体提前中断、客户端断开分类与清理自动化通过；Python/PowerShell 语法、差异格式及 65 项完整测试全部通过。
5. 第六阶段全部阻塞验证完成，发布基线升为 `0.6.0`；下一阶段进入便携打包与稳定性测试。
6. 可选环境清理：两台测试机分别卸载临时安装的 iperf3，并按各自日常用途恢复 Ethernet IPv4 配置；这些操作由用户手动完成，不属于阶段阻塞项。
