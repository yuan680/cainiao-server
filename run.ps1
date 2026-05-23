#!/usr/bin/env powershell
<#
.SYNOPSIS
  菜鸟物流查询 — 一键启动脚本（本地开发/部署）
  同时启动 cainiao_server (58080) 和 wps_server (8080)
.DESCRIPTION
  用法:
    .\run.ps1                  # 启动服务（前台运行）
    .\run.ps1 -Daemon         # 后台静默启动（Windows 计划任务/开机自启用）

  启动后:
    cainiao_server:  http://127.0.0.1:58080  （菜鸟 API 中间层）
    wps_server:      http://127.0.0.1:8080   （WPS 友好页面 + 表单）

  依赖:
    - Python 3.8+
    - pip install requests
#>

param(
    [switch]$Daemon
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir    = Join-Path $ScriptDir "logs"

# 确保日志目录存在
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$msg, [string]$color = "White")
    $ts = Get-Date -Format "HH:mm:ss"
    if ($Daemon) {
        "[$ts] $msg" | Out-File -FilePath (Join-Path $logDir "daemon.log") -Append -Encoding utf8
    } else {
        Write-Host "[$ts] $msg" -ForegroundColor $color
    }
}

function Test-Python {
    try {
        $v = & python --version 2>&1
        if ($v -match "Python 3\.\d+") { return $true }
        return $false
    } catch { return $false }
}

function Find-ProcessByPort {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        if ($conn) { return $conn.OwningProcess }
    } catch {}
    return $null
}

function Kill-ProcessOnPort {
    param([int]$Port)
    $pid = Find-ProcessByPort -Port $Port
    if ($pid) {
        try {
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
            Write-Log "  Killed old process (PID $pid) on port $Port" -color "Yellow"
        } catch {}
    }
}

# ══════════════════════════════════════════
# 前置检查
# ══════════════════════════════════════════
if (-not (Test-Python)) {
    Write-Host "❌ 未找到 Python 3，请先安装 Python 3.8+" -ForegroundColor Red
    exit 1
}

# 检查依赖
try {
    import requests
} catch {
    Write-Log "📦 安装依赖 requests..." -color "Yellow"
    & python -m pip install requests -q
}

# ══════════════════════════════════════════
# 清理旧进程
# ══════════════════════════════════════════
Write-Log "正在初始化..." -color "Cyan"
Kill-ProcessOnPort -Port 58080
Kill-ProcessOnPort -Port 8080

# ══════════════════════════════════════════
# 启动 cainiao_server (58080)
# ══════════════════════════════════════════
$caiaoLog = Join-Path $logDir "cainiao_server.log"
Write-Log "▶ 启动 cainiao_server (端口 58080)..." -color "Yellow"
$caiaoArgs = @(
    "`"$(Join-Path $ScriptDir "cainiao_server.py")`""
    "--port", "58080"
    "--host", "127.0.0.1"
)

if ($Daemon) {
    Start-Process -FilePath "python" -ArgumentList $caiaoArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden -RedirectStandardOutput $caiaoLog -RedirectStandardError $caiaoLog
} else {
    Start-Process -FilePath "python" -ArgumentList $caiaoArgs -WorkingDirectory $ScriptDir -WindowStyle Minimized
}
Start-Sleep -Seconds 2

# 验证
$pid58080 = Find-ProcessByPort -Port 58080
if ($pid58080) {
    Write-Log "  ✅ cainiao_server 运行中 (PID $pid58080)" -color "Green"
} else {
    Write-Log "  ❌ cainiao_server 启动失败" -color "Red"
    if (-not $Daemon) { Read-Host "按 Enter 退出"; exit 1 }
}

# ══════════════════════════════════════════
# 启动 wps_server (8080)
# ══════════════════════════════════════════
$wpsLog = Join-Path $logDir "wps_server.log"
Write-Log "▶ 启动 wps_server (端口 8080)..." -color "Yellow"
$wpsArgs = @(
    "`"$(Join-Path $ScriptDir "wps_server.py")`""
)

if ($Daemon) {
    Start-Process -FilePath "python" -ArgumentList $wpsArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden -RedirectStandardOutput $wpsLog -RedirectStandardError $wpsLog
} else {
    Start-Process -FilePath "python" -ArgumentList $wpsArgs -WorkingDirectory $ScriptDir -WindowStyle Minimized
}
Start-Sleep -Seconds 2

# 验证
$pid8080 = Find-ProcessByPort -Port 8080
if ($pid8080) {
    Write-Log "  ✅ wps_server 运行中 (PID $pid8080)" -color "Green"
} else {
    Write-Log "  ❌ wps_server 启动失败" -color "Red"
}

# ══════════════════════════════════════════
# 健康检查
# ══════════════════════════════════════════
Start-Sleep -Seconds 1
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:58080/health" -TimeoutSec 5 -ErrorAction Stop
    Write-Log "  ✅ cainiao_server 健康检查通过" -color "Green"
} catch {
    Write-Log "  ⚠ cainiao_server 健康检查失败: $_" -color "Yellow"
}

try {
    $wpsHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 5 -ErrorAction Stop
    Write-Log "  ✅ wps_server 健康检查通过" -color "Green"
} catch {
    Write-Log "  ⚠ wps_server 健康检查失败: $_" -color "Yellow"
}

# ══════════════════════════════════════════
# 输出状态
# ══════════════════════════════════════════
if ($Daemon) {
    Write-Log "✅ 服务已后台启动" -color "Green"
    Write-Log "   cainiao_server: http://127.0.0.1:58080" -color "Cyan"
    Write-Log "   wps_server:     http://127.0.0.1:8080" -color "Cyan"
    Write-Log "   日志目录: $logDir" -color "Cyan"
    exit 0
}

Clear-Host
Write-Host @"

╔════════════════════════════════════════════╗
║       菜鸟物流查询 — 服务运行中           ║
╚════════════════════════════════════════════╝

"@ -ForegroundColor Cyan

Write-Host "  📍 cainiao_server" -ForegroundColor White
Write-Host "     地址: http://127.0.0.1:58080" -ForegroundColor Cyan
Write-Host "     查询: http://127.0.0.1:58080/query?mailNo=LP00812637173551" -ForegroundColor Cyan
Write-Host ""
Write-Host "  📍 wps_server" -ForegroundColor White
Write-Host "     地址: http://127.0.0.1:8080" -ForegroundColor Cyan
Write-Host "     首页: http://127.0.0.1:8080/" -ForegroundColor Cyan
Write-Host "     WPS 按钮 URL: http://127.0.0.1:8080/?no={物流单号}" -ForegroundColor Cyan
Write-Host ""
Write-Host "  测试查询:" -ForegroundColor White
Write-Host "    curl http://127.0.0.1:58080/query?mailNo=LP00812637173551" -ForegroundColor Gray
Write-Host "    curl http://127.0.0.1:8080/query?no=LP00812637173551" -ForegroundColor Gray
Write-Host ""
Write-Host "  ⚠  关闭此窗口将停止服务" -ForegroundColor Yellow
Write-Host ""

# 前台保持，等待用户退出
try {
    Read-Host "  按 Enter 停止服务"
} finally {
    Write-Log "正在停止服务..." -color "Yellow"
    Kill-ProcessOnPort -Port 58080
    Kill-ProcessOnPort -Port 8080
    Write-Log "服务已停止" -color "Green"
}
