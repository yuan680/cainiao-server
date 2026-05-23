@echo off
chcp 65001 >nul
echo ============================================
echo  移除端口转发 (80 → 58080)
echo ============================================
echo.
echo ⚠ 请以管理员身份运行本文件！
echo.
netsh interface portproxy delete v4tov4 listenport=80 listenaddress=127.0.0.1
echo ✅ 已移除端口转发规则
pause
