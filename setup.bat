@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem Find a Python 3.11+ : py launcher, then Anaconda, then python on PATH
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"

py -3 -c "%CHECK%" >nul 2>nul && (py -3 install.py %* & goto :end)

if exist "%USERPROFILE%\anaconda3\python.exe" (
  "%USERPROFILE%\anaconda3\python.exe" -c "%CHECK%" >nul 2>nul && ("%USERPROFILE%\anaconda3\python.exe" install.py %* & goto :end)
)

python -c "%CHECK%" >nul 2>nul && (python install.py %* & goto :end)

echo.
echo [X] 没有找到 Python 3.11 或更新版本。
echo     请到 https://www.python.org/downloads/ 安装（安装时勾选 Add python.exe to PATH），再双击本文件。

:end
echo.
pause
