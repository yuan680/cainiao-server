@echo off
chcp 65001 >nul
title 菜鸟物流查询 - 一键安装
cd /d "%~dp0"

echo ============================================
echo  菜鸟物流查询服务 - 一键安装
echo ============================================
echo.
echo  ⚠ 请以管理员身份运行此脚本（右键→以管理员身份运行）
echo.
echo  本脚本将：
echo    1. 设置端口转发 80 → 58080（WPS 仅支持 80 端口）
echo    2. 添加开机自启（后台静默运行）
echo    3. 立即启动服务
echo.
echo  之后你只需：
echo    • 打开 WPS 多维表
echo    • 点击「更新」按钮 ← 即可自动查询
echo.
pause

:: ========== 检查管理员权限 ==========
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 请以管理员身份运行此脚本！
    echo    右键 → 以管理员身份运行
    pause
    exit /b 1
)

:: ========== 1. 设置端口转发 ==========
echo.
echo [1/3] 设置端口转发 80 → 58080 ...
netsh interface portproxy delete v4tov4 listenport=80 listenaddress=* >nul 2>&1
netsh interface portproxy add v4tov4 listenport=80 listenaddress=* connectport=58080 connectaddress=127.0.0.1
if %errorlevel% equ 0 (
    echo   ✅ 端口转发设置成功
) else (
    echo   ⚠ 端口转发设置失败，请检查 80 端口是否被占用
    echo     用管理员身份运行: netstat -ano ^| findstr ":80 "
)

:: ========== 2. 添加开机自启 ==========
echo.
echo [2/3] 添加开机自动启动 ...
set "SERVER_SCRIPT=%~dp0cainiao_server.py"
set "PYTHONW_PATH="

:: 找 pythonw.exe
where pythonw.exe >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where pythonw.exe') do set "PYTHONW_PATH=%%i" & goto found_pythonw
)
:: 找 python.exe 所在目录下的 pythonw.exe
where python.exe >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where python.exe') do set "PYTHON_DIR=%%~dpi"
    if exist "%PYTHON_DIR%pythonw.exe" set "PYTHONW_PATH=%PYTHON_DIR%pythonw.exe"
)

:found_pythonw
if defined PYTHONW_PATH (
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CainiaoTrackServer" /t REG_SZ /d "\"%PYTHONW_PATH%\" \"%SERVER_SCRIPT%\" --port 58080" /f >nul
    echo   ✅ 开机自启已添加
    echo   📎 注册表路径: HKCU\...\Run\CainiaoTrackServer
) else (
    echo   ⚠ 未找到 pythonw.exe，尝试使用 python.exe
    where python.exe >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "delims=" %%i in ('where python.exe') do set "PYTHON_DIR=%%~dpi"
        reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CainiaoTrackServer" /t REG_SZ /d "\"%PYTHON_DIR%python.exe%\" \"%SERVER_SCRIPT%\" --port 58080" /f >nul
        echo   ✅ 开机自启已添加（使用 python.exe 有控制台窗口）
    ) else (
        echo   ❌ 未找到 Python！请先安装 Python 3.8+
        pause
        exit /b 1
    )
)

:: ========== 3. 立即启动服务（后台静默运行） ==========
echo.
echo [3/3] 启动服务 ...
:: 停止已有进程
for /f "tokens=2 delims=," %%i in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
    taskkill /f /pid %%i >nul 2>&1
)

if defined PYTHONW_PATH (
    start /B "" "%PYTHONW_PATH%" "%SERVER_SCRIPT%" --port 58080
) else (
    start /B "" python.exe "%SERVER_SCRIPT%" --port 58080
)

:: 等待 2 秒后检查
timeout /t 2 /nobreak >nul
curl -s http://localhost/health >nul 2>&1
if %errorlevel% equ 0 (
    echo   ✅ 服务已启动并正常运行！
) else (
    :: 再试一次通过 58080
    curl -s http://127.0.0.1:58080/health >nul 2>&1
    if %errorlevel% equ 0 (
        echo   ✅ 服务已启动（58080 端口）
    ) else (
        echo   ⚠ 服务可能尚未完全启动，请稍后检查
    )
)

:: ========== 完成 ==========
echo.
echo ============================================
echo  ✅ 安装完成！
echo ============================================
echo.
echo  现在你可以：
echo    1. 打开 WPS 多维表格
echo    2. 点击「更新」按钮 ← 直接出结果
echo    3. 无需再手动运行任何脚本
echo.
echo  🔄 下次开机也会自动运行，无需任何操作
echo.
echo  ❌ 如需卸载，请运行: uninstall.bat（以管理员身份）
echo.
pause
