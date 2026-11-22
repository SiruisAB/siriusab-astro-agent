@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".conda\Scripts\python.exe" (
    echo [错误] 未找到 .conda 环境，请先执行：
    echo   E:\anaconda3\python.exe -m venv .conda  或  conda create -p .conda -y python=3.12
    echo   .\.conda\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
if not exist ".env" (
    echo [提示] 未找到 .env，正在从 .env.example 复制，请填入 API_KEY 后重新运行
    copy ".env.example" ".env" >nul
    pause
    exit /b 1
)
echo 启动天文 Agent（对话界面，ctrl+q 退出）...
".conda\Scripts\python.exe" main.py %*
pause
