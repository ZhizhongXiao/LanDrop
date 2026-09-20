[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$expectedPython = "3.12"
$expectedPyInstaller = "6.22.3"
$outputDirectory = Join-Path $projectRoot "dist\LanDrop"
$executable = Join-Path $outputDirectory "LanDrop.exe"

Push-Location $projectRoot
try {
    $runningPortable = @(
        Get-Process -Name "LanDrop" -ErrorAction SilentlyContinue |
            Where-Object {
                try {
                    [string]::Equals(
                        [System.IO.Path]::GetFullPath($_.Path),
                        [System.IO.Path]::GetFullPath($executable),
                        [System.StringComparison]::OrdinalIgnoreCase
                    )
                }
                catch {
                    $false
                }
            }
    )
    if ($runningPortable.Count -gt 0) {
        $processIds = ($runningPortable.Id -join ", ")
        throw "目标便携版仍在运行（PID：$processIds）。请从托盘退出 LanDrop 后再构建；当前 dist 不会被修改。"
    }

    $pythonVersion = (& $Python -c "import platform; print(platform.python_version()); print(platform.architecture()[0])")
    if ($LASTEXITCODE -ne 0) {
        throw "无法运行指定的 Python：$Python"
    }
    if (-not $pythonVersion[0].StartsWith("$expectedPython.")) {
        throw "构建要求 Python $expectedPython.x，当前为 $($pythonVersion[0])。"
    }
    if ($pythonVersion[1] -ne "64bit") {
        throw "构建要求 64 位 Python，当前为 $($pythonVersion[1])。"
    }

    $pyInstallerVersion = (& $Python -m PyInstaller --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $pyInstallerVersion -ne $expectedPyInstaller) {
        throw "构建要求 PyInstaller $expectedPyInstaller，当前为 $pyInstallerVersion。请先安装 requirements-build.txt。"
    }

    & $Python -m PyInstaller --clean --noconfirm LanDrop.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码 $LASTEXITCODE。"
    }

    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "构建完成但未找到 $executable。"
    }

    $selfCheck = Start-Process `
        -FilePath $executable `
        -ArgumentList "--self-check" `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfCheck.ExitCode -ne 0) {
        throw "便携版自检失败，退出码 $($selfCheck.ExitCode)。请检查 %LOCALAPPDATA%\LanDrop\logs\application.log。"
    }

    $gitRevision = (& git rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) {
        $gitRevision = "unknown"
    }
    $buildInfo = [ordered]@{
        landrop_version = (& $Python -c "from landrop import __version__; print(__version__)").Trim()
        python_version = $pythonVersion[0]
        python_architecture = $pythonVersion[1]
        pyinstaller_version = $pyInstallerVersion
        git_revision = $gitRevision.Trim()
        built_at = (Get-Date).ToUniversalTime().ToString("o")
        executable_sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash
        portable_self_check = "passed"
    }
    $buildInfo |
        ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $outputDirectory "build-info.json") -Encoding utf8

    Write-Host "LanDrop 便携版构建完成：$outputDirectory"
    Write-Host "SHA-256：$($buildInfo.executable_sha256)"
}
finally {
    Pop-Location
}
