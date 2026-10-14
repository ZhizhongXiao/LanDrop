# LanDrop RC8 验收包说明

本目录包含 LanDrop `0.6.0-rc8` 的冻结候选产物和实机验收材料。RC8 尚未通过最终发布验收，不是正式 release。

## 使用哪个文件

- 首次安装和 RC8 常规验收：只运行 `LanDrop-Setup-0.6.0-rc8.exe`。
- R04 周期二：按验收清单先运行 `upgrade-baseline-A-0.5.9/LanDrop-Setup-A-0.5.9.exe`，再使用同一个 RC8 Setup 升级。
- 不要直接运行或解压安装 `LanDrop-main-onedir-0.6.0-rc8.zip`；它是冻结的主程序 payload 基线，不是 Setup。
- `Uninstall-0.6.0-rc8.exe` 是 Setup 内嵌维护组件的独立身份核验材料；正常卸载应从 Windows 设置的 LanDrop 卸载入口开始。

## 验收文件

- 冻结身份、产物 SHA-256 和限制：[RC8 冻结记录](docs/releases/0.6.0-rc8.md)
- P8-R01～R04 步骤和检查项：[RC8 实机验收清单](docs/plans/PHASE8F_RC8_ACCEPTANCE_CHECKLIST.md)

运行 Setup 前，使用 PowerShell 对照冻结记录逐项核验文件哈希，例如：

```powershell
Get-FileHash .\LanDrop-Setup-0.6.0-rc8.exe -Algorithm SHA256
Get-FileHash .\Uninstall-0.6.0-rc8.exe -Algorithm SHA256
```

只在哈希与冻结记录一致时继续。验收期间不要重打包或替换候选文件。

## 记录结果

建议在本目录创建 `Evidence\<电脑名>-<日期>`，保存该电脑的清单副本、截图、PowerShell 检查输出和每阶段结果。若本清单此前已在另一台电脑上填写，先复制一份作为当前电脑的工作清单；原有勾选和描述不能作为新电脑的验收证据。可用记事本编辑 Markdown，不需要安装 Codex、VS Code、源码或 Python。

PowerShell 检查结果可复制到文本文件；截图用 Windows 截图工具保存。记录 Windows 版本、电脑名、网络类别、Setup 哈希、安装/升级/卸载结果和异常现场。不要记录配对码、QR token、Cookie 或其他凭据。

遇到安装状态不一致、事务残留、意外删除、防火墙变化、传输异常或卸载失败时，按清单立即停止后续操作，保留磁盘/注册表/进程现场；不要手工清理或重复运行 Setup/Uninstall“试试看”。

## 后续源码

此验收包不包含开发源码。需要继续开发时，从 GitHub 克隆主仓库：<https://github.com/ZhizhongXiao/LanDrop>。克隆代码不替代本验收包中的冻结 RC8 产物。
