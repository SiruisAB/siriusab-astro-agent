"""浏览器版启动器（阶段三）：把对话界面投到浏览器，Cherry Studio 的"壳"。

用法：
  ./.conda/python.exe serve.py                     # http://localhost:8000
  ./.conda/python.exe serve.py --port 8080
  ./.conda/python.exe serve.py --mode research     # 启动即进入指定模式

每个浏览器标签页是一个独立的界面进程（web driver），会话数据共享 sessions.db
——在浏览器里点侧栏切换会话、跑流式、审批 shell，与终端里完全一致。
依赖：pip install textual-serve（aiohttp 等会随之安装）。
"""

import argparse

from textual_serve.server import Server

from main import MODE_CONFIGS


def main() -> None:
    parser = argparse.ArgumentParser(description="天文 Agent 浏览器版")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", default="chat", choices=list(MODE_CONFIGS),
                        help="启动时进入的模式（默认 chat）")
    args = parser.parse_args()

    import sys
    from pathlib import Path

    py = sys.executable
    app_path = Path(__file__).resolve().parent / "main.py"
    Server(
        f'"{py}" "{app_path}" --mode {args.mode}',
        host=args.host,
        port=args.port,
        title="天文 Agent",
    ).serve()


if __name__ == "__main__":
    main()
