[CmdletBinding()]
param(
    [string]$BuildDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $BuildDirectory) {
    $BuildDirectory = Join-Path $projectRoot "dist\LanDrop"
}
$source = [System.IO.Path]::GetFullPath($BuildDirectory)
$temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$testRoot = Join-Path $temporaryRoot ("LanDrop 便携路径测试 " + [guid]::NewGuid().ToString("N"))
$copiedBuild = Join-Path $testRoot "含 空格的中文目录\LanDrop"
$unrelatedWorkingDirectory = Join-Path $testRoot "其他 工作目录"

if (-not (Test-Path -LiteralPath (Join-Path $source "LanDrop.exe") -PathType Leaf)) {
    throw "未找到便携版程序：$source\LanDrop.exe"
}
if (-not ([System.IO.Path]::GetFullPath($testRoot).StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "测试目录没有位于系统临时目录中，已拒绝继续。"
}

try {
    New-Item -ItemType Directory -Path $copiedBuild -Force | Out-Null
    New-Item -ItemType Directory -Path $unrelatedWorkingDirectory -Force | Out-Null
    Copy-Item -Path (Join-Path $source "*") -Destination $copiedBuild -Recurse -Force

    $sourceExecutable = Join-Path $source "LanDrop.exe"
    $copiedExecutable = Join-Path $copiedBuild "LanDrop.exe"
    $sourceHash = (Get-FileHash -LiteralPath $sourceExecutable -Algorithm SHA256).Hash
    $copiedHash = (Get-FileHash -LiteralPath $copiedExecutable -Algorithm SHA256).Hash
    if ($sourceHash -ne $copiedHash) {
        throw "复制后的 LanDrop.exe 哈希与原始构建不一致。"
    }

    $selfCheck = Start-Process `
        -FilePath $copiedExecutable `
        -ArgumentList "--self-check" `
        -WorkingDirectory $unrelatedWorkingDirectory `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfCheck.ExitCode -ne 0) {
        throw "移动路径自检失败，退出码 $($selfCheck.ExitCode)。"
    }

    $buildInfoPath = Join-Path $copiedBuild "build-info.json"
    if (-not (Test-Path -LiteralPath $buildInfoPath -PathType Leaf)) {
        throw "复制后的便携目录缺少 build-info.json。"
    }
    $buildInfo = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
    if ($buildInfo.portable_self_check -ne "passed") {
        throw "构建信息未记录便携版自检通过。"
    }

    [PSCustomObject]@{
        Result = "passed"
        SourceDirectory = $source
        TestDirectory = $copiedBuild
        WorkingDirectory = $unrelatedWorkingDirectory
        ExecutableSha256 = $copiedHash
        SelfCheckExitCode = $selfCheck.ExitCode
    } | Format-List
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        if (
            $resolvedTestRoot.StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
            ([System.IO.Path]::GetFileName($resolvedTestRoot) -like "LanDrop 便携路径测试 *")
        ) {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
        }
    }
}
