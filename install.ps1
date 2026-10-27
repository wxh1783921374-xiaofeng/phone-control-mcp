# install.ps1 —— phone-control-mcp 一键安装
# 功能：① 创建 .venv 并安装 mcp 依赖；② 若找不到 adb，自动下载 Google platform-tools；③ 运行自检
# 用法：右键“使用 PowerShell 运行”，或在终端执行：
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host "=== phone-control-mcp 安装 ===" -ForegroundColor Cyan

# ---------- 1. Python 虚拟环境 + mcp 依赖 ----------
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[1/3] 创建虚拟环境 .venv ..."
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd) { throw "找不到 python，请先安装 Python 3.10+（https://www.python.org/downloads/）" }
    & python -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败" }
} else {
    Write-Host "[1/3] 虚拟环境已存在，跳过创建"
}

Write-Host "      安装/更新 mcp 依赖 ..."
& $venvPy -m pip install --upgrade pip -q --disable-pip-version-check
& $venvPy -m pip install "mcp>=1.2" -q --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "安装 mcp 失败，请检查网络或 pip 源" }

# ---------- 2. adb (platform-tools) ----------
$localAdb = Join-Path $root "adb\platform-tools\adb.exe"
$sysAdb = Get-Command adb -ErrorAction SilentlyContinue
if (Test-Path $localAdb) {
    Write-Host "[2/3] 项目内置 adb 已存在：$localAdb"
} elseif ($sysAdb) {
    Write-Host "[2/3] 系统 PATH 中已有 adb：$($sysAdb.Source)"
} else {
    Write-Host "[2/3] 下载 Google platform-tools（约 8 MB）..."
    $zip = Join-Path $env:TEMP "platform-tools-latest-windows.zip"
    $url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath (Join-Path $root "adb") -Force
        Remove-Item $zip -Force
        Write-Host "      已解压到 $root\adb\platform-tools" -ForegroundColor Green
    } catch {
        Write-Warning "自动下载失败：$_`n请手动下载 $url 并解压到 $root\adb\ 目录（保证存在 adb\platform-tools\adb.exe）"
    }
}

# ---------- 3. 自检 ----------
Write-Host "[3/3] 运行自检 doctor ..."
Write-Host ""
& $venvPy (Join-Path $root "phone_control.py") doctor

Write-Host ""
Write-Host "=== 安装完成 ===" -ForegroundColor Cyan
Write-Host "MCP 客户端配置命令：$venvPy"
Write-Host "MCP 服务器脚本：  $root\server.py"
Write-Host "CLI 用法：        & '$venvPy' '$root\phone_control.py' <命令>  （或系统 python 也可，CLI 零依赖）"
