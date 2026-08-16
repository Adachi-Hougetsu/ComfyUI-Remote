@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set APPDIR=%~dp0
rem 本机 python.exe 路径：默认走 PATH 里的 python；秋叶整合包等请改成你自己的完整路径
set "PY=python"
rem 优先使用项目内 .venv（如已创建）；不存在则回退 PATH 里的 python
if exist "%APPDIR%.venv\Scripts\python.exe" set "PY=%APPDIR%.venv\Scripts\python.exe"

echo [1/2] 安装依赖...
"%PY%" -m pip install -q -r "%APPDIR%requirements.txt"

set "SSL_ARGS="
set "FORCE_SSL="
if /i "%~1"=="https" set "FORCE_SSL=1"
if defined FORCE_SSL if exist "%APPDIR%certs\server.crt" if exist "%APPDIR%certs\server.key" (
    set "SSL_ARGS=--ssl-certfile "%APPDIR%certs\server.crt" --ssl-keyfile "%APPDIR%certs\server.key""
    echo [2/2] 启动控制层 HTTPS：https://你的IP:8000（IP 用 ipconfig 查看）
    echo       日常调试请用普通 run.bat（HTTP）；HTTPS 只在最后装 PWA 时用，详见 docs\安卓安装态.md
) else (
    echo [2/2] 启动控制层 HTTP：http://你的IP:8000（IP 用 ipconfig 查看）
    echo       日常开发调试用这个模式（手机直接开 http://你的IP:8000）。
    echo       安卓要安装 PWA（加到主屏幕）：先运行 make_cert.bat 生成证书，
    echo       再开一次 http://你的IP:8000/ca.crt 给手机装根证书，最后 run.bat https 走 HTTPS
)

cd /d "%APPDIR%"
"%PY%" -m uvicorn server:app --host 0.0.0.0 --port 8000 --app-dir "%APPDIR%" %SSL_ARGS%
pause
