@echo off
chcp 65001 >nul
echo ============================================
echo  菜鸟物流 - 端口转发设置 (80 → 58080)
echo ============================================
echo.
echo ⚠ 请以管理员身份运行本文件！
echo.

:: 先清理旧的转发规则
netsh interface portproxy delete v4tov4 listenport=80 listenaddress=127.0.0.1 >nul 2>&1

:: 添加转发规则：80端口 → 58080端口
netsh interface portproxy add v4tov4 listenport=80 listenaddress=127.0.0.1 connectport=58080 connectaddress=127.0.0.1

if %errorlevel% equ 0 (
    echo ✅ 端口转发设置成功！
    echo    访问 http://localhost/query?mailNo=LP00812637173551
    echo    等同于访问 http://localhost:58080/query?mailNo=...
    echo.
    echo 请确保 cainiao_server.py 已在运行 (双击 start_server.bat)
) else (
    echo ❌ 设置失败，请以管理员身份运行
)

echo.
pause
