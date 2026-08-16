@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
rem 本机 python.exe 路径：默认走 PATH 里的 python；请改成你自己的完整路径
set "PY=python"
cd /d "%~dp0"
"%PY%" make_cert.py %*
echo.
pause
