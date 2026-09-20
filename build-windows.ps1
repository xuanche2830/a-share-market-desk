param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot "work\build-venv\Scripts\python.exe"
$releaseRoot = Join-Path $projectRoot "release"
$appFolder = Join-Path $releaseRoot "自主看盘台"

Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot "work\build-venv")
}

if (-not $SkipInstall) {
    & $venvPython -m pip install "pyinstaller>=6,<7"
}

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "自主看盘台" `
    --paths (Join-Path $projectRoot "src") `
    --add-data "$(Join-Path $projectRoot 'web');web" `
    --distpath $releaseRoot `
    --workpath (Join-Path $projectRoot "work\pyinstaller-build") `
    --specpath (Join-Path $projectRoot "work") `
    (Join-Path $projectRoot "src\server.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败（退出码 $LASTEXITCODE），未生成新版本。请先退出正在运行的自主看盘台后重试。"
}

$dataFolder = Join-Path $appFolder "data"
New-Item -ItemType Directory -Force -Path $dataFolder | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "data\trading_calendar.json") -Destination $dataFolder -Force
if (Test-Path -LiteralPath (Join-Path $projectRoot "data\state.json")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "data\state.json") -Destination $dataFolder -Force
}
Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\便携版使用说明.txt") -Destination $appFolder -Force
Set-Content -LiteralPath (Join-Path $appFolder "portable.mode") -Encoding UTF8 -Value @(
    "portable=1"
    "data_scope=application_folder"
    "registry_writes=disabled"
)
$exePath = Join-Path $appFolder "自主看盘台.exe"
$exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
Set-Content -LiteralPath (Join-Path $appFolder "SHA256.txt") -Encoding UTF8 -Value "自主看盘台.exe  SHA256  $exeHash"

Write-Host ""
Write-Host "打包完成：$appFolder"
Write-Host "双击 自主看盘台.exe 即可启动。"
