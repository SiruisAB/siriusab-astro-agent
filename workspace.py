"""Local desktop-style workbench for the existing chat and agent engines."""

import argparse
import asyncio
import os
import queue
import subprocess
import threading
import uuid
import webbrowser
from contextlib import suppress
from pathlib import Path

from aiohttp import WSMsgType, web
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from agent_turn import run_agent_turn
from chatstore import ChatStore
from main import build_agent, build_model, list_model_profiles, session_chat_reply
from workbench_usage import ModelUsage, WorkbenchModel

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "workbench_ui"
MODES = ("chat", "agent", "research", "code")
def _highlight(code, language="", _attrs="", *, filename=""):
    try:
        lexer = (get_lexer_for_filename(filename, code, stripnl=False, ensurenl=False)
                 if filename else get_lexer_by_name(language, stripnl=False, ensurenl=False))
    except ClassNotFound:
        return None
    # Pygments escapes the source before inserting span tags.
    return highlight(code, lexer, HtmlFormatter(nowrap=True, classprefix="syn-"))


MARKDOWN = MarkdownIt("commonmark", {"html": False, "highlight": _highlight}).enable("table")
PREVIEW_BYTES = 64 * 1024
TEXT_EXTENSIONS = {
    ".csv", ".css", ".ecsv", ".html", ".ipynb", ".js", ".json", ".log", ".md",
    ".py", ".r", ".sh", ".sql", ".tex", ".toml", ".ts", ".tsv", ".txt", ".xml",
    ".yaml", ".yml",
}
ATTACH_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".xlsx", ".pptx"}


class TurnDisconnected(Exception):
    pass


class LiveTurn:
    def __init__(self):
        self.events = queue.SimpleQueue()
        self.decisions = queue.SimpleQueue()
        self.closed = threading.Event()

    def emit(self, kind, **payload):
        if self.closed.is_set():
            raise TurnDisconnected
        self.events.put({"kind": kind, **payload})

    def drain(self):
        batch = []
        merged_text = []
        for _ in range(1024):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if merged_text and batch[-1]["kind"] != event["kind"]:
                batch[-1]["text"] = "".join(merged_text)
                merged_text.clear()
            if event["kind"] in {"draft", "delta"}:
                if not merged_text:
                    batch.append(event)
                merged_text.append(event["text"])
            else:
                batch.append(event)
        if merged_text:
            batch[-1]["text"] = "".join(merged_text)
        return batch

    def review(self, request):
        actions = request["action_requests"]
        approval_id = uuid.uuid4().hex
        self.emit("approval", id=approval_id, actions=[
            {"name": action.get("name", "命令"), "command": action.get("args", {}).get("command", "")}
            for action in actions
        ])
        while not self.closed.is_set():
            try:
                decision = self.decisions.get(timeout=0.2)
            except queue.Empty:
                continue
            if decision.get("id") != approval_id:
                continue
            if decision.get("choice") == "approve":
                return [{"type": "approve"} for _ in actions]
            if decision.get("choice") == "reject":
                return [{"type": "reject", "message": "用户拒绝执行这条命令。"} for _ in actions]
        raise TurnDisconnected


def _visible(parts):
    return all(
        not part.startswith((".", "id_rsa", "id_ed25519", "sessions.db", "venv"))
        and part not in {"__pycache__", "node_modules", "conversation_history"}
        and Path(part).suffix not in {".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"}
        and ":" not in part
        for part in (value.lower() for value in parts)
    )


def _workspace_path(root, relative):
    path = Path(relative)
    if path.anchor or ".." in path.parts or not _visible(path.parts):
        raise web.HTTPNotFound(text="文件不在工作区内")
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise web.HTTPNotFound(text="文件不存在") from exc
    if not resolved.is_relative_to(root) or not _visible(resolved.relative_to(root).parts):
        raise web.HTTPNotFound(text="文件不在工作区内")
    return resolved


def _message(message):
    text = str(message.content)
    if isinstance(message, HumanMessage):
        return {"role": "user", "text": text}
    return {"role": "assistant", "html": MARKDOWN.render(text or "（空回复）")}


def _execute_turn(turn, store, sid, mode, profile_name, profile, question, attachments):
    try:
        suffix = "" if profile_name == "default" else f"_{profile_name.upper()}"
        limit = os.getenv(f"CONTEXT_WINDOW{suffix}", "")
        limit = int(limit) if limit.isascii() and limit.isdecimal() and len(limit) <= 9 else None
        stream_usage = os.getenv(f"STREAM_USAGE{suffix}", "1").lower() not in {"0", "no", "false", "off"}
        tracker = ModelUsage(lambda stats: turn.emit("usage", **stats), limit or None)

        def model_factory(**params):
            return build_model(profile, model_class=WorkbenchModel, callbacks=[tracker],
                               stream_usage=stream_usage, **params)

        model = model_factory()
        history = store.load_messages(sid) if sid else []
        sid = sid or store.create_session()
        turn.emit("session", id=sid)
        on_answer = lambda delta: turn.emit("delta", text=delta)
        if mode == "chat":
            ctx = {
                "store": store, "session_id": sid, "chat_history": history,
                "saved_message_count": len(history), "model": model, "model_factory": model_factory,
                "profiles": {profile_name: profile}, "current": profile_name,
                "custom_prompts": {}, "attachments": attachments,
            }
            answer, thinking = session_chat_reply(
                ctx, question, on_answer=on_answer,
                on_draft=lambda delta: turn.emit("draft", text=delta),
            )
        else:
            agent, config = build_agent(model, mode, checkpointer=InMemorySaver())
            config["configurable"]["thread_id"] = f"workbench-{sid}-{uuid.uuid4().hex}"
            answer, thinking = run_agent_turn(
                agent, config, [*history, HumanMessage(content=question)],
                on_answer=on_answer, on_approval=turn.review,
                on_tool=lambda message: turn.emit("tool", name=message.name or "工具", status=message.status),
            )
            store.replace_messages(
                sid, [*history, HumanMessage(content=question), AIMessage(content=answer)],
                first_question=question, expected_count=len(history),
            )
        turn.emit("done", html=MARKDOWN.render(answer or "（空回复）"), thinking=thinking)
    except TurnDisconnected:
        return
    except Exception as exc:
        error = str(exc)
        key = profile.get("api_key")
        if key:
            error = error.replace(key, "[已隐藏]")
        with suppress(TurnDisconnected):
            turn.emit("error", text=error or type(exc).__name__)


async def _read_decisions(socket, turn):
    async for message in socket:
        if message.type is WSMsgType.TEXT:
            try:
                decision = message.json()
            except ValueError:
                continue
            if isinstance(decision, dict) and decision.get("kind") == "decision":
                turn.decisions.put(decision)
    turn.closed.set()


async def _stream_turn(socket, args):
    turn = LiveTurn()
    reader = asyncio.create_task(_read_decisions(socket, turn))
    worker = asyncio.create_task(asyncio.to_thread(_execute_turn, turn, *args))
    try:
        while not turn.closed.is_set():
            for event in turn.drain():
                await socket.send_json(event)
            if worker.done() and turn.events.empty():
                break
            await asyncio.sleep(0.04)
    finally:
        turn.closed.set()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await worker


@web.middleware
async def _local_only(request, handler):
    if request.url.host not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden(text="请从本机打开工作台")
    if request.path == "/api/run" and request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden(text="请求来源无效")
    return await handler(request)


async def _set_headers(request, response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"


def create_app(root=ROOT, profiles=None):
    root = Path(root).resolve(strict=True)
    profiles = list_model_profiles() if profiles is None else profiles
    state_dir = root / ".workbench"
    state_dir.mkdir(exist_ok=True)
    stores = {mode: ChatStore(root / "sessions.db" if mode == "chat" else state_dir / f"{mode}.db") for mode in MODES}
    running = asyncio.Lock()
    routes = web.RouteTableDef()

    def read_mode(request):
        mode = request.query.get("mode", "chat")
        if mode not in stores:
            raise web.HTTPBadRequest(text="未知模式")
        return mode

    @routes.get("/")
    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    @routes.get("/api/state")
    async def state(request):
        mode = read_mode(request)
        return web.json_response({
            "profiles": [{"id": name, "model": profile["model"]} for name, profile in profiles.items()],
            "sessions": await asyncio.to_thread(stores[mode].list_sessions),
            "workspace": root.name,
        })

    @routes.get("/api/sessions/{sid}")
    async def session(request):
        store = stores[read_mode(request)]
        sid = request.match_info["sid"]
        if not await asyncio.to_thread(store.get_title, sid):
            raise web.HTTPNotFound(text="会话不存在")
        messages = await asyncio.to_thread(store.load_messages, sid)
        return web.json_response([_message(message) for message in messages])

    @routes.get("/api/files")
    async def files(request):
        folder = _workspace_path(root, request.query.get("path", ""))
        if not folder.is_dir():
            raise web.HTTPNotFound(text="目录不存在")
        entries = []
        for entry in folder.iterdir():
            relative = entry.relative_to(root)
            try:
                path = _workspace_path(root, relative)
            except web.HTTPNotFound:
                continue
            if path != entry:  # 文件树不跟随链接，避免重复或回环。
                continue
            entries.append({"name": entry.name, "path": relative.as_posix(), "folder": entry.is_dir()})
        return web.json_response(sorted(entries, key=lambda item: (not item["folder"], item["name"].casefold()))[:300])

    @routes.get("/api/preview")
    async def preview(request):
        path = _workspace_path(root, request.query.get("path", ""))
        if not path.is_file():
            raise web.HTTPNotFound(text="文件不存在")
        extension = path.suffix.lower()
        info = {"attach": extension in ATTACH_EXTENSIONS, "text": None, "html": None, "truncated": False}
        if extension in TEXT_EXTENSIONS:
            with path.open("rb") as source:
                data = source.read(PREVIEW_BYTES + 1)
            info["text"] = data[:PREVIEW_BYTES].decode("utf-8-sig", errors="replace")
            info["truncated"] = len(data) > PREVIEW_BYTES
            if extension not in {".csv", ".ecsv", ".log", ".tsv", ".txt"}:
                info["html"] = await asyncio.to_thread(_highlight, info["text"], filename=path.name)
        return web.json_response(info)

    @routes.get("/api/run")
    async def run(request):
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=128 * 1024)
        await socket.prepare(request)
        try:
            data = await socket.receive_json(timeout=10)
            if not isinstance(data, dict):
                raise ValueError("请求格式无效")
            mode, name, sid = data.get("mode"), data.get("profile"), data.get("session")
            question = data.get("text")
            if not isinstance(mode, str) or mode not in stores:
                raise ValueError("未知模式")
            if not isinstance(name, str) or name not in profiles:
                raise ValueError("请先在 .env 配置模型档案和 API_KEY")
            if not isinstance(question, str) or not question.strip() or len(question) > 48_000:
                raise ValueError("请输入 1 至 48000 字符的问题")
            store = stores[mode]
            if sid is not None and (not isinstance(sid, str) or not store.get_title(sid)):
                raise ValueError("会话不存在")
            requested = data.get("attachments", [])
            if (not isinstance(requested, list) or len(requested) > 8 or
                    any(not isinstance(item, str) for item in requested) or (requested and mode != "chat")):
                raise ValueError("仅 Chat 支持附件，每轮最多 8 个")
            attachments = []
            for item in requested:
                path = _workspace_path(root, item)
                if not path.is_file() or path.suffix.lower() not in ATTACH_EXTENSIONS:
                    raise ValueError("附件格式不支持")
                attachments.append(str(path))
            if running.locked():
                raise ValueError("工作台正在处理另一轮请求")
            async with running:
                await _stream_turn(socket, (store, sid, mode, name, profiles[name], question.strip(), attachments))
        except (ValueError, TypeError, asyncio.TimeoutError, web.HTTPException) as exc:
            if not socket.closed:
                await socket.send_json({"kind": "error", "text": getattr(exc, "text", None) or str(exc) or "请求超时"})
        except ConnectionError:
            pass
        finally:
            await socket.close()
        return socket

    app = web.Application(middlewares=[_local_only])
    app.add_routes(routes)
    app.router.add_static("/ui/", ASSETS, show_index=False)
    app.on_response_prepare.append(_set_headers)
    return app


def _open_window(url):
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    if os.name == "nt" and edge.is_file():
        try:
            subprocess.Popen([str(edge), f"--app={url}"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser(description="天文 Agent 工作台")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--open", action="store_true", help="打开工作台窗口")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1 到 65535 之间")
    app = create_app()
    if args.open:
        async def open_when_listening(_app):
            asyncio.get_running_loop().call_later(0.6, _open_window, f"http://127.0.0.1:{args.port}/")
        app.on_startup.append(open_when_listening)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
