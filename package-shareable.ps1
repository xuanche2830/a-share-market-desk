$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseRoot = Join-Path $projectRoot "release"
$sourceFolder = Join-Path $releaseRoot "自主看盘台"
$shareName = "自主看盘台-分享版-Windows-x64"
$shareFolder = Join-Path $releaseRoot $shareName
$zipPath = Join-Path $releaseRoot "$shareName.zip"
$zipHashPath = "$zipPath.sha256.txt"

function Assert-ReleaseChild([string]$Path) {
    $releaseFull = [System.IO.Path]::GetFullPath($releaseRoot).TrimEnd('\') + '\'
    $targetFull = [System.IO.Path]::GetFullPath($Path)
    if (-not $targetFull.StartsWith($releaseFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝处理 release 目录之外的路径：$targetFull"
    }
}

Assert-ReleaseChild $shareFolder
Assert-ReleaseChild $zipPath
Assert-ReleaseChild $zipHashPath

if (-not (Test-Path -LiteralPath (Join-Path $sourceFolder "自主看盘台.exe"))) {
    throw "请先运行 build-windows.ps1 生成 Windows 便携版。"
}

if (Test-Path -LiteralPath $shareFolder) {
    Remove-Item -LiteralPath $shareFolder -Recurse -Force
}
foreach ($target in @($zipPath, $zipHashPath)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $shareFolder "data") | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceFolder "自主看盘台.exe") -Destination $shareFolder
Copy-Item -LiteralPath (Join-Path $sourceFolder "_internal") -Destination $shareFolder -Recurse
Copy-Item -LiteralPath (Join-Path $sourceFolder "portable.mode") -Destination $shareFolder
Copy-Item -LiteralPath (Join-Path $projectRoot "data\trading_calendar.json") -Destination (Join-Path $shareFolder "data")
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\便携版使用说明.txt") -Destination (Join-Path $shareFolder "使用说明.txt")

$exePath = Join-Path $shareFolder "自主看盘台.exe"
$exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $shareFolder "SHA256.txt") -Encoding UTF8 -Value "自主看盘台.exe  SHA256  $exeHash"

Compress-Archive -LiteralPath $shareFolder -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
Set-Content -LiteralPath $zipHashPath -Encoding UTF8 -Value "$shareName.zip  SHA256  $zipHash"

Write-Host "分享版文件夹：$shareFolder"
Write-Host "分享版压缩包：$zipPath"
Write-Host "压缩包 SHA256：$zipHash"
