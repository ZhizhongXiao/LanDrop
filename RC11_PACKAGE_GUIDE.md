# LanDrop 0.6.0 RC11 历史验收包说明

此说明仅对应已被取代的 RC11 候选。当前正式版交付说明见 [`0.6.0_PACKAGE_GUIDE.md`](0.6.0_PACKAGE_GUIDE.md)，请勿将 RC11 文件或哈希用于正式版校验。

此包是新的 `0.6.0-rc11` 候选，不能沿用 RC10 的冻结记录或验收结果。当前 Phase 8F R01～R04 尚待实机验收。

## 使用哪个文件

- 首次安装和 R01～R03：只运行 `LanDrop-Setup-0.6.0-rc11.exe`。
- `Uninstall-0.6.0-rc11.exe` 用于核对 Setup 内嵌的维护组件身份；正常卸载从 Windows 设置中的 LanDrop 卸载入口开始。
- `LanDrop-main-onedir-0.6.0-rc11.zip` 是冻结主程序 payload，不是 Setup；不要直接安装或解压运行它。
- RC11 候选目录没有 A（0.5.9）升级基线，因此 RC11 当时不能开始 R04 周期二。该限制和验收记录只对应历史 RC11。

## 冻结身份

- Build ID：`0.6.0-99628ea-20261017050753`
- 运行时代码提交：`99628ea4650406163ba615f7ed18d9402cc7144f`
- 候选目录：`C:\Project\LanDrop\dist\0.6.0-rc11-99628ea`
- 产物大小和 SHA-256：见 [`docs/releases/0.6.0-rc11.md`](docs/releases/0.6.0-rc11.md)

运行 Setup 前先在候选目录用 PowerShell 对照冻结记录复核：

```powershell
Get-FileHash .\LanDrop-Setup-0.6.0-rc11.exe -Algorithm SHA256
Get-FileHash .\Uninstall-0.6.0-rc11.exe -Algorithm SHA256
Get-FileHash .\LanDrop-main-onedir-0.6.0-rc11.zip -Algorithm SHA256
Get-FileHash .\payload-manifest.json -Algorithm SHA256
Get-FileHash .\build-info.json -Algorithm SHA256
```

只有大小与 SHA-256 均匹配时才继续。验收开始后不要重打包、替换或重新压缩候选文件。

## 记录验收

- 执行步骤见 [`docs/plans/PHASE8F_RC11_ACCEPTANCE_CHECKLIST.md`](docs/plans/PHASE8F_RC11_ACCEPTANCE_CHECKLIST.md)。
- 自动回归、静态检查、自检和哈希复核日志位于 `Evidence\BUILD-2026-10-17`。
- 在 `Evidence\<电脑名>_<日期>` 保存清单副本、截图、PowerShell 检查输出和实机结果。特别记录配对前/后的传输文件 SHA-256，以及卸载后的安装目录、注册表、进程、TCP 8000、防火墙规则和用户哨兵文件哈希。
- 不记录配对码、QR token、Cookie 或其他凭据。
- 卸载不会替用户删除 Windows 防火墙允许规则；按钮用于打开 Windows 的“允许应用”页面，用户可按需要手动检查或删除。
- 遇到安装状态不一致、事务残留、意外删除、防火墙变化、传输异常或卸载失败时，立即停止并保留现场；不要手工清理或重复运行 Setup/Uninstall“试试看”。
