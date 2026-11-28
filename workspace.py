"""Local desktop-style workbench for the existing chat and agent engines."""

import argparse
import asyncio
import os
import queue
import subprocess
import threading
import webbrowser
from contextlib import suppress
from pathlib import Path

from aiohttp import WSMsgType, web
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from chatstore import ChatHistoryChanged, ChatStore
from compact import compact_focus, compact_session
from model_profiles import environment_model_profiles
from workbench_settings import SettingsChanged, WorkbenchSettings
from model_endpoints import model_identity
from runtime_loader import RUNTIME
from hitl import normalize_interrupt, pending_interrupts, resume_values, validate_response
from taskstore import TaskStore
from knowledge_store import KnowledgeChanged, KnowledgeStore
from memory_retrieval import MemorySearch, resolve_source
from prompt_layers import prepare_retrieval, retrieval_message
from subagent_store import SubagentStore
from approval_policy import file_fingerprint, file_preview
from skill_store import MAX_ARCHIVE, SkillChanged, SkillStore, import_official, official_catalog
from web_advisor import WebAdvisorStore
from web_browser_controls import connection_status, open_login_browser, view_job
from turn_control import TurnControl, TurnStopped
from experiment_store import ExperimentStore, ExperimentChanged, digest as experiment_digest, encoded as experiment_json

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "workbench_ui"
MODES = ("chat", "agent", "research", "code")


def build_model(*args, **kwargs):
    from main import build_model as build
    return build(*args, **kwargs)


def build_agent(*args, **kwargs):
    from main import build_agent as build
    return build(*args, **kwargs)


def session_chat_reply(*args, **kwargs):
    from main import session_chat_reply as reply
    return reply(*args, **kwargs)


def run_agent_turn(*args, **kwargs):
    from agent_turn import run_agent_turn as run
    return run(*args, **kwargs)


def interaction_answers(messages):
    from agent_turn import interaction_answers as extract
    return extract(messages)


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
        self.save_pending = None
        self.before_resume = None
        self.cached_answers = {}
        self.cached_requests = {}
        self.root_dir = None
        self.control = TurnControl(self.emit)
        self.answer_parts = []
        self.draft_parts = []
        self.explicit_stop = False

    def _prepare_files(self, request):
        for action in request.get("actions", []):
            if self.root_dir and action["name"] in {"write_file", "edit_file"}:
                try:
                    action["file_hash"] = file_fingerprint(self.root_dir, action["args"]["file_path"])
                    action["description"] = file_preview(self.root_dir, action["name"], action["args"])
                except (OSError, ValueError):
                    action["file_hash"] = "unavailable"

    def _files_unchanged(self, request, response):
        if not self.root_dir or request["type"] != "approval":
            return True
        decisions = response.get("decisions", []) if request["protocol"] == "batched" else [response]
        for action, decision in zip(request["actions"], decisions):
            if action["name"] not in {"write_file", "edit_file"} or decision["type"] not in {"approve", "edit"}:
                continue
            args = decision["edited_action"]["args"] if decision["type"] == "edit" else action["args"]
            try:
                # Validate the edited target even if the reviewer selected a new path.
                current = file_fingerprint(self.root_dir, args["file_path"])
                if args["file_path"] == action["args"]["file_path"] and current != action.get("file_hash"):
                    return False
            except (OSError, ValueError):
                return False
        return True

    def emit(self, kind, **payload):
        if self.closed.is_set():
            raise TurnDisconnected
        if kind in {"delta", "draft"}:
            self.control.check()
            (self.answer_parts if kind == "delta" else self.draft_parts).append(payload["text"])
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

    def review_interrupts(self, items):
        requests = [normalize_interrupt(item, index) for index, item in enumerate(items)]
        answers = {}
        def direction_changed():
            if not self.control.has_pending():
                return None
            values = []
            for request in requests:
                if request["type"] == "approval" and all("reject" in action["allowed_decisions"] for action in request["actions"]):
                    value = {"decisions": [{"type":"reject", "message":"用户补充了新方向，取消旧审批请求，按最新指令重新规划。"} for action in request["actions"]]}
                    if request["protocol"] == "per_call": value = value["decisions"][0]
                elif request["type"] == "question":
                    with self.control.lock:
                        text = "\n".join(item["text"] for item in self.control.pending)
                    value = {"answer":"[用户补充任务方向，未选择原问题选项]\n" + text}
                elif request["type"] == "confirmation":
                    value = {"approved":False, "reason":"用户补充任务方向，取消原确认请求"}
                else:
                    return None
                values.append(validate_response(request, value))
            self.emit("decision_invalidated", ids=[request["id"] for request in requests], text="已取消旧审批/问题，补充指令将在下个模型步骤生效；没有批准操作。")
            if self.before_resume: self.before_resume()
            return resume_values(items, values)
        self.control.check()
        changed = direction_changed()
        if changed is not None:
            return changed
        for request in requests:
            if request["id"] in self.cached_answers:
                try:
                    value = validate_response(request, self.cached_answers[request["id"]])
                    original = self.cached_requests.get(request["id"], request)
                    if self._files_unchanged(original, value):
                        answers[request["id"]] = value
                except ValueError:
                    pass
            self._prepare_files(request)
        if self.save_pending is not None:
            self.save_pending([{**item, **({"response": answers[item["id"]]} if item["id"] in answers else {})}
                               for item in requests])
        for request in requests:
            if request["id"] not in answers:
                self.emit("approval" if request["type"] == "approval" else "question", **request)
        if len(answers) == len(requests):
            if self.before_resume:
                self.before_resume()
            return resume_values(items, [answers[request["id"]] for request in requests])
        while not self.closed.is_set():
            self.control.check()
            changed = direction_changed()
            if changed is not None:
                return changed
            try:
                decision = self.decisions.get(timeout=0.2)
            except queue.Empty:
                continue
            request = next((request for request in requests if request["id"] == decision.get("id")), None)
            if request is None or request["id"] in answers:
                continue
            response = decision.get("value")
            # Accept the previous all-approve/all-reject protocol for existing clients.
            if response is None and request["type"] == "approval" and decision.get("choice") in {"approve", "reject"}:
                response = [{"type": decision["choice"], "message": "用户拒绝执行；不要重试。"}
                            if decision["choice"] == "reject" else {"type": "approve"} for _ in request["actions"]]
            try:
                value = validate_response(request, response)
                if not self._files_unchanged(request, value):
                    self._prepare_files(request)
                    self.emit("approval", **request)
                    raise ValueError("目标文件已变化或路径无法访问，修改未提交；请核对参数并重新审核。")
                answers[request["id"]] = value
            except (ValueError, TypeError) as exc:
                self.emit("decision_error", id=request["id"], text=str(exc))
                continue
            self.emit("decision_saved", id=request["id"])
            if self.save_pending:
                self.save_pending([{**item, **({"response": answers[item["id"]]} if item["id"] in answers else {})}
                                   for item in requests])
            if len(answers) == len(requests):
                stale = [item for item in requests if not self._files_unchanged(item, answers[item["id"]])]
                if stale:
                    for item in stale:
                        answers.pop(item["id"])
                        self._prepare_files(item)
                        self.emit("approval", **item)
                        self.emit("decision_error", id=item["id"], text="文件已变化，请重新审核。")
                    if self.save_pending:
                        self.save_pending([{**item, **({"response": answers[item["id"]]} if item["id"] in answers else {})} for item in requests])
                    continue
                if self.before_resume:
                    self.before_resume()
                return resume_values(items, [answers[request["id"]] for request in requests])
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
    from langchain_core.messages import HumanMessage

    text = str(message.content)
    if isinstance(message, HumanMessage):
        return {"role": "user", "text": text}
    return {"role": "assistant", "html": MARKDOWN.render(text or "（空回复）")}


def _execute_turn(turn, store, sid, mode, profile_name, profile, question, attachments, tasks=None, resume=False, knowledge=None):
    with turn.control.activate():
        try:
            return _execute_turn_impl(turn, store, sid, mode, profile_name, profile, question, attachments, tasks, resume, knowledge)
        finally:
            with turn.control.lock:
                turn.control.accepting = False
                pending = list(turn.control.pending)
            if pending:
                with suppress(TurnDisconnected): turn.emit("steer_unapplied", items=pending)


def _new_supplements(history, trace, control):
    from langchain_core.messages import HumanMessage
    known = {message.additional_kwargs.get("supplement_id") for message in history}
    result = []
    for message in [*trace, *control.applied_messages()]:
        identifier = message.additional_kwargs.get("supplement_id")
        if isinstance(message, HumanMessage) and identifier and identifier not in known:
            result.append(message)
            known.add(identifier)
    return result


def _execute_turn_impl(turn, store, sid, mode, profile_name, profile, question, attachments, tasks=None, resume=False, knowledge=None):
    record = None
    agent = None
    user_replies = []
    initial_history_count = None
    trace = []
    try:
        RUNTIME.wait_until_ready()
        turn.control.check()
        from langchain_core.messages import AIMessage, HumanMessage
        from workbench_usage import ModelUsage, WorkbenchModel

        suffix = "" if profile_name == "default" else f"_{profile_name.upper()}"
        limit = str(profile.get("context_window") or os.getenv(f"CONTEXT_WINDOW{suffix}", ""))
        limit = int(limit) if limit.isascii() and limit.isdecimal() and len(limit) <= 9 else None
        stream_usage = os.getenv(f"STREAM_USAGE{suffix}", "1").lower() not in {"0", "no", "false", "off"}
        if profile.get("stream_usage") is not None:
            stream_usage = profile["stream_usage"]
        tracker = ModelUsage(lambda stats: turn.emit("usage", **stats), limit or None)

        def model_factory(**params):
            params.setdefault("streaming", True)
            return build_model(profile, model_class=WorkbenchModel, callbacks=[tracker],
                               stream_usage=stream_usage, **params)

        model = model_factory()
        history = store.load_messages(sid) if sid else []
        initial_history_count = len(history)
        focus = compact_focus(question)
        if focus is not None:
            turn.emit("status", text="正在压缩上下文…")
            result = compact_session(store, sid, model, focus)
            turn.emit("compact", **result, html=MARKDOWN.render(result["text"]))
            return
        if sid:
            history, context, prefix, memory_token = store.capture_context(sid)
        else:
            context, prefix, memory_token = history, 0, None
        sid = sid or store.create_session()
        turn.emit("session", id=sid)
        on_answer = lambda delta: turn.emit("delta", text=delta)
        if mode == "chat":
            ctx = {
                "store": store, "session_id": sid, "chat_history": context,
                "context_prefix_count": prefix,
                "saved_message_count": len(history), "model": model, "model_factory": model_factory,
                "memory_token": memory_token,
                "profiles": {profile_name: profile}, "current": profile_name,
                "custom_prompts": {}, "attachments": attachments,
                "knowledge": knowledge,
                "on_subagent": lambda event: turn.emit("subagent", **event),
                "project_dir": tasks.root if tasks else Path(store._path).parent,
                "selected_skills": profile.get("_skills", []),
                "on_skill": lambda event: turn.emit("skill", **event),
                "retrieval_budget": min(6000, limit // 4) if limit else 6000,
                "on_retrieval": lambda data: turn.emit("retrieval", references=data["references"], engine=data["engine"],
                                                       knowledge_count=data["knowledge_count"], warnings=data["warnings"]),
            }
            answer, thinking = session_chat_reply(
                ctx, question, on_answer=on_answer,
                on_draft=lambda delta: turn.emit("draft", text=delta),
            )
        else:
            tasks = tasks or TaskStore(ROOT)
            with tasks.execution_lock(), tasks.checkpoint() as checkpointer:
                if resume:
                    record = tasks.get(mode, sid)
                    if not record or record["status"] != "waiting":
                        raise ValueError("当前没有等待恢复的审批或问题。")
                    if model_identity(record["model"]) != model_identity(profile):
                        raise ValueError("此任务的模型配置已改变，请恢复原模型档案后继续。")
                    turn.cached_answers = {request["id"]: request["response"] for request in record["pending"] if "response" in request}
                    turn.cached_requests = {request["id"]: request for request in record["pending"]}
                else:
                    retrieved = prepare_retrieval(store, sid, mode, question, knowledge=knowledge, prefix=prefix,
                                                 budget=min(6000, limit // 4) if limit else 6000)
                    if retrieved["text"]:
                        context = [*context, retrieval_message(retrieved["text"])]
                        turn.emit("retrieval", references=retrieved["references"], engine=retrieved["engine"],
                                  knowledge_count=retrieved["knowledge_count"], warnings=retrieved["warnings"])
                    identity = model_identity(profile)
                    if profile.get("_skills"):
                        identity["skills"] = profile["_skills"]
                    if profile["model"].casefold() == "qwen3.8-27b-coder390":
                        identity["reasoning_effort"] = profile.get("reasoning_effort") or "none"
                    record = tasks.start(mode, sid, question, profile_name,
                                         identity, len(history) + 1)
                    store.replace_messages(sid, [*history, HumanMessage(content=question)],
                                           first_question=question, expected_count=len(history), expected_token=memory_token)
                agent, config = build_agent(model, mode, checkpointer=checkpointer, root_dir=tasks.root,
                                            on_subagent=lambda event: turn.emit("subagent", **event),
                                            skill_scope=record["run_id"], selected_skills=profile.get("_skills", []),
                                            on_skill=lambda event: turn.emit("skill", **event),
                                            on_web_advisor=lambda event: turn.emit("web_advisor", **event),
                                            cancelled=lambda: turn.closed.is_set() or turn.control.stopped.is_set(), turn_control=turn.control,
                                            on_experiment=lambda event: turn.emit("experiment", **event))
                turn.root_dir = tasks.root
                config["configurable"]["thread_id"] = record["thread_id"]
                config["configurable"].update({"conversation_id": sid, "memory_db": store._path,
                                               "run_id": record["run_id"]})
                store.import_legacy_blocks(sid, tasks.root / "results" / "context_blocks.db",
                                           f"workbench-{mode}-{sid}-")
                turn.save_pending = lambda requests: tasks.update(mode, sid, record["run_id"], "waiting", pending=requests)
                turn.before_resume = lambda: tasks.update(mode, sid, record["run_id"], "running")
                try:
                    answer, thinking = run_agent_turn(
                        agent, config, None if resume else [*context, HumanMessage(content=question)],
                        on_answer=on_answer, on_interrupts=turn.review_interrupts,
                        on_tool=lambda message: turn.emit("tool", name=message.name or "工具", status=message.status),
                        turn_control=turn.control,
                    )
                finally:
                    # Checkpoints remain authoritative for task recovery; this immutable
                    # snapshot makes the full tool trace available to later session runs.
                    trace = agent.get_state(config).values.get("messages", [])
                    if trace:
                        store.archive_messages(sid, trace, run_id=record["run_id"], thread_id=record["thread_id"])
                replies = interaction_answers(agent.get_state(config).values.get("messages", []))
                user_replies = [message.content for message in replies]
                history = store.load_messages(sid)
                turn.control.check()
                store.replace_messages(sid, [*history, *_new_supplements(history, trace, turn.control), *replies, AIMessage(content=answer)], expected_count=record["expected_count"])
                tasks.update(mode, sid, record["run_id"], "complete")
        turn.control.check()
        turn.emit("done", html=MARKDOWN.render(answer or "（空回复）"), thinking=thinking, user_replies=user_replies)
    except (TurnStopped, TurnDisconnected):
        # Do not roll back completed tools or auto-resume abandoned approvals.
        current_record = tasks.get(mode, sid) if record and tasks else None
        preserve_waiting = not turn.explicit_stop and current_record and current_record["status"] == "waiting"
        if sid and not preserve_waiting:
            with suppress(Exception):
                from langchain_core.messages import AIMessage, HumanMessage
                saved = store.load_messages(sid)
                chat_already_saved = mode == "chat" and initial_history_count is not None and len(saved) > initial_history_count
                if not chat_already_saved and (mode == "chat" or record is None):
                    saved = [*saved, HumanMessage(content=question)]
                note = ("[用户已停止本轮；" if turn.explicit_stop else "[连接已中断，本轮未完成；") + "以下为未完成内容，不是最终答案。已执行操作不会自动撤销。]"
                partial = "".join(turn.answer_parts)
                if partial: note += "\n" + partial
                with turn.control.lock:
                    pending_messages = [HumanMessage(content="[中止时尚未应用的补充指令]\n" + item["text"], additional_kwargs={"user_supplement":True, "unapplied":True}) for item in turn.control.pending]
                applied = [] if chat_already_saved else _new_supplements(saved, trace, turn.control)
                store.replace_messages(sid, [*saved, *applied, AIMessage(content=note, additional_kwargs={"interrupted":True}), *pending_messages], expected_count=len(store.load_messages(sid)))
                if turn.draft_parts:
                    store.archive_messages(sid, [AIMessage(content="".join(turn.draft_parts), additional_kwargs={"interrupted":True})], kind="interrupted_draft")
        if record and not preserve_waiting:
            with suppress(Exception): tasks.update(mode, sid, record["run_id"], "cancelled")
        with turn.control.lock:
            unsent = list(turn.control.pending)
        with suppress(TurnDisconnected):
            turn.emit("stopped", text="本轮已停止。已执行操作不会撤销；已进入网页的内容不能撤回。", pending=unsent)
    except Exception as exc:
        error = str(exc)
        key = profile.get("api_key")
        if key:
            error = error.replace(key, "[已隐藏]")
        if record is not None:
            pending = []
            with suppress(Exception):
                # Validation failures may leave a real interrupt pending and recoverable.
                with tasks.checkpoint() as saver:
                    restored, cfg = build_agent(model, mode, checkpointer=saver, root_dir=tasks.root)
                    cfg["configurable"]["thread_id"] = record["thread_id"]
                    pending = pending_interrupts(restored.get_state(cfg))
            with suppress(Exception):
                tasks.update(mode, sid, record["run_id"], "waiting" if pending else "failed",
                             pending=[normalize_interrupt(item, index) for index, item in enumerate(pending)], error=error)
            if not pending:
                with suppress(Exception):
                    saved = store.load_messages(sid)
                    store.replace_messages(sid, [*saved, *_new_supplements(saved, trace, turn.control), AIMessage(content=f"本轮任务未完成：{error}")],
                                           expected_count=record["expected_count"])
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
            elif isinstance(decision, dict) and decision.get("kind") == "stop":
                turn.explicit_stop = True
                turn.emit("stop_requested", text="已请求停止；正在关闭模型请求或等待已开始的工具安全退出。")
                await asyncio.to_thread(turn.control.stop)
            elif isinstance(decision, dict) and decision.get("kind") == "steer":
                identifier, text = decision.get("id"), decision.get("text")
                try:
                    import re
                    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) or not isinstance(text, str) or not text.strip() or len(text) > 48000 or "\0" in text:
                        raise ValueError("补充指令格式无效")
                    new = turn.control.enqueue(identifier, text.strip())
                    if new and getattr(turn, "archive_supplement", None):
                        with suppress(Exception): await asyncio.to_thread(turn.archive_supplement, identifier, text.strip())
                    turn.emit("steer_accepted", id=identifier, text=text.strip(), message="已接收，等待下个安全模型步骤；旧的未执行工具请求将取消。")
                except ValueError as exc:
                    turn.emit("steer_error", id=identifier, text=str(exc))
    turn.closed.set()
    await asyncio.to_thread(turn.control.stop)


async def _stream_turn(socket, args):
    turn = LiveTurn()
    store, sid = args[:2]
    turn.emit("session", id=sid)
    def archive_supplement(identifier, text):
        from langchain_core.messages import HumanMessage
        store.archive_messages(sid, [HumanMessage(content=text, id="steer-"+identifier,
            additional_kwargs={"user_supplement":True, "supplement_id":identifier, "receipt":"accepted"})], kind="user_supplement")
    turn.archive_supplement = archive_supplement
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
        await asyncio.to_thread(turn.control.stop)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await worker


@web.middleware
async def _local_only(request, handler):
    if request.url.host not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden(text="请从本机打开工作台")
    if (request.path == "/api/run" or request.method == "POST") and request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden(text="请求来源无效")
    return await handler(request)


async def _set_headers(request, response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"


async def _advisor_autoclose(root):
    """包装空闲关闭循环：模块缺失或出错都不能影响工作台启动。"""
    try:
        from web_advisor_idle import autoclose_loop
    except ImportError:
        return
    with suppress(Exception):
        await autoclose_loop(root)


def create_app(root=ROOT, profiles=None, *, warmup=True):
    root = Path(root).resolve(strict=True)
    base_profiles = environment_model_profiles() if profiles is None else profiles
    settings = WorkbenchSettings(root)
    profiles = settings.profiles(base_profiles)
    state_dir = root / ".workbench"
    state_dir.mkdir(exist_ok=True)
    stores = {mode: ChatStore(root / "sessions.db" if mode == "chat" else state_dir / f"{mode}.db") for mode in MODES}
    knowledge = KnowledgeStore(root, stores)
    subagents = SubagentStore(root)
    tasks = TaskStore(root)
    skills = SkillStore(root)
    web_advisors = WebAdvisorStore(root)
    experiments = ExperimentStore(root)
    browser_controls = asyncio.Lock()
    running = asyncio.Lock()
    routes = web.RouteTableDef()

    def read_mode(request):
        mode = request.query.get("mode", "chat")
        if mode not in stores:
            raise web.HTTPBadRequest(text="未知模式")
        return mode

    def experiment_owner(request):
        mode = read_mode(request)
        sid = request.query.get("sid", "")
        if sid and not stores[mode].get_title(sid):
            raise web.HTTPNotFound(text="实验所属会话不存在")
        scope = request.query.get("scope", "session")
        if scope not in {"session","project"}: raise web.HTTPBadRequest(text="实验范围不正确")
        return mode, sid, scope == "project"

    def experiment_error(exc):
        if isinstance(exc, LookupError): return web.HTTPNotFound(text=str(exc))
        if isinstance(exc, ExperimentChanged): return web.HTTPConflict(text=str(exc))
        return web.HTTPBadRequest(text=str(exc))

    def experiment_response(request, record):
        if request.query.get("display") == "1":
            spec, outcome = record["spec"], record["outcome"]
            display = {field: experiment_json(spec[field]) for field in ("parameters", "units", "seed")}
            if outcome:
                display["metrics"] = experiment_json(outcome["metrics"])
            record = {**record, "display": display}
        return web.json_response(record)

    @routes.get("/api/experiments")
    async def experiment_list(request):
        mode, sid, all_project = experiment_owner(request)
        return web.json_response(await asyncio.to_thread(experiments.list, mode, sid, all_project=all_project))

    @routes.post("/api/experiments")
    async def experiment_create(request):
        mode, sid, _ = experiment_owner(request)
        if running.locked(): raise web.HTTPConflict(text="请先结束当前任务，再冻结实验基线")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) - {"spec","parent_id","request_id"}:
                raise ValueError("实验创建格式不正确")
            identifier = payload.get("request_id")
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 64 or not all(c.isalnum() or c in "_-" for c in identifier):
                raise ValueError("需要有效的实验创建标识")
            parent = payload.get("parent_id")
            if parent is not None and not isinstance(parent, str): raise ValueError("父实验标识不正确")
            async with running:
                result = await asyncio.to_thread(experiments.create, payload.get("spec"), mode, sid, parent_id=parent,
                                                operation_key=experiment_digest(["ui",mode,sid,identifier]))
            return experiment_response(request, result)
        except (ValueError, LookupError, OSError) as exc:
            raise experiment_error(exc) from exc

    @routes.get("/api/experiments/{eid}")
    async def experiment_detail(request):
        mode, sid, all_project = experiment_owner(request)
        try:
            result = await asyncio.to_thread(experiments.get, request.match_info["eid"], mode, sid, all_project=all_project)
            return experiment_response(request, result)
        except (ValueError, LookupError, OSError) as exc: raise experiment_error(exc) from exc

    @routes.get("/api/experiments/{eid}/check")
    async def experiment_check(request):
        mode, sid, all_project = experiment_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(experiments.check, request.match_info["eid"], mode, sid, all_project=all_project))
        except (ValueError, LookupError, OSError) as exc: raise experiment_error(exc) from exc

    @routes.get("/api/experiments/{eid}/export")
    async def experiment_export(request):
        mode, sid, all_project = experiment_owner(request)
        try:
            identifier = request.match_info["eid"]
            blob = await asyncio.to_thread(experiments.export, identifier, mode, sid, all_project=all_project)
            return web.Response(body=blob, content_type="application/zip", headers={"Content-Disposition":f'attachment; filename="experiment-{identifier}.zip"'})
        except (ValueError, LookupError, OSError) as exc: raise experiment_error(exc) from exc

    @routes.post("/api/experiments/{eid}/{action:start|finish}")
    async def experiment_update(request):
        mode, sid, _ = experiment_owner(request)
        if running.locked(): raise web.HTTPConflict(text="请先结束当前任务，再登记实验状态")
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) - {"revision","acknowledged","result"} or payload.get("acknowledged") is not True:
                raise ValueError("需要明确确认此操作仅登记状态，不执行或停止实验进程")
            async with running:
                args = (request.match_info["eid"],mode,sid,payload.get("revision"))
                result = await asyncio.to_thread(experiments.start, *args) if request.match_info["action"] == "start" else await asyncio.to_thread(experiments.finish, *args, payload.get("result"))
            return experiment_response(request, result)
        except (ValueError, LookupError, OSError) as exc: raise experiment_error(exc) from exc

    @routes.get("/")
    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    @routes.get("/api/health")
    async def health(request):
        return web.json_response({"service": "astro-agent-workbench", "protocol": 2,
                                  "workspace": str(root), "engine": RUNTIME.snapshot()})

    @routes.get("/api/settings")
    async def read_settings(request):
        result = await asyncio.to_thread(settings.public, base_profiles)
        result["current_port"] = request.url.port
        return web.json_response(result)

    @routes.get("/api/web-advisors/browser")
    async def web_browser_status(request):
        config = (await asyncio.to_thread(settings.load))["web_advisor"]
        return web.json_response(await connection_status(config))

    @routes.post("/api/web-advisors/login")
    async def web_browser_login(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="登录窗口操作需要有效 JSON 确认") from None
        if not isinstance(data, dict) or set(data) != {"confirmed"} or data["confirmed"] is not True:
            raise web.HTTPBadRequest(text="需要明确确认打开专用官网登录窗口")
        if browser_controls.locked():
            raise web.HTTPConflict(text="正在处理浏览器操作，请先检查现有窗口，不要重复启动")
        async with browser_controls:
            try:
                config = (await asyncio.to_thread(settings.load))["web_advisor"]
                return web.json_response(await open_login_browser(root, config))
            except Exception:
                raise web.HTTPServiceUnavailable(text="无法启动登录窗口；请检查项目的启动网页顾问.bat 和保存的设置") from None

    @routes.get("/api/skills")
    async def skill_catalog(request):
        return web.json_response(await asyncio.to_thread(skills.catalog))

    @routes.get("/api/skills/official")
    async def official_skills(request):
        try:
            return web.json_response(await asyncio.to_thread(official_catalog))
        except (OSError, ValueError) as exc:
            raise web.HTTPBadGateway(text=f"官方技能目录不可用，请稍后重试或上传 ZIP：{exc}") from exc

    @routes.get("/api/skills/{name}")
    async def skill_detail(request):
        try:
            return web.json_response(await asyncio.to_thread(skills.get, request.match_info["name"]))
        except (OSError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.post("/api/skills/{name}/enabled")
    async def enable_skill(request):
        try:
            payload = await request.json()
            result = await asyncio.to_thread(skills.set_enabled, request.match_info["name"], payload.get("enabled"),
                                            payload.get("checksum"), payload.get("revision"))
            return web.json_response(result)
        except SkillChanged as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except (OSError, ValueError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.post("/api/skills/import")
    async def import_skill(request):
        import zipfile
        try:
            if request.content_type == "application/json":
                payload = await request.json()
                if not isinstance(payload, dict) or payload.get("source") != "anthropics":
                    raise ValueError("仅支持固定 Anthropic 仓库来源或本地 ZIP")
                result = await asyncio.to_thread(import_official, skills, payload.get("name"))
            else:
                parts, total = [], 0
                async for part in request.content.iter_chunked(64 * 1024):
                    total += len(part)
                    if total > MAX_ARCHIVE:
                        raise ValueError("技能 ZIP 超过 32 MiB")
                    parts.append(part)
                result = await asyncio.to_thread(skills.import_archive, b"".join(parts), request.query.get("name") or None)
            return web.json_response(result, status=201)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.post("/api/settings")
    async def save_settings(request):
        try:
            payload = await request.json()
            if running.locked():
                raise SettingsChanged("工作台正在处理任务，请等待本轮结束后再保存设置。")
            async with running:
                data = await asyncio.to_thread(settings.prepare, payload, base_profiles)
                updated = settings.profiles(base_profiles, data)
                for mode in MODES:
                    for sid in await asyncio.to_thread(tasks.active, mode):
                        record = await asyncio.to_thread(tasks.get, mode, sid)
                        name = record["profile"]
                        identity = model_identity(updated.get(name, {}))
                        if identity != model_identity(record["model"]):
                            raise SettingsChanged("待处理任务仍在使用该模型，请先完成或放弃本轮，再更改模型 ID、URL、接口类型或删除模型。")
                await asyncio.to_thread(settings.save, data, payload["revision"])
                profiles.clear()
                profiles.update(updated)
            result = settings.public(base_profiles, data)
            result["current_port"] = request.url.port
            result["restart_required"] = data["port"] != request.url.port
            return web.json_response(result)
        except SettingsChanged as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except OSError as exc:
            raise web.HTTPInternalServerError(text="无法保存本地设置，请检查工作区的写入权限。") from exc

    @routes.get("/api/state")
    async def state(request):
        mode = read_mode(request)
        sessions = await asyncio.to_thread(stores[mode].list_sessions)
        pending = await asyncio.to_thread(tasks.active, mode)
        for item in sessions:
            item["pending"] = item["id"] in pending
        return web.json_response({
            "profiles": [{"id": name, "model": profile["model"], **({"label": profile["label"]} if profile.get("label") else {})}
                         for name, profile in profiles.items()],
            "sessions": sessions,
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

    @routes.get("/api/sessions/{sid}/pending")
    async def pending(request):
        mode, sid = read_mode(request), request.match_info["sid"]
        if not stores[mode].get_title(sid):
            raise web.HTTPNotFound(text="会话不存在")
        record = await asyncio.to_thread(tasks.get, mode, sid)
        if record and record["status"] in {"waiting", "running"}:
            return web.json_response({"run_id": record["run_id"], "profile": record["profile"],
                                      "status": record["status"], "requests": record["pending"]})
        return web.json_response(None)

    def memory_owner(request):
        mode, sid = read_mode(request), request.match_info["sid"]
        store = stores[mode]
        if not store.get_title(sid):
            raise web.HTTPNotFound(text="会话不存在")
        return mode, sid, store

    def memory_page(request):
        try:
            offset = int(request.query.get("offset", "0"))
            limit = int(request.query.get("limit", "50"))
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError()
            return {"offset": offset, "limit": limit}
        except ValueError as exc:
            raise web.HTTPBadRequest(text="无效的原文分页参数") from exc

    @routes.get("/api/sessions/{sid}/memory")
    async def memory_state(request):
        _, sid, store = memory_owner(request)
        return web.json_response(await asyncio.to_thread(store.memory_state, sid))

    @routes.get("/api/sessions/{sid}/memory/search")
    async def memory_search(request):
        _, sid, store = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(MemorySearch(store).search, sid, request.query.get("q", ""),
                                                            historical=request.query.get("history") == "1"))
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/memory/source")
    async def memory_source(request):
        _, sid, store = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(resolve_source, store, sid, request.query.get("ref", "")))
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    def ensure_memory_idle(mode, sid):
        record = tasks.get(mode, sid)
        if record and record["status"] in {"running", "waiting"}:
            raise KnowledgeChanged("请先完成或放弃待处理任务，再更改科研知识或重建索引。")

    @routes.post("/api/sessions/{sid}/memory/reindex")
    async def memory_reindex(request):
        mode, sid, store = memory_owner(request)
        try:
            if running.locked():
                raise KnowledgeChanged("工作台正在处理任务，请稍后重建索引。")
            def rebuild():
                with tasks.execution_lock():
                    ensure_memory_idle(mode, sid)
                    return MemorySearch(store).rebuild(sid)
            async with running:
                return web.json_response(await asyncio.to_thread(rebuild))
        except KnowledgeChanged as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/knowledge")
    async def list_knowledge(request):
        mode, sid, _ = memory_owner(request)
        return web.json_response(await asyncio.to_thread(knowledge.list, mode, sid))

    @routes.get("/api/sessions/{sid}/subagents")
    async def list_subagents(request):
        mode, sid, _ = memory_owner(request)
        return web.json_response(await asyncio.to_thread(subagents.list, mode, sid))

    @routes.get("/api/sessions/{sid}/web-advisors")
    async def web_advisor_jobs(request):
        mode, sid, _ = memory_owner(request)
        return web.json_response(await asyncio.to_thread(web_advisors.list, mode, sid))

    @routes.get("/api/sessions/{sid}/web-advisors/{jid}")
    async def web_advisor_report(request):
        mode, sid, _ = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(web_advisors.get, mode, sid, request.match_info["jid"]))
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc

    @routes.post("/api/sessions/{sid}/web-advisors/{jid}/{action:preview|focus}")
    async def web_advisor_view(request):
        mode, sid, _ = memory_owner(request)
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="官网查看操作需要有效 JSON 确认") from None
        if not isinstance(data, dict) or set(data) != {"confirmed"} or data["confirmed"] is not True:
            raise web.HTTPBadRequest(text="需要明确确认查看此任务的官网画面或切换标签页")
        try:
            job = await asyncio.to_thread(web_advisors.get, mode, sid, request.match_info["jid"])
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        if browser_controls.locked():
            raise web.HTTPTooManyRequests(text="浏览器画面正在读取，请稍后再试")
        async with browser_controls:
            try:
                return web.json_response(await view_job(job, focus=request.match_info["action"] == "focus"))
            except Exception as exc:
                from web_browser import UserActionRequired
                if isinstance(exc, UserActionRequired):
                    raise web.HTTPConflict(text=str(exc)) from None
                raise web.HTTPServiceUnavailable(text="无法读取该任务的官网标签页；请检查浏览器、CDP 地址或标签页是否仍打开。没有重新发送任务。") from None

    @routes.post("/api/sessions/{sid}/web-advisors/{jid}/resolve")
    async def resolve_web_advisor(request):
        mode, sid, _ = memory_owner(request)
        try:
            data = await request.json()
            if (not isinstance(data, dict) or set(data) != {"verified"}
                    or type(data.get("verified")) is not bool or data["verified"] is not True):
                raise ValueError("请明确确认已经核实网页；解除队列不会重发")
            if running.locked():
                raise ValueError("请先结束或放弃当前工作台任务，再核实并解除网页队列")
            result = await asyncio.to_thread(web_advisors.resolve, mode, sid, request.match_info["jid"])
            return web.json_response(result)
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/subagents/{jid}")
    async def subagent_report(request):
        mode, sid, _ = memory_owner(request)
        try:
            result = await asyncio.to_thread(subagents.get, mode, sid, request.match_info["jid"])
            return web.json_response(result)
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc

    @routes.post("/api/sessions/{sid}/knowledge")
    async def save_knowledge(request):
        mode, sid, _ = memory_owner(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("知识条目请求无效")
            if running.locked():
                raise KnowledgeChanged("工作台正在处理任务，请稍后更改科研知识。")
            def save():
                with tasks.execution_lock():
                    # Project-shared evidence may affect other modes too; freeze it during any pending task.
                    if any(tasks.active(item) for item in MODES):
                        raise KnowledgeChanged("有待处理任务，请先完成或放弃任务，再更改科研知识。")
                    if data.get("action") == "toggle":
                        knowledge.toggle(data.get("entry_id"), data.get("revision"), data.get("enabled"))
                    elif data.get("action") == "save":
                        knowledge.save({**data, "source_mode": data.get("source_mode", mode), "source_session": data.get("source_session", sid)})
                    else:
                        raise ValueError("未知的知识操作")
            async with running:
                await asyncio.to_thread(save)
                return web.json_response(await asyncio.to_thread(knowledge.list, mode, sid))
        except KnowledgeChanged as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/memory/originals")
    async def memory_originals(request):
        _, sid, store = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(store.original_page, sid, **memory_page(request)))
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/memory/versions/{vid}")
    async def memory_version(request):
        _, sid, store = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(store.memory_version, sid, request.match_info["vid"], **memory_page(request)))
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

    @routes.get("/api/sessions/{sid}/memory/blocks/{bid}")
    async def memory_block(request):
        _, sid, store = memory_owner(request)
        try:
            return web.json_response(await asyncio.to_thread(store.memory_block, sid, request.match_info["bid"]))
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

    @routes.post("/api/sessions/{sid}/memory")
    async def change_memory(request):
        mode, sid, store = memory_owner(request)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("token"), dict):
                raise ValueError("缺少摘要版本校验信息")
            vid = data.get("version_id")
            if vid is not None and not isinstance(vid, str):
                raise ValueError("无效的摘要版本")
            if running.locked():
                raise ChatHistoryChanged("工作台正在处理任务，请等待本轮结束后再更改摘要。")
            def change():
                with tasks.execution_lock():
                    record = tasks.get(mode, sid)
                    if record and record["status"] in {"running", "waiting"}:
                        raise ChatHistoryChanged("请先完成或放弃待处理任务，再更改摘要。")
                    if data.get("action") == "edit" and vid:
                        store.revise_memory(sid, vid, data.get("summary"), data["token"])
                    elif data.get("action") == "restore":
                        store.restore_memory(sid, vid, data["token"])
                    else:
                        raise ValueError("未知的摘要操作")
            async with running:
                await asyncio.to_thread(change)
                return web.json_response(await asyncio.to_thread(store.memory_state, sid))
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ChatHistoryChanged as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    @routes.post("/api/sessions/{sid}/discard")
    async def discard(request):
        mode, sid = read_mode(request), request.match_info["sid"]
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="请求格式无效")
        # A closed WebSocket may need one queue-poll interval to release its worker.
        for _ in range(20):
            if not running.locked():
                break
            await asyncio.sleep(0.05)
        def cancel():
            with tasks.execution_lock():
                record = tasks.get(mode, sid)
                if not record or data.get("run_id") != record["run_id"] or record["status"] not in {"waiting", "running"}:
                    raise ValueError("任务已更新，请重新打开会话。")
                tasks.update(mode, sid, record["run_id"], "cancelled")
        try:
            await asyncio.to_thread(cancel)
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok": True})

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
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=2 * 1024 * 1024)
        await socket.prepare(request)
        try:
            data = await socket.receive_json(timeout=10)
            if not isinstance(data, dict):
                raise ValueError("请求格式无效")
            mode, name, sid = data.get("mode"), data.get("profile"), data.get("session")
            question = data.get("text")
            resuming = data.get("resume") is True
            if not isinstance(mode, str) or mode not in stores:
                raise ValueError("未知模式")
            if not isinstance(name, str) or name not in profiles:
                raise ValueError("请先在 .env 配置模型档案和 API_KEY")
            if not resuming and (not isinstance(question, str) or not question.strip() or len(question) > 48_000):
                raise ValueError("请输入 1 至 48000 字符的问题")
            store = stores[mode]
            if sid is not None and (not isinstance(sid, str) or not store.get_title(sid)):
                raise ValueError("会话不存在")
            active = tasks.get(mode, sid) if sid else None
            if resuming:
                if not active or active["status"] != "waiting" or mode == "chat":
                    raise ValueError("当前没有待恢复的任务。")
                name = active["profile"]
                if name not in profiles:
                    raise ValueError("任务原来的模型档案已不存在。")
                question = active["question"]
            elif active and active["status"] in {"waiting", "running"}:
                raise ValueError("请先继续审批或放弃本轮任务，再发送新消息。")
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
            if resuming:
                for _ in range(20):
                    if not running.locked():
                        break
                    await asyncio.sleep(0.05)
            if running.locked():
                raise ValueError("工作台正在处理另一轮请求")
            async with running:
                if name not in profiles:
                    raise ValueError("模型配置已更新，请重新选择模型。")
                profile = dict(profiles[name])
                chosen = data.get("skills", [])
                if chosen and resuming:
                    raise ValueError("待恢复任务沿用原技能快照，不能在恢复请求中替换")
                if not isinstance(chosen, list) or len(chosen) > 3:
                    raise ValueError("每轮最多选择 3 个技能")
                for entry in chosen:
                    if not isinstance(entry, dict) or set(entry) != {"name", "checksum"}:
                        raise ValueError("所选技能格式不正确")
                    snapshot = await asyncio.to_thread(skills.get, entry["name"], enabled=True)
                    if snapshot["checksum"] != entry["checksum"]:
                        raise SkillChanged("所选技能已变更，请刷新后重新选择")
                profile["_skills"] = active["model"].get("skills", []) if resuming else chosen
                effort = data.get("reasoning_effort")
                qwen = profile["model"].casefold() == "qwen3.8-27b-coder390"
                if effort is not None and (not qwen or effort not in ("none", "xhigh") or resuming):
                    raise ValueError("本轮推理强度仅支持 Qwen 的 none/xhigh；待恢复任务沿用原请求")
                if qwen:
                    profile["reasoning_effort"] = ((active["model"].get("reasoning_effort") or "none") if resuming else (effort or "none"))
                    if compact_focus(question) is not None:
                        profile["reasoning_effort"] = "none"
                if sid is None:
                    sid = await asyncio.to_thread(store.create_session, question)
                await _stream_turn(socket, (store, sid, mode, name, profile, question.strip(), attachments, tasks, resuming, knowledge))
        except (ValueError, TypeError, asyncio.TimeoutError, web.HTTPException) as exc:
            if not socket.closed:
                await socket.send_json({"kind": "error", "text": getattr(exc, "text", None) or str(exc) or "请求超时"})
        except ConnectionError:
            pass
        finally:
            await socket.close()
        return socket

    app = web.Application(middlewares=[_local_only], client_max_size=256 * 1024)
    app.add_routes(routes)
    app.router.add_static("/ui/", ASSETS, show_index=False)
    app.on_response_prepare.append(_set_headers)

    async def advisor_idle(_app):
        """网页顾问专用浏览器空闲自动关闭（设置项为 0 时不生效）。"""
        task = asyncio.create_task(_advisor_autoclose(root))
        _app["advisor_idle_task"] = task

        async def stop(_):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        _app.on_cleanup.append(stop)

    app.on_startup.append(advisor_idle)
    if warmup:
        async def load_engine(_app):
            RUNTIME.start()
        app.on_startup.append(load_engine)
    return app


def _open_window(url):
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    if os.name == "nt" and edge.is_file():
        try:
            from window_geometry import launch_bounds
            x, y, width, height = launch_bounds()
            subprocess.Popen([str(edge), f"--app={url}",
                              f"--window-size={width},{height}", f"--window-position={x},{y}"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def _existing_workbench(port, root=ROOT):
    import json
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.3) as response:
            payload = json.loads(response.read(4096))
        if not isinstance(payload, dict):
            return False
        return (payload.get("service") == "astro-agent-workbench" and payload.get("protocol") == 2
                and Path(payload["workspace"]).resolve() == Path(root).resolve())
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return False


def main():
    parser = argparse.ArgumentParser(description="天文 Agent 工作台")
    parser.add_argument("--port", default=None, type=int, help="临时覆盖设置中保存的端口")
    parser.add_argument("--open", action="store_true", help="打开工作台窗口")
    parser.add_argument("--no-warmup", action="store_true", help="首次发送时才加载任务引擎")
    args = parser.parse_args()
    if args.port is None:
        try:
            args.port = WorkbenchSettings(ROOT).port()
        except ValueError as exc:
            parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1 到 65535 之间")
    url = f"http://127.0.0.1:{args.port}/"
    if _existing_workbench(args.port):
        print(f"复用已启动的工作台：{url}")
        if args.open:
            _open_window(url)
        return
    app = create_app(warmup=False)

    def when_listening(message):
        print(message)
        if not args.no_warmup:
            RUNTIME.start()
        if args.open:
            _open_window(url)

    # aiohttp calls print only after TCPSite.start(): no fixed browser delay.
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None, print=when_listening)


if __name__ == "__main__":
    main()
