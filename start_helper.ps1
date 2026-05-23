<#
.SYNOPSIS
  菜鸟物流查询 — 一键启动辅助脚本
  由 start.bat 调用，无需手动编辑任何配置。
.DESCRIPTION
  1. 启动本地 Python 服务（端口 58080）
  2. 启动 ngrok 公网隧道，自动检测分配的 URL
  3. 自动更新 wps_button_flow.js 中的 PUBLIC_URL
  4. 按任意键退出并清理进程
#>

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ngrokExe  = Join-Path $ScriptDir "ngrok.exe"
$jsFile    = Join-Path $ScriptDir "wps_button_flow.js"
$pyFile    = Join-Path $ScriptDir "cainiao_server.py"
$logFile   = Join-Path $env:TEMP "ngrok_output.txt"

function Cleanup {
    Get-Process -Name "ngrok" -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%cainiao_server%'" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Item $logFile -ErrorAction SilentlyContinue
}

# ====== 清理旧进程 ======
Write-Host "正在初始化..." -ForegroundColor Yellow
Cleanup
Start-Sleep -Seconds 1
Remove-Item $logFile -ErrorAction SilentlyContinue

# ====== 检查必要文件 ======
if (-not (Test-Path $ngrokExe)) { Write-Host "[✗] 找不到 ngrok.exe" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }
if (-not (Test-Path $pyFile))  { Write-Host "[✗] 找不到 cainiao_server.py" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }
if (-not (Test-Path $jsFile))  { Write-Host "[✗] 找不到 wps_button_flow.js" -ForegroundColor Red; Read-Host "按 Enter 退出"; exit 1 }

# ====== 1. 启动本地 Python 服务 ======
Write-Host "▶ 启动本地服务..." -ForegroundColor Yellow
$pythonJob = Start-Process -FilePath "python" -ArgumentList "`"$pyFile`"" -WorkingDirectory $ScriptDir -WindowStyle Minimized -PassThru
Start-Sleep -Seconds 2

if ($pythonJob.HasExited) {
    Write-Host "[✗] Python 服务启动失败" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}
Write-Host "  [✓] Python 服务已启动 (端口 58080)" -ForegroundColor Green

# ====== 2. 启动 ngrok 隧道（临时清除代理，免费版不支持通过代理运行）======
Write-Host "▶ 连接公网隧道..." -ForegroundColor Yellow
# 保存并临时清除代理环境变量，避免 ngrok 免费版被代理拦截
$savedHttpProxy  = $env:HTTP_PROXY
$savedHttpsProxy = $env:HTTPS_PROXY
$env:HTTP_PROXY  = $null
$env:HTTPS_PROXY = $null
try {
    $ngrokJob = Start-Process -FilePath $ngrokExe -ArgumentList "http 58080 --log=stdout" -WorkingDirectory $ScriptDir -NoNewWindow -RedirectStandardOutput $logFile -PassThru
} finally {
    # 立即恢复代理（后续 localhost API 调用不影响）
    $env:HTTP_PROXY  = $savedHttpProxy
    $env:HTTPS_PROXY = $savedHttpsProxy
}

# ====== 3. 等待 ngrok 就绪，通过本地 API 获取公网 URL（最长 30 秒）======
$publicUrl = $null
$maxWait = 30
for ($i = 0; $i -lt $maxWait; $i++) {
    Start-Sleep -Seconds 1
    try {
        $apiResp = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2 -ErrorAction Stop
        $tunnels = $apiResp.tunnels
        if ($tunnels -and $tunnels.Count -gt 0) {
            $publicUrl = $tunnels[0].public_url
            if ($publicUrl) { break }
        }
    } catch { }
    Write-Host "  ." -NoNewline -ForegroundColor Gray
}

Write-Host ""

if (-not $publicUrl) {
    Write-Host "[✗] ngrok 隧道连接失败" -ForegroundColor Red
    Write-Host "    请检查网络连接，或稍后重试" -ForegroundColor Yellow
    Cleanup
    Read-Host "按 Enter 退出"
    exit 1
}

# ====== 4. 构建查询地址（标准 HTTPS URL，不含端口号，兼容 WPS）======
$queryUrl = "$publicUrl/query"

# ====== 5. 自动更新 JS 配置 ======
Write-Host "▶ 更新 JS 配置..." -ForegroundColor Yellow
try {
    $jsContent = Get-Content $jsFile -Raw -Encoding UTF8
    $jsContent = $jsContent -replace '(var PUBLIC_URL\s*=\s*")[^"]+(")', "`$1$queryUrl`$2"
    Set-Content $jsFile -Value $jsContent -Encoding UTF8
    Write-Host "  [✓] 公网地址已写入: $queryUrl" -ForegroundColor Green
} catch {
    Write-Host "[✗] JS 文件更新失败: $_" -ForegroundColor Red
    Cleanup
    Read-Host "按 Enter 退出"
    exit 1
}

# ====== 6. 验证隧道是否可用 ======
Write-Host "▶ 验证隧道..." -ForegroundColor Yellow
try {
    $testUrl = "$publicUrl/health"
    $testResp = Invoke-WebRequest -Uri $testUrl -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
    if ($testResp.StatusCode -eq 200) {
        Write-Host "  [✓] 隧道验证通过" -ForegroundColor Green
    }
} catch {
    Write-Host "  [!] 隧道验证超时，但可能仍可正常使用" -ForegroundColor Yellow
}

# ====== 7. 显示最终状态 ======
Clear-Host
Write-Host @"

╔════════════════════════════════════════════╗
║       菜鸟物流查询服务 — 运行中           ║
╚════════════════════════════════════════════╝

  公网地址: $queryUrl
  本地地址: http://localhost:58080
  JS 配置:  已自动更新 ✓

  使用方式:
  1. 打开 WPS 多维表
  2. 点击「查询物流」按钮执行脚本
  3. 脚本会自动查询所有 LP 开头的单号

  测试链接（浏览器打开验证）:
  ${publicUrl}/query?mailNo=LP00812637173551

"@ -ForegroundColor Cyan

Write-Host "  ⚠  关闭此窗口将自动停止服务" -ForegroundColor Yellow
Write-Host ""
Read-Host "  按 Enter 停止服务"

# ====== 清理退出 ======
Write-Host "正在停止服务..." -ForegroundColor Yellow
Cleanup
Write-Host "服务已停止，再见！" -ForegroundColor Green
