@echo off
chcp 65001 >nul
title 菜鸟物流查询 - 卸载
cd /d "%~dp0"

echo ============================================
echo  菜鸟物流查询服务 - 卸载
echo ============================================
echo.
echo  ⚠ 请以管理员身份运行此脚本
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 请以管理员身份运行此脚本！
    pause
    exit /b 1
)

:: 1. 停止服务进程
echo [1/3] 停止服务进程 ...
for /f "tokens=2 delims=," %%i in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
    taskkill /f /pid %%i >nul 2>&1
)
for /f "tokens=2 delims=," %%i in ('tasklist /fi "imagename eq python.exe" /fo csv /nh 2^>nul ^| findstr /i "cainiao_server"') do (
    taskkill /f /pid %%i >nul 2>&1
)
echo   ✅ 服务进程已停止

:: 2. 移除端口转发
echo [2/3] 移除端口转发 ...
netsh interface portproxy delete v4tov4 listenport=80 listenaddress=* >nul 2>&1
echo   ✅ 端口转发已移除

:: 3. 移除开机自启
echo [3/3] 移除开机自启 ...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CainiaoTrackServer" /f >nul 2>&1
echo   ✅ 开机自启已移除

:: 完成
echo.
echo ============================================
echo  ✅ 已完全卸载！
echo ============================================
echo.
echo  如需重新安装，请以管理员身份运行 install.bat
echo.
pause
