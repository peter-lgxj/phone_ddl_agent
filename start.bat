@echo off
chcp 65001 >nul
title 手机智能体

echo ========================================
echo         手机智能体 启动中...
echo ========================================
echo.

:: 检查 ADB 设备
echo [1/3] 检查手机连接...
adb devices | findstr /v "List" | findstr "device" >nul
if errorlevel 1 (
    echo [警告] 未检测到手机，请连接手机并开启USB调试后重试
    pause
    exit /b
)
echo [OK] 手机已连接
echo.

:: 激活 conda 环境
echo [2/3] 激活虚拟环境 mobile_agent...
call conda activate mobile_agent
if errorlevel 1 (
    echo [错误] 激活环境失败，请确认 conda 和 mobile_agent 环境已安装
    pause
    exit /b
)
echo [OK] 环境已激活
echo.

:: 启动程序
echo [3/3] 启动服务...
echo.
echo 浏览器访问: http://localhost:5000
echo 按 Ctrl+C 停止服务
echo ========================================
echo.

cd /d %~dp0
python app.py

pause