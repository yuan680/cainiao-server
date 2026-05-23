<#
.SYNOPSIS
  菜鸟物流查询 — 一键启动辅助脚本
  由 start.bat 调用，无需手动编辑任何配置。
.DESCRIPTION
  Remote 模式（默认）：直接连接 Render 线上服务，无需本地 Python 进程。
  Local 模式 （-Mode local）：启动本地 Python 服务 + Cloudflare 隧道。
  两种模式都会自动更新 wps_button_flow.js 中的 PUBLIC_URL。
#>

param(
    [ValidateSet("remote","local")]
    [string]$Mode = "remote"
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cloudflaredExe = "C:\Users\Administrator\cloudflared.exe"
$jsFile    = Join-Path $ScriptDir "wps_button_flow.js"
$pyFile    = Join-Path $ScriptDir "cainiao_server.py"

# ─── 远程（Render）和本地地址 ───
$remoteUrl  = "https://cainiao-server.onrender.com/query"
$localUrl   = "https://cainiaotrack.xyz/query"

function CleanupLocal {
    Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%cainiao_server%'" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# ====== 按模式选择连接地址 ======
if ($Mode -eq "remote") {
    $queryUrl = $remoteUrl
    Write-Host "▶ 模式：远程（Render 线上服务）" -ForegroundColor Green
} else {
    $queryUrl = $localUrl
    Write-Host "▶ 模式：本地（Python + Cloudflare 隧道）" -ForegroundColor Cyan
}

# ====== 检查必要文件 ======
if (-not (Test-Path $jsFile))  { Write-Host "[✗] 找不到 wps_button_flow.js" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }
if ($Mode -eq "local") {
    if (-not (Test-Path $cloudflaredExe)) { Write-Host "[✗] 找不到 cloudflared.exe" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }
    if (-not (Test-Path $pyFile))  { Write-Host "[✗] 找不到 cainiao_server.py" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }
}

# ====== 1. 自动更新 JS 配置 ======
Write-Host "▶ 更新 JS 配置..." -ForegroundColor Yellow
try {
    $jsContent = Get-Content $jsFile -Raw -Encoding UTF8
    $jsContent = $jsContent -replace '(var PUBLIC_URL\s*=\s*")[^"]+(")', "`$1$queryUrl`$2"
    Set-Content $jsFile -Value $jsContent -Encoding UTF8
    Write-Host "  [✓] 公网地址已写入: $queryUrl" -ForegroundColor Green
} catch {
    Write-Host "[✗] JS 文件更新失败: $_" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

# ====== 2. 验证远程服务（Remote 模式）=====
if ($Mode -eq "remote") {
    Write-Host "▶ 验证 Render 服务..." -ForegroundColor Yellow
    try {
        $testResp = Invoke-WebRequest -Uri "${remoteUrl}?mailNo=LP00812637173551" -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
        if ($testResp.StatusCode -eq 200) {
            Write-Host "  [✓] Render 服务正常响应" -ForegroundColor Green
        }
    } catch {
        Write-Host "  [!] Render 验证请求失败: $_" -ForegroundColor Yellow
        Write-Host "  [!] 请确认 https://cainiao-server.onrender.com/health 能否正常访问" -ForegroundColor Yellow
    }
}

# ====== 3. 本地模式：启动本地 Python 服务 ======
if ($Mode -eq "local") {
    # 清理旧进程
    Write-Host "▶ 正在初始化..." -ForegroundColor Yellow
    CleanupLocal
    Start-Sleep -Seconds 1

    Write-Host "▶ 启动本地服务..." -ForegroundColor Yellow
    $pythonJob = Start-Process -FilePath "python" -ArgumentList "`"$pyFile`"" -WorkingDirectory $ScriptDir -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 2

    if ($pythonJob.HasExited) {
        Write-Host "[✗] Python 服务启动失败" -ForegroundColor Red
        Read-Host "按 Enter 退出"
        exit 1
    }
    Write-Host "  [✓] Python 服务已启动 (端口 58080)" -ForegroundColor Green

    # 启动 Cloudflare 隧道
    Write-Host "▶ 连接公网隧道..." -ForegroundColor Yellow
    $cfJob = Start-Process -FilePath $cloudflaredExe -ArgumentList "tunnel run cainiao-track" -WindowStyle Minimized -PassThru

    # 等待本地服务就绪（最长 30 秒）
    Write-Host "  ⏳ 等待服务就绪..." -ForegroundColor Yellow
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:58080/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Write-Host "  ." -NoNewline -ForegroundColor Gray
    }
    Write-Host ""

    if (-not $ready) {
        Write-Host "[✗] 本地服务未就绪" -ForegroundColor Red
        CleanupLocal
        Read-Host "按 Enter 退出"
        exit 1
    }

    # 验证隧道
    Write-Host "▶ 验证隧道..." -ForegroundColor Yellow
    try {
        $testResp = Invoke-WebRequest -Uri "https://cainiaotrack.xyz/health" -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        if ($testResp.StatusCode -eq 200) {
            Write-Host "  [✓] 隧道验证通过" -ForegroundColor Green
        }
    } catch {
        Write-Host "  [!] 隧道正在传播 DNS，稍后即可使用" -ForegroundColor Yellow
    }
}

# ====== 4. 显示最终状态 ======
Clear-Host
Write-Host @"

╔════════════════════════════════════════════╗
║       菜鸟物流查询服务 — 配置完成         ║
╚════════════════════════════════════════════╝

  模式:      $(if ($Mode -eq "remote") { "远程（Render）" } else { "本地+隧道" })
  公网地址:  $queryUrl
  JS 配置:   已自动更新 ✓

  使用方式:
  1. 打开 WPS 多维表
  2. 点击「查询物流」按钮执行脚本
  3. 脚本会自动查询所有 LP 开头的单号

  测试链接（浏览器打开验证）:
  $(($queryUrl -replace '/query$','') + '/query?mailNo=LP00812637173551')

"@ -ForegroundColor Cyan

if ($Mode -eq "remote") {
    Write-Host "  ℹ  无需关闭本窗口，修改 JS 后可直接在 WPS 中运行" -ForegroundColor Green
} else {
    Write-Host "  ⚠  关闭此窗口将自动停止本地服务" -ForegroundColor Yellow
}
Write-Host ""
Read-Host "  按 Enter 停止服务"

# ====== 清理退出 ======
Write-Host "正在停止服务..." -ForegroundColor Yellow
Cleanup
Write-Host "服务已停止，再见！" -ForegroundColor Green
