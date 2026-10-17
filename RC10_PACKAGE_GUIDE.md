# LanDrop 0.6.0 RC10 历史验收包说明

此说明只记录历史冻结的 LanDrop `0.6.0-rc10` 候选。RC10 已于 2026-10-17 被 RC11 取代，不得用于当前 Phase 8F 验收；其哈希和验收结果也不能复制到 RC11。当前入口见 [`RC11_PACKAGE_GUIDE.md`](RC11_PACKAGE_GUIDE.md)。

## 使用哪个文件

- 首次安装和 RC10 常规验收：只运行 `LanDrop-Setup-0.6.0-rc10.exe`。
- R04 周期二：先运行 `upgrade-baseline-A-0.5.9/LanDrop-Setup-A-0.5.9.exe`，再用同一个 RC10 Setup 升级 A→RC10。
- 不要直接运行或解压安装 `LanDrop-main-onedir-0.6.0-rc10.zip`；它是冻结主程序 payload，不是 Setup。
- `Uninstall-0.6.0-rc10.exe` 用于核验 Setup 内嵌维护组件身份。正常卸载从 Windows 设置的 LanDrop 卸载入口开始。

## 冻结身份

- Build ID：`0.6.0-f483066-20261016121544`
- 候选目录：`C:\Project\LanDrop\dist\0.6.0-rc10-f483066`
- 产物 SHA-256 与文件大小：见 `docs/releases/0.6.0-rc10.md`

运行 Setup 前先用 PowerShell 对照冻结记录核验：

```powershell
Get-FileHash .\LanDrop-Setup-0.6.0-rc10.exe -Algorithm SHA256
Get-FileHash .\Uninstall-0.6.0-rc10.exe -Algorithm SHA256
Get-FileHash .\LanDrop-main-onedir-0.6.0-rc10.zip -Algorithm SHA256
```

只在哈希和大小与冻结记录一致时继续。验收开始后不要重打包、替换或重新压缩候选文件。

## 记录结果

- 执行步骤见 `docs/plans/PHASE8F_RC10_ACCEPTANCE_CHECKLIST.md`。
- 在 `Evidence\\<电脑名>_<日期>` 保存清单副本、截图、PowerShell 检查输出和阶段结果。
- 记录 Windows 版本、电脑名、网络类别、Setup 哈希、安装/升级/卸载结果和异常现场。
- 不记录配对码、QR token、Cookie 或其他凭据。
- 遇到安装状态不一致、事务残留、意外删除、防火墙变化、传输异常或卸载失败时，立即停止并保留现场；不要手工清理或重复运行 Setup/Uninstall“试试看”。

完整回归、静态检查和构建自检记录位于 `Evidence\\BUILD-2026-10-16`。
