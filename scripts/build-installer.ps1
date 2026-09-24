#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "build"))
$payloadRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot "setup-payload"))
$manifestPath = Join-Path $buildRoot "payload-manifest.json"
$mainBuild = Join-Path $projectRoot "dist\LanDrop"
$uninstallBuild = Join-Path $projectRoot "dist\Uninstall.exe"
$setupBuild = Join-Path $projectRoot "dist\LanDrop-Setup.exe"
$verificationRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot "安装 验证"))

function Assert-BuildChild([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $buildRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作 build 根外的路径：$resolved"
    }
}

Push-Location $projectRoot
try {
    $pythonDetails = @(& $Python -c "import platform; print(platform.python_version()); print(platform.architecture()[0])")
    if ($LASTEXITCODE -ne 0 -or -not $pythonDetails[0].StartsWith("3.12.") -or $pythonDetails[1] -ne "64bit") {
        throw "构建要求 Python 3.12 x64。"
    }
    $pyInstallerVersion = (& $Python -m PyInstaller --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $pyInstallerVersion -ne "6.22.3") {
        throw "构建要求 PyInstaller 6.22.3，当前为 $pyInstallerVersion。"
    }

    & $PSScriptRoot\build-portable.ps1 -Python $Python
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $mainBuild "LanDrop.exe") -PathType Leaf)) {
        throw "主程序 onedir 构建失败。"
    }

    & $Python -m PyInstaller --clean --noconfirm Uninstall.spec
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uninstallBuild -PathType Leaf)) {
        throw "Uninstall onefile 构建失败。"
    }
    $uninstallSelfCheck = Start-Process `
        -FilePath $uninstallBuild `
        -ArgumentList "--self-check" `
        -WorkingDirectory $projectRoot `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($uninstallSelfCheck.ExitCode -ne 0) {
        throw "Uninstall onefile 自检失败，退出码 $($uninstallSelfCheck.ExitCode)。"
    }

    foreach ($target in @($payloadRoot, $verificationRoot)) {
        Assert-BuildChild $target
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path (Join-Path $payloadRoot "app") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $payloadRoot "maintenance") -Force | Out-Null
    Copy-Item -Path (Join-Path $mainBuild "*") -Destination (Join-Path $payloadRoot "app") -Recurse -Force
    Copy-Item -LiteralPath $uninstallBuild -Destination (Join-Path $payloadRoot "maintenance\Uninstall.exe") -Force

    $version = (& $Python -c "from landrop import __version__; print(__version__)").Trim()
    $gitRevision = (& git rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) { $gitRevision = "unknown" }
    $buildId = "$version-$($gitRevision.Trim())-$((Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss'))"
    & $Python scripts\build-setup-payload.py `
        --payload $payloadRoot `
        --manifest $manifestPath `
        --version $version `
        --build-id $buildId
    if ($LASTEXITCODE -ne 0) { throw "payload manifest 生成失败。" }

    & $Python -m PyInstaller --clean --noconfirm Setup.spec
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setupBuild -PathType Leaf)) {
        throw "Setup onefile 构建失败。"
    }

    New-Item -ItemType Directory -Path $verificationRoot -Force | Out-Null
    $verificationExe = Join-Path $verificationRoot "LanDrop-Setup.exe"
    Copy-Item -LiteralPath $setupBuild -Destination $verificationExe -Force
    $selfCheck = Start-Process `
        -FilePath $verificationExe `
        -ArgumentList "--self-check" `
        -WorkingDirectory $verificationRoot `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfCheck.ExitCode -ne 0) {
        throw "Setup onefile 非项目 cwd 自检失败，退出码 $($selfCheck.ExitCode)。"
    }

    $buildInfo = [ordered]@{
        landrop_version = $version
        build_id = $buildId
        python_version = $pythonDetails[0]
        python_architecture = $pythonDetails[1]
        pyinstaller_version = $pyInstallerVersion
        setup_sha256 = (Get-FileHash -LiteralPath $setupBuild -Algorithm SHA256).Hash
        uninstall_sha256 = (Get-FileHash -LiteralPath $uninstallBuild -Algorithm SHA256).Hash
        main_payload_manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
        setup_self_check = "passed"
    }
    $buildInfo | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $projectRoot "dist\LanDrop-Setup.build-info.json") -Encoding utf8
    Write-Host "LanDrop Setup 构建完成：$setupBuild"
    Write-Host "SHA-256：$($buildInfo.setup_sha256)"
}
finally {
    Pop-Location
}
