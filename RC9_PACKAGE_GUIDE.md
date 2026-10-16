# LanDrop 0.6.0 RC9 验收包说明

此包包含冻结的 LanDrop `0.6.0-rc9` 候选及 Phase 8F 清单。RC9 的 Phase 8F 尚待 R01～R04 实机验收；RC8 验收结果不能替代 RC9。

## 使用哪个文件

- 首次安装和 RC9 常规验收：只运行 `LanDrop-Setup-0.6.0-rc9.exe`。
- R04 周期二：先运行 `upgrade-baseline-A-0.5.9/LanDrop-Setup-A-0.5.9.exe`，再用同一个 RC9 Setup 升级 A→RC9。
- 不要直接运行或解压安装 `LanDrop-main-onedir-0.6.0-rc9.zip`；它是冻结主程序 payload，不是 Setup。
- `Uninstall-0.6.0-rc9.exe` 用于核验 Setup 内嵌维护组件身份。正常卸载从 Windows 设置的 LanDrop 卸载入口开始。

## 冻结身份

- Build ID：`0.6.0-4dff80a-20261016111954`
- 候选目录：`C:\Project\LanDrop\dist\0.6.0-rc9-4dff80a`
- 产物 SHA-256 与文件大小：见 `docs/releases/0.6.0-rc9.md`

运行 Setup 前先用 PowerShell 对照冻结记录核验文件身份：

```powershell
Get-FileHash .\LanDrop-Setup-0.6.0-rc9.exe -Algorithm SHA256
Get-FileHash .\Uninstall-0.6.0-rc9.exe -Algorithm SHA256
Get-FileHash .\LanDrop-main-onedir-0.6.0-rc9.zip -Algorithm SHA256
```

只在哈希和大小与冻结记录一致时继续。验收开始后不要重打包、替换或重新压缩候选文件。

## 记录结果

- 执行步骤见 `docs/plans/PHASE8F_RC9_ACCEPTANCE_CHECKLIST.md`。
- 在 `Evidence\<电脑名>_<日期>` 保存清单副本、截图、PowerShell 检查输出和阶段结果。
- 记录 Windows 版本、电脑名、网络类别、Setup 哈希、安装/升级/卸载结果和异常现场。
- 不记录配对码、QR token、Cookie 或其他凭据。
- 遇到安装状态不一致、事务残留、意外删除、防火墙变化、传输异常或卸载失败时，立即停止并保留现场；不要手工清理或重复运行 Setup/Uninstall“试试看”。

完整回归、静态检查和构建自检记录位于 `Evidence\BUILD-2026-10-16`。
