# LanDrop

LanDrop 是一个面向 Windows 11 与 Android 浏览器的轻量局域网文件传输工具。
它计划在用户信任的 `Private` 网络中临时开启服务，实现电脑与手机之间的文件下载和上传。

## 当前状态

第一阶段的只读下载服务器已经实现并通过人工验收，当前包含：

- Windows 活动 IPv4 与网络类别的只读检测；
- 排除 Meta/TUN、VPN、Wi-Fi Direct、蓝牙和回环接口；
- 仅允许在 Windows `Private` 网络中启动；
- 只绑定选中的 LAN IPv4 与 `127.0.0.1`，不监听其他网卡；
- 仅共享指定目录，阻止路径越界和目录外符号链接；
- 文件统一使用下载响应，支持中文文件名；
- 拒绝 POST、PUT、PATCH 和 DELETE 等写操作；
- 明确提示端口占用、共享目录无效及网络状态异常；
- 使用 `Ctrl+C` 停止并关闭监听端口。

已在 Windows 标记为 `Private` 的用户手机热点中完成电脑和手机端人工验收。公司等 `Public` 网络仍会按产品边界拒绝启动。

## 产品边界

- 首先支持 Windows 11；手机端使用浏览器。
- 仅支持家庭 Wi-Fi、自有手机热点等可信 `Private` 网络。
- 不支持在陌生 `Public` 网络中提供文件传输服务。
- 只访问用户明确选择的共享目录和接收目录。
- 程序只检测网络与防火墙状态，不自动修改系统配置。
- 第一阶段使用 Python 标准库验证只读下载链路；第二阶段起正式 HTTP 应用层采用 Bottle。

## 目录结构

```text
LanDrop/
├─ app.py               # 命令行入口
├─ landrop/
│  ├─ cli.py            # 参数、状态输出与生命周期
│  ├─ network.py        # Windows 网络检测和安全选网
│  └─ server.py         # 只读 HTTP 文件服务
├─ shared/              # 第一阶段专用测试共享目录
├─ tests/               # 自动化测试目录
├─ notes/
│  └─ test-log.md       # 网络、设备和故障测试记录
├─ docs/
│  └─ plans/            # 设计与开发计划
├─ requirements.txt     # Python 依赖
└─ README.md            # 项目说明
```

## 环境要求

- Windows 11；
- Python 3.10 或更高版本；
- 当前使用家庭 Wi-Fi 或自有手机热点等可信网络；
- Windows 中该连接已由用户确认为 `Private`；
- Windows 防火墙保持开启。

第一阶段仅使用 Python 标准库，无需安装第三方依赖。

## 检查网络

先执行只读诊断：

```powershell
python app.py --diagnose
```

输出会分别标识 LAN 候选与被排除的 TUN/VPN 等接口。该操作不会修改网络或防火墙设置。

## 启动服务

```powershell
python app.py
```

默认共享 `shared` 目录并仅使用端口 `8000`。启动后控制台会显示电脑本机地址和手机访问地址；按 `Ctrl+C` 停止。

可选参数：

```powershell
python app.py --shared-dir C:\path\to\safe-test-folder
python app.py --interface WLAN
python app.py --interface 192.168.1.35
```

端口固定为 `8000`，命令行不提供改用其他端口的参数。当检测到多个可信 LAN 候选时，程序不会自行猜测，必须通过 `--interface` 指定接口名或 IPv4。指定接口也不能绕过 `Public` 网络限制。

## 第一阶段人工验收

1. 确认 Windows 防火墙开启，当前网络为 `Private`。
2. 启动程序，等待控制台明确显示“LanDrop 只读下载服务已启动”，再确认显示的接口不是 Meta/TUN/蓝牙等虚拟接口并检查监听；过早查询可能发生在网络检测和端口绑定之前。
3. 在电脑访问控制台给出的 `127.0.0.1` 和 LAN IPv4 地址。
4. 用同一可信局域网中的手机访问 LAN 地址。
5. 下载 `hello.txt` 和 `中文示例.txt`，确认浏览器进入下载流程。
6. 增加无隐私的图片、ZIP 和约 100 MB 文件并核对下载大小。
7. 访问不存在的文件以及包含 `../` 的越界路径，确认分别失败。
8. 按 `Ctrl+C`，等待控制台明确显示“服务已停止，监听端口已关闭”，再刷新页面并检查端口；在停止完成提示前查询可能仍处于短暂关闭过程。
9. 将结果记录到 `notes/test-log.md`。

若手机无法访问，先确认程序仍在运行、地址与端口正确、两台设备位于同一 LAN、网络类别为 `Private`，再核对防火墙规则是否对应当前 Python 路径及端口 `8000`。程序不会自动创建或修改防火墙规则。

## 安全提醒

`shared` 目录只应放置无隐私的测试文件。不要将桌面、下载目录、用户主目录或整个磁盘直接作为共享目录。第一阶段没有上传、删除、重命名、二维码、GUI、托盘、自动倒计时或 Public 网络传输功能。

详细设计与阶段验收要求见 [`docs/plans/LAN_file_transfer_development_plan_revised_v3.md`](docs/plans/LAN_file_transfer_development_plan_revised_v3.md)。
