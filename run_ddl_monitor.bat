@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 手机 DDL 检索

set "PYTHONUTF8=1"
set "PYTHON="

rem 按相对路径寻找虚拟环境，换机器也能用；都没有时退回 PATH 上的 python
if exist "%~dp0venv\Scripts\python.exe" set "PYTHON=%~dp0venv\Scripts\python.exe"
if not defined PYTHON if exist "%~dp0venv312\Scripts\python.exe" set "PYTHON=%~dp0venv312\Scripts\python.exe"
if not defined PYTHON if exist "%~dp0..\venv\Scripts\python.exe" set "PYTHON=%~dp0..\venv\Scripts\python.exe"
if not defined PYTHON if exist "%~dp0..\venv312\Scripts\python.exe" set "PYTHON=%~dp0..\venv312\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到可用的 Python。
    echo 下一步：安装 Python 3.10 以上版本，或在项目目录创建 venv 后重新运行：
    echo     python -m venv venv
    echo     venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 2
)

if not exist "sources.yaml" (
    echo [提示] 还没有 sources.yaml，程序会提示如何添加检索目标。
    echo.
)

rem 双击后由启动器询问：1 = 仅扫描一次，2 = 定期
rem 也可以在本文件后面跟参数，例如：run_ddl_monitor.bat --mode once
"%PYTHON%" -m ddl_agent.monitor.launcher %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" if not "%CODE%"=="-1073741510" echo 程序退出，代码：%CODE%
pause
endlocal
exit /b %CODE%
