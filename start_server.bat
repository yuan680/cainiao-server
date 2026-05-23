@echo off
chcp 65001 >nul
title 菜鸟物流查询本地服务
cd /d "%~dp0"

echo ============================================
echo  菜鸟物流查询本地服务启动中...
echo ============================================
echo.
echo  服务端口: 58080  (cainiao_server.py)
echo.
echo  注意: WPS AirScript 需要 80 端口转发
echo        请以管理员身份运行 setup_portforward.bat
echo        或手动运行: netsh interface portproxy add v4tov4 ^
echo                     listenport=80 listenaddress=127.0.0.1 ^
echo                     connectport=58080 connectaddress=127.0.0.1
echo.
echo  WPS 配置地址:
echo     http://localhost/query?mailNo={快递单号}
echo.
echo  按 Ctrl+C 停止服务
echo.

:: 查找 Python
set PYTHON_CMD=python
where py >nul 2>nul && set PYTHON_CMD=py -3

:: 启动主服务 (58080) — 无需管理员
start "Cainiao-Main" /MIN cmd /c "%PYTHON_CMD% cainiao_server.py --port 58080"

echo  🟢 主服务已启动 (58080)
echo.
echo  测试: http://localhost:58080/query?mailNo=LP00812637173551
echo.
echo  如果已配置端口转发，也可测试:
echo  http://localhost/query?mailNo=LP00812637173551
echo.
pause
echo.

:: 保持窗口打开
pause >nul
