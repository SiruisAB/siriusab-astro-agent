@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
rem 兼容两种环境布局：conda 环境的 python.exe 在 .conda 根目录，venv 的在 .conda\Scripts 下
set "PY=.conda\python.exe"
if not exist "%PY%" set "PY=.conda\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到 .conda 环境（.conda\python.exe 或 .conda\Scripts\python.exe），请先执行：
    echo   conda create -p .conda -y python=3.12
    echo   .\.conda\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
if "%~1"=="" goto workbench
if /i "%~1"=="--port" goto workbench
if /i "%~1"=="--open" goto workbench
if /i "%~1"=="--no-warmup" goto workbench
if /i "%~1"=="--help" goto workbench
if not exist ".env" if not exist ".workbench\settings.json" (
    echo [提示] 未找到 .env，正在从 .env.example 复制，请填入 API_KEY 后重新运行
    copy ".env.example" ".env" >nul
    pause
    exit /b 1
)
"%PY%" main.py %*
goto finished

:workbench
echo 启动天文 Agent 工作台，已有服务会直接复用…
"%PY%" workspace.py --open %*

:finished
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
exit /b %RC%
