@echo off
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
if not exist ".env" (
    echo [提示] 未找到 .env，正在从 .env.example 复制，请填入 API_KEY 后重新运行
    copy ".env.example" ".env" >nul
    pause
    exit /b 1
)
if "%~1"=="" (
    echo 启动天文 Agent 工作台…
    "%PY%" workspace.py --open
) else (
    "%PY%" main.py %*
)
pause
