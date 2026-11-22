"""对话式主界面（LM Studio 风格，Textual TUI，流式输出）。

- 启动默认先打开文件浏览器（BrowserScreen），q/esc 返回对话
- 消息流：用户消息与助手回答按时间排列，整体滚动
- 流式输出：回答逐段渲染进 Live 区（纯文本增量，成本低），完成后替换为
  Markdown 正式渲染；chat 两阶段时草稿在展开的折叠块里流式生成，
  答完自动收起
- 思考过程收纳：推理模型 reasoning_content、chat 两阶段草稿、
  agent 工具调用轨迹 → Collapsible 折叠块
- 后台线程 worker；shell 审批在对话内完成（y/n/替代命令）
- 斜杠命令与 REPL 相同；/browse 全屏进出；ctrl+q 退出
"""

import contextlib
import io
import os
import queue

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Input, Label, ListItem, ListView, Markdown, Static

from main import (
    MODE_CONFIGS,
    _artifact_auto_fetch,
    _msg_reasoning,
    artifact_note,
    build_agent,
    chat_reply,
    fetch_artifacts,
    handle_command,
    session_command,
)
from viewer import make_tree, parse_range, view_renderable
from browser import BrowserScreen

FLUSH_CHARS = 24  # 流式增量缓冲阈值：攒够再跨线程刷新，避免逐 token 重绘


def _agent_turn_extract(messages):
    """从一轮 agent 结果提取 (最终回答, 思考过程 dict)。

    思考过程来源：本轮各 AIMessage 的 reasoning_content + 工具调用轨迹
    （tool_calls 与对应 ToolMessage 的配对摘要）。
    """
    turn = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            break
        turn.append(msg)
    turn.reverse()

    trace = []
    reasoning_parts = []
    for i, msg in enumerate(turn):
        r = _msg_reasoning(msg)
        if r:
            reasoning_parts.append(r)
        for tc in getattr(msg, "tool_calls", None) or []:
            result = ""
            for later in turn[i + 1:]:
                if isinstance(later, ToolMessage) and later.tool_call_id == tc["id"]:
                    result = str(later.content)[:150]
                    break
            trace.append({
                "tool": tc.get("name", "?"),
                "args": {k: str(v)[:80] for k, v in (tc.get("args") or {}).items()},
                "result": result,
            })
    answer = turn[-1].content if turn and isinstance(turn[-1], AIMessage) else ""
    return answer, {"draft": None, "trace": trace,
                    "reasoning": "\n\n".join(reasoning_parts)}


class ChatApp(App):
    CSS = """
    #main { height: 1fr; }
    #side { width: 28; border-right: heavy $primary; display: none; }
    #side.visible { display: block; }
    .side-title { padding: 0 1; color: $primary; text-style: bold; }
    #sessions { height: 1fr; }
    #chat { height: 1fr; padding: 0 1; }
    #bottom { dock: bottom; height: auto; }
    #status { height: 1; padding: 0 1; color: $warning; display: none; }
    #status.visible { display: block; }
    #prompt { height: 3; }
    .user { margin: 1 0 0 0; border-left: thick $accent; padding: 0 1; }
    .assistant { margin: 0 0 1 0; }
    .thinking { margin: 0; color: $text-muted; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("s", "toggle_side", "会话栏"),
    ]

    def __init__(self, mode: str, ctx: dict) -> None:
        super().__init__()
        self.mode = mode
        self.ctx = ctx  # profiles/current/model/agents/store/session_id/chat_history/…
        self._busy = False
        self.awaiting_approval: dict | None = None  # {"queue": Queue} 挂起审批
        self._current_question = ""                 # 当前轮提问（首问自动命名用）
        self._session_ids: list[str] = []           # 侧栏序号 -> session_id
        # 流式状态（每轮开始时重置）
        self._live: Static | None = None        # 回答 Live 区（纯文本增量）
        self._live_text = ""
        self._delta_buf = ""                    # worker 线程侧缓冲
        self._draft_coll: Collapsible | None = None
        self._draft_body: Static | None = None
        self._draft_text = ""
        self._draft_buf = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main"):
            with Vertical(id="side", classes="visible"):
                yield Static("会话（n 新建 / 点击切换）", classes="side-title")
                yield ListView(id="sessions")
            with VerticalScroll(id="chat"):
                yield Static("", id="welcome")
        with Vertical(id="bottom"):
            yield Static("", id="status")
            yield Input(placeholder="提问…（/help 查看命令；ctrl+q 退出）", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._update_title()
        self._refresh_sessions()
        self._reload_chat_area()
        self.query_one("#prompt", Input).focus()
        # 默认先打开文件浏览器；q/esc 返回对话。传入命令回调：
        # 浏览器终端输入条里 / 开头的命令路由到这里执行（浏览时也能 /mode 切模式）
        self.push_screen(BrowserScreen(command_handler=self._run_command_from_browser))

    # ---------- 会话侧栏与消息区 ----------

    def _refresh_sessions(self) -> None:
        """按 store 重建侧栏列表（最近更新在前，当前会话加 ▶）。"""
        store = self.ctx["store"]
        sessions = store.list_sessions()
        self._session_ids = [s["id"] for s in sessions]
        lv = self.query_one("#sessions", ListView)
        lv.clear()
        for i, s in enumerate(sessions, 1):
            mark = "▶ " if s["id"] == self.ctx["session_id"] else ""
            lv.append(ListItem(Label(f"{mark}{i}. {s['title']}", markup=False)))

    def _reload_chat_area(self) -> None:
        """整体重绘消息区：欢迎行 + 当前会话的全部历史（多模态历史只显示文本）。"""
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        # 切换会话后流式组件全部作废
        self._live = None
        self._live_text = ""
        self._draft_coll = None
        self._draft_body = None
        prof = self.ctx["profiles"].get(self.ctx["current"], {})
        chat.mount(Static(Text(
            "天文 Agent 对话模式。四档模式 /model 切模型 /view 看文件，"
            f"/help 看全部；/session 管理会话。\n"
            f"当前: {self.mode} 模式 — {MODE_CONFIGS[self.mode]['desc']}\n"
            f"模型: {self.ctx['current']} ({prof.get('model', '?')})",
            style="dim",
        ), classes="thinking"))
        for m in self.ctx["chat_history"]:
            if isinstance(m, HumanMessage):
                chat.mount(Static(Text(str(m.content)), classes="user"))
            elif isinstance(m, AIMessage):
                chat.mount(Markdown(str(m.content) or "（空回复）", classes="assistant"))
        chat.scroll_end(animate=False)
        self.query_one("#prompt", Input).focus()

    @on(ListView.Selected, "#sessions")
    def on_session_clicked(self, event: ListView.Selected) -> None:
        idx = getattr(event, "index", None)
        if idx is None:
            idx = self.query_one("#sessions", ListView).index
        if idx is None:
            return
        out = session_command(f"/session {idx + 1}", self.ctx)
        self._append(Static(Text(out, style="dim"), classes="thinking"))
        self._refresh_sessions()
        self._reload_chat_area()

    def action_toggle_side(self) -> None:
        self.query_one("#side", Vertical).toggle_class("visible")

    def action_new_session(self) -> None:
        out = session_command("/session new", self.ctx)
        self._append(Static(Text(out, style="dim"), classes="thinking"))
        self._refresh_sessions()
        self._reload_chat_area()

    # ---------- UI 基础 ----------

    def _append(self, widget, before=None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(widget, before=before)
        chat.scroll_end(animate=False)

    def _status(self, text: str) -> None:
        st = self.query_one("#status", Static)
        st.update(Text(text, style="dim") if text else "")
        st.set_class(bool(text), "visible")

    def _update_title(self) -> None:
        prof = self.ctx["profiles"].get(self.ctx["current"], {})
        self.title = (f"天文 Agent · {self.mode} · "
                      f"{self.ctx['current']}({prof.get('model', '?')})")
        self.sub_title = "ctrl+q 退出"

    def _custom(self, mode: str) -> str:
        return self.ctx["custom_prompts"].get(mode, "")

    # ---------- 输入路由 ----------

    @on(Input.Submitted, "#prompt")
    def on_prompt(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        if self.awaiting_approval is not None:  # 审批优先于一切
            self._resolve_approval(text)
            return
        if text.startswith("/"):
            self._run_command(text)
            return
        if self._busy:
            self._append(Static(Text("（上一个问题还在处理中…）", style="dim"), classes="thinking"))
            return
        self._ask(text)

    def _ask(self, text: str) -> None:
        self._append(Static(Text(text), classes="user"))
        self._busy = True
        self._current_question = text
        self._live = None
        self._live_text = ""
        self._delta_buf = ""
        self._draft_coll = None
        self._draft_body = None
        self._draft_text = ""
        self._draft_buf = ""
        two_stage = os.environ.get("CHAT_TWO_STAGE", "1").lower() not in ("0", "false", "no", "off")
        self._status("内部草稿生成中…" if (two_stage and self.mode == "chat")
                     else "思考中…（ctrl+q 退出）")
        self._run_turn(text)

    # ---------- 后台执行（线程 worker）----------

    @work(thread=True)
    def _run_turn(self, question: str) -> None:
        try:
            if self.mode == "chat":
                reply, thinking = chat_reply(
                    self.ctx["model"], self.ctx["chat_history"], question,
                    custom_prompt=self._custom("chat"),
                    attachments=self.ctx.get("attachments") or [],
                    on_draft=self._on_draft_delta,
                    on_answer=self._on_answer_delta,
                    profile_name=self.ctx.get("current"),
                )
                self._flush_all()
                # 多会话持久化：全量重写当前会话消息 + 首问自动命名 + 刷新侧栏
                store = self.ctx["store"]
                store.replace_messages(self.ctx["session_id"], self.ctx["chat_history"])
                store.auto_title(self.ctx["session_id"], self._current_question)
                note = self._fetch_artifacts(reply)
                self.call_from_thread(self._show_reply, reply, thinking, note)
                self.call_from_thread(self._refresh_sessions)
                return
            mode = self.mode
            current_custom = self._custom(mode)
            agents = self.ctx["agents"]
            if (
                mode not in agents
                or agents[mode]["custom"] != current_custom
                or agents[mode]["model"] != self.ctx["current"]
            ):
                agent, config = build_agent(self.ctx["model"], mode, current_custom)
                agents[mode] = {"agent": agent, "config": config,
                                "custom": current_custom, "model": self.ctx["current"]}
            rec = agents[mode]
            inputs = {"messages": [{"role": "user", "content": question}]}
            while True:
                pending = None
                for kind, data in rec["agent"].stream(
                    inputs, config=rec["config"], stream_mode=["messages", "updates"],
                ):
                    if kind == "messages":
                        chunk, _meta = data
                        if isinstance(chunk, AIMessageChunk):
                            delta = chunk.content
                            if isinstance(delta, str) and delta:
                                self._on_answer_delta(delta)
                    elif kind == "updates" and "__interrupt__" in data:
                        pending = data["__interrupt__"]
                self._flush_answer()
                if not pending:
                    break
                req = pending[0].value
                q: "queue.Queue" = queue.Queue()
                self.call_from_thread(self._ask_approval, req, q)
                decision = q.get()  # worker 阻塞等待审批，界面照常可用
                inputs = Command(resume={"decisions": [decision]})
            state = rec["agent"].get_state(rec["config"])
            answer, thinking = _agent_turn_extract(state.values.get("messages", []))
            note = self._fetch_artifacts(answer)
            self.call_from_thread(self._show_reply, answer, thinking, note)
        except Exception as e:  # 任何错误都进对话流，不打断界面
            self._flush_answer()
            from main import enrich_error
            self.call_from_thread(
                self._show_error,
                enrich_error(e, self.ctx.get("attachments") or []),
            )

    # ---------- 流式增量（worker 线程缓冲 → UI 线程刷新）----------

    def _on_answer_delta(self, delta: str) -> None:
        self._delta_buf += delta
        if len(self._delta_buf) >= FLUSH_CHARS:
            self._flush_answer()

    def _flush_answer(self) -> None:
        if self._delta_buf:
            buf, self._delta_buf = self._delta_buf, ""
            self.call_from_thread(self._append_answer, buf)

    def _append_answer(self, delta: str) -> None:
        self._live_text += delta
        if self._live is None:
            self._live = Static(Text(self._live_text), classes="assistant", id="live")
            self._append(self._live)
            self._status("回答生成中…")
        else:
            self._live.update(Text(self._live_text))
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    def _on_draft_delta(self, delta: str) -> None:
        self._draft_buf += delta
        if len(self._draft_buf) >= FLUSH_CHARS:
            buf, self._draft_buf = self._draft_buf, ""
            self.call_from_thread(self._append_draft, buf)

    def _append_draft(self, delta: str) -> None:
        self._draft_text += delta
        if self._draft_coll is None:
            self._draft_body = Static(Text(self._draft_text), classes="thinking")
            self._draft_coll = Collapsible(
                self._draft_body, title="内部草稿（生成中…）", collapsed=False
            )
            self._append(self._draft_coll)
        else:
            self._draft_body.update(Text(self._draft_text))
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    def _flush_all(self) -> None:
        self._flush_answer()
        if self._draft_buf:
            buf, self._draft_buf = self._draft_buf, ""
            self.call_from_thread(self._append_draft, buf)

    def _fetch_artifacts(self, text: str) -> str:
        """在工作线程里抓取回答中的产物链接（失败不影响回答展示）。"""
        try:
            if not _artifact_auto_fetch(self.ctx["current"]):
                return ""
            return artifact_note(fetch_artifacts(text))
        except Exception as e:  # 抓取永远不该打断对话
            return f"[抓取产物出错] {type(e).__name__}: {e}"

    # ---------- 完成 / 出错 ----------

    def _show_reply(self, reply: str, thinking: dict, note: str = "") -> None:
        self._add_thinking(thinking, skip_draft=self._draft_coll is not None)
        self._finish_live(reply)
        if note:
            self._append(Static(Text(note, style="cyan"), classes="thinking"))
        self._busy = False
        self._status("")
        self.query_one("#prompt", Input).focus()

    def _show_error(self, message: str) -> None:
        self._append(Static(Text(f"[出错] {message}", style="bold red"), classes="user"))
        self._busy = False
        self._status("")

    def _finish_live(self, reply: str) -> None:
        """Live 文本区替换为 Markdown 正式渲染；草稿折叠块自动收起。"""
        if self._live is not None:
            self._live.remove()
            self._live = None
            self._live_text = ""
        if self._draft_coll is not None:
            try:
                self._draft_coll.title = "内部草稿"
                self._draft_coll.collapsed = True  # 思考过程自动收纳
            except Exception:
                pass
            self._draft_coll = None
        self._append(Markdown(reply or "（空回复）", classes="assistant"))

    # ---------- 思考过程收纳 ----------

    def _add_thinking(self, thinking: dict, skip_draft: bool = False) -> None:
        parts = []
        if thinking.get("reasoning"):
            parts.append(("思考过程（模型 reasoning）", thinking["reasoning"]))
        if thinking.get("draft") and not skip_draft:  # 流式时草稿已实时展示过
            parts.append(("内部草稿（chat 两阶段）", thinking["draft"]))
        trace = thinking.get("trace") or []
        if trace:
            lines = []
            for i, t in enumerate(trace, 1):
                args = ", ".join(f"{k}={v}" for k, v in t["args"].items()) or "-"
                lines.append(f"{i}. {t['tool']}({args})\n   → {t['result']}")
            parts.append((f"工具调用轨迹（{len(trace)} 次）", "\n".join(lines)))
        for title, body in parts:
            # 思考块排在回答前面（Live 区可能已在对话流里）
            self._append(Collapsible(
                Static(Text(body), classes="thinking"),
                title=title, collapsed=True,
            ), before=self._live)

    # ---------- shell 审批 ----------

    def _ask_approval(self, req, q: "queue.Queue") -> None:
        _reqs = (req or {}).get("action_requests") or [] if isinstance(req, dict) else []
        self._approval_tool = (_reqs[0].get("name") if _reqs else None) or "run_powershell"
        desc = req.get("description") if isinstance(req, dict) else str(req)
        self._append(Static(Text(
            f"[请求执行命令，请审批]\n{desc or req}\n"
            "y=批准 / n=拒绝 / 直接输入替代命令",
            style="yellow",
        ), classes="user"))
        self.awaiting_approval = {"queue": q}
        self._status("等待审批…")
        self.query_one("#prompt", Input).focus()

    def _resolve_approval(self, choice: str) -> None:
        q = self.awaiting_approval["queue"]
        self.awaiting_approval = None
        if choice.lower() == "y":
            decision = {"type": "approve"}
        elif choice.lower() == "n":
            decision = {"type": "reject",
                        "message": "用户拒绝了这条命令，请改用其他方式完成，或询问用户怎么办。"}
        else:
            decision = {"type": "edit",
                        "edited_action": {"name": getattr(self, "_approval_tool", "run_powershell"),
                                          "args": {"command": choice}}}
        self._append(Static(Text(f"[审批] {choice}", style="yellow"), classes="thinking"))
        self._status("继续执行…")
        q.put(decision)

    # ---------- 斜杠命令 ----------

    def _run_command(self, text: str) -> None:
        cmd = text.split(None, 1)[0].lower()
        if cmd == "/view":
            args = text.split()
            if len(args) < 2:
                self._append(Static(Text("用法: /view <文件> [起始行|起始-结束]", style="dim"),
                                    classes="thinking"))
            else:
                lines = None
                if len(args) > 2:
                    lines = parse_range(args[2])
                    if lines is None:
                        self._append(Static(Text(
                            f"行号参数无效: {args[2]!r}  例: /view tools.py 100-150", style="red"),
                            classes="thinking"))
                        return
                self._append(Static(
                    view_renderable(args[1], lines),
                    classes="assistant",
                ))
            return
        if cmd == "/tree":
            args = text.split()
            depth = int(args[1]) if len(args) > 1 and args[1].isdigit() else 2
            self._append(Static(make_tree(".", depth), classes="assistant"))
            return
        if cmd == "/browse":
            self.push_screen(BrowserScreen(command_handler=self._run_command_from_browser))
            return
        if cmd == "/session":
            out = session_command(text, self.ctx)
            if out:
                self._append(Static(Text(out, style="dim"), classes="thinking"))
            self._refresh_sessions()
            self._reload_chat_area()
            return
        # 其余命令（/mode /model /prompt /help）走 handle_command，捕获其输出展示
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mode = handle_command(text, self.mode, self.ctx["custom_prompts"], self.ctx)
        out = buf.getvalue().rstrip()
        if out:
            self._append(Static(Text(out), classes="thinking"))
        self._update_title()

    def _run_command_from_browser(self, text: str):
        """浏览器终端输入条里的斜杠命令回调。

        返回 str 或 rich 渲染对象，由浏览器写进终端日志区：
        /view /tree 直接渲染成表格/树；/browse 提示已在浏览器；
        其余走 handle_command（/mode 切模式、/model 切模型、/file 管附件…），
        stdout 捕获为文本返回。命令对对话状态的实际效果（模式切换、附件管理）
        与在对话流里输入完全一致。
        """
        cmd = text.split(None, 1)[0].lower()
        if cmd == "/view":
            args = text.split()
            if len(args) < 2:
                return "用法: /view <文件> [起始行|起始-结束]"
            lines = parse_range(args[2]) if len(args) > 2 else None
            if len(args) > 2 and lines is None:
                return f"行号参数无效: {args[2]!r}  例: /view tools.py 100-150"
            return view_renderable(args[1], lines)
        if cmd == "/tree":
            args = text.split()
            depth = int(args[1]) if len(args) > 1 and args[1].isdigit() else 2
            return make_tree(".", depth)
        if cmd == "/browse":
            return "已经在浏览器里了；q/esc 退出浏览器回到对话"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mode = handle_command(text, self.mode, self.ctx["custom_prompts"], self.ctx)
        self._update_title()
        return buf.getvalue().rstrip() or "(无输出)"


def run_chat(mode: str, ctx: dict) -> None:
    """启动对话式界面（阻塞直至 ctrl+q 退出）。"""
    ChatApp(mode, ctx).run()
