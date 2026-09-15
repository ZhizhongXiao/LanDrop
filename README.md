# LanDrop

LanDrop 是一个面向 Windows 11 与 Android 浏览器的轻量局域网文件传输工具。它只在用户信任的 `Private` 网络中临时开启服务，让电脑与手机安全地上传和下载文件。

## 当前状态

- 第一阶段只读下载已通过电脑和手机人工验收。
- 第二阶段 Bottle 双向传输、文件安全处理与浏览器配对已经实现，自动化测试通过，等待电脑和手机人工验收。
- 公司等 `Public` 网络仍会拒绝启动。
- GUI、二维码、托盘、通知和 5 分钟生命周期尚未实现。

第二阶段包含：

- Bottle 0.13.4 HTTP 应用层；
- 下载目录与上传接收目录分离；
- 流式写入、1 GiB 默认上限和磁盘空间预检；
- 随机 `.part` 临时文件，完整同步后原子改名；
- 同名文件自动增加 ` (1)`，不静默覆盖；
- Unicode 文件名、Windows 非法字符和保留设备名处理；
- 共享目录越界和目录外符号链接防护；
- 8 位临时配对码和失败次数限制；
- 浏览器保存高熵凭据，电脑只保存 SHA-256 哈希；
- HttpOnly、SameSite Cookie 与上传 CSRF 校验；
- 浏览器自行取消信任，以及 PC 端列出和撤销信任。

## 产品边界

- 首先支持 Windows 11；手机端使用浏览器。
- 仅支持家庭 Wi-Fi、自有手机热点等可信 `Private` 网络。
- 不支持在陌生 `Public` 网络中提供文件传输服务。
- 只访问用户明确选择的共享目录和接收目录。
- 程序只检测网络与防火墙状态，不自动修改系统配置。
- HTTP 是本阶段在可信 LAN 内的轻量方案，不宣称具备 Public 网络中的抗窃听能力。

## 目录结构

```text
LanDrop/
├─ app.py               # 命令行入口
├─ landrop/
│  ├─ cli.py            # 参数、状态输出与生命周期
│  ├─ network.py        # Windows 网络检测和安全选网
│  ├─ server.py         # 双地址多线程 WSGI 服务
│  ├─ storage.py        # 安全下载、上传和文件处理
│  ├─ trust.py          # 浏览器凭据哈希存储
│  └─ web.py            # Bottle 页面与路由
├─ shared/              # 电脑提供下载的目录
├─ received/            # 手机上传文件的默认接收目录
├─ tests/               # 自动化测试
├─ notes/
│  └─ test-log.md       # 网络、设备和故障测试记录
├─ docs/plans/          # 设计与开发计划
├─ requirements.txt
└─ README.md
```

## 环境要求

- Windows 11；
- Python 3.10 或更高版本；
- 当前网络由用户确认为可信 `Private` 网络；
- Windows 防火墙保持开启，Private/TCP/8000 规则已正确配置。

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 启动

先做只读网络诊断：

```powershell
python app.py --diagnose
```

启动双向传输：

```powershell
python app.py
```

服务固定使用 TCP 8000，并只绑定选中的 LAN IPv4 与 `127.0.0.1`。控制台会显示本次 8 位配对码；新浏览器首次访问时需要输入，已信任浏览器会自动识别。按 `Ctrl+C` 停止服务。

## 自定义目录与上传上限

```powershell
python app.py `
    --shared-dir "D:\LanDrop\Shared" `
    --receive-dir "D:\LanDrop\Received" `
    --max-upload-mib 512
```

两个目录必须已经存在。上传上限允许设置为 1 至 10240 MiB；默认 1024 MiB。修改目录或上限需要停止后重新启动。

## 管理可信浏览器

列出浏览器：

```powershell
python app.py --trusted-clients
```

撤销一个浏览器：

```powershell
python app.py --forget-trusted <CLIENT_ID>
```

撤销全部浏览器：

```powershell
python app.py --forget-trusted all
```

凭据记录默认保存在 `%LOCALAPPDATA%\LanDrop\credentials.json`。文件中只有令牌哈希，不保存浏览器原始令牌。浏览器也可以在文件页面点击“取消信任此浏览器”。

## 第二阶段人工验收建议

1. 在自己的 Private 手机热点或家庭 Wi-Fi 中启动服务。
2. 新浏览器输入错误配对码，确认被拒绝；再输入正确配对码。
3. 配对后下载英文、中文、PNG、ZIP 和约 100 MiB 文件。
4. 从手机上传英文名、中文名、空文件、PNG、ZIP 和较大文件。
5. 重复上传同名文件，确认自动生成 `文件名 (1).扩展名`。
6. 上传超过自定义小上限的文件，确认返回 413 且没有残留 `.part`。
7. 停止并重新启动服务，确认同一浏览器无需再次输入配对码。
8. 取消信任后刷新，确认必须重新配对。
9. 按 `Ctrl+C`，等待“服务已停止，监听端口已关闭”。

自动化测试：

```powershell
python -m unittest discover -s tests -v
```

测试只使用项目内临时目录和进程内 WSGI 请求，不监听局域网端口。

## 安全提醒

- 不要共享桌面、下载目录、用户主目录或整个磁盘。
- 上传完成的用户文件和共享文件不会因取消信任而删除。
- Private 网络与浏览器凭据不能替代 HTTPS；不要在公司、酒店、咖啡店等陌生网络中使用。
- 程序不会自动更改网络类别、代理、VPN 或防火墙规则。

详细设计见 [`docs/plans/LAN_file_transfer_development_plan_revised_v3.md`](docs/plans/LAN_file_transfer_development_plan_revised_v3.md)。
