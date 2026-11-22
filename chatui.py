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
from threading import Lock

from langchain_core.messages import AIMessage, HumanMessage
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Footer, Header, Input, Label, ListItem, ListView, Markdown, Static

from main import (
    MODE_CONFIGS,
    _artifact_auto_fetch,
    artifact_note,
    build_agent,
    fetch_artifacts,
    handle_command,
    load_chat_session,
    session_chat_reply,
    session_command,
)
from agent_turn import run_agent_turn
from browser import BrowserScreen
from chatstore import ChatHistoryChanged
from viewer import make_tree, parse_range, view_renderable

STREAM_REFRESH_INTERVAL = 0.05  # 按时间合并流式更新，短片段也能及时显示
_READ_ONLY_COMMANDS = {"/view", "/tree", "/help"}
_BUSY_MESSAGE = "（上一个问题还在处理中…）"


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
        # 流式状态（每轮开始时重置）
        self._live: Static | None = None        # 回答 Live 区（纯文本增量）
        self._live_text = ""
        self._draft_coll: Collapsible | None = None
        self._draft_body: Static | None = None
        self._draft_text = ""
        self._stream_lock = Lock()
        self._answer_parts: list[str] = []
        self._draft_parts: list[str] = []

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
        self._stream_timer = self.set_interval(STREAM_REFRESH_INTERVAL, self._flush_stream, pause=True)
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
        lv = self.query_one("#sessions", ListView)
        lv.clear()
        for i, s in enumerate(sessions, 1):
            mark = "▶ " if s["id"] == self.ctx["session_id"] else ""
            lv.append(ListItem(Label(f"{mark}{i}. {s['title']}", markup=False), name=s["id"]))

    def _reload_chat_area(self) -> None:
        """整体重绘消息区：欢迎行 + 当前会话的全部历史（多模态历史只显示文本）。"""
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        # 切换会话后流式组件全部作废
        self._live = None
        self._live_text = ""
        self._draft_coll = None
        self._draft_body = None
        self._draft_text = ""
        with self._stream_lock:
            self._answer_parts.clear()
            self._draft_parts.clear()
        prof = self.ctx["profiles"].get(self.ctx["current"], {})
        widgets = [Static(Text(
            "天文 Agent 对话模式。四档模式 /model 切模型 /view 看文件，"
            f"/help 看全部；/session 管理会话。\n"
            f"当前: {self.mode} 模式 — {MODE_CONFIGS[self.mode]['desc']}\n"
            f"模型: {self.ctx['current']} ({prof.get('model', '?')})",
            style="dim",
        ), classes="thinking")]
        for m in self.ctx["chat_history"]:
            if isinstance(m, HumanMessage):
                widgets.append(Static(Text(str(m.content)), classes="user"))
            elif isinstance(m, AIMessage):
                widgets.append(Markdown(str(m.content) or "（空回复）", classes="assistant"))
        mounted = chat.mount_all(widgets)

        async def scroll_when_ready() -> None:
            await mounted
            # Markdown 挂载后还要解析正文，等布局更新后再跳到最新消息。
            if widgets[0] in chat.children:
                chat.scroll_end(animate=False)

        chat.call_later(scroll_when_ready)
        self.query_one("#prompt", Input).focus()

    @on(ListView.Selected, "#sessions")
    def on_session_clicked(self, event: ListView.Selected) -> None:
        if self._busy:
            self._notice(_BUSY_MESSAGE)
            return
        # 其他窗口可能更新会话排序，按条目的会话 ID 切换。
        sid = event.item.name
        if not sid or sid == self.ctx["session_id"]:
            return
        title = self.ctx["store"].get_title(sid)
        if not title:
            self._refresh_sessions()
            self._notice("该会话已删除")
            return
        load_chat_session(self.ctx, sid)
        self._refresh_sessions()
        self._reload_chat_area()
        self._notice(f"已切换到会话「{title}」")

    def action_toggle_side(self) -> None:
        self.query_one("#side", Vertical).toggle_class("visible")

    def action_new_session(self) -> None:
        self._run_command("/session new")

    # ---------- UI 基础 ----------

    def _append(self, widget: Widget, before: Widget | None = None, *, scroll: bool = True) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(widget, before=before)
        if scroll:
            chat.scroll_end(animate=False)

    def _notice(self, text: str) -> None:
        self._append(Static(Text(text, style="dim"), classes="thinking"))

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
            self._notice(_BUSY_MESSAGE)
            return
        self._ask(text)

    def _ask(self, text: str) -> None:
        self._append(Static(Text(text), classes="user"))
        self._busy = True
        self._live = None
        self._live_text = ""
        self._draft_coll = None
        self._draft_body = None
        self._draft_text = ""
        self._stream_timer.resume()
        two_stage = os.environ.get("CHAT_TWO_STAGE", "1").lower() not in ("0", "false", "no", "off")
        self._status("内部草稿生成中…" if (two_stage and self.mode == "chat")
                     else "思考中…（ctrl+q 退出）")
        self._run_turn(text)

    # ---------- 后台执行（线程 worker）----------

    @work(thread=True)
    def _run_turn(self, question: str) -> None:
        try:
            if self.mode == "chat":
                reply, thinking = session_chat_reply(
                    self.ctx, question,
                    on_draft=self._on_draft_delta,
                    on_answer=self._on_answer_delta,
                )
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
            def approve(req):
                decisions: queue.Queue = queue.Queue()
                self.call_from_thread(self._ask_approval, req, decisions)
                return [decisions.get()]

            answer, thinking = run_agent_turn(
                rec["agent"], rec["config"], [{"role": "user", "content": question}],
                on_answer=self._on_answer_delta, on_approval=approve,
            )
            note = self._fetch_artifacts(answer)
            self.call_from_thread(self._show_reply, answer, thinking, note)
        except Exception as e:  # 任何错误都进对话流，不打断界面
            if isinstance(e, ChatHistoryChanged):
                self.call_from_thread(self._reload_chat_area)
                self.call_from_thread(self._refresh_sessions)
            from main import enrich_error
            self.call_from_thread(
                self._show_error,
                enrich_error(e, self.ctx.get("attachments") or []),
            )

    # ---------- 流式增量（worker 线程缓冲 → UI 线程刷新）----------

    def _on_answer_delta(self, delta: str) -> None:
        with self._stream_lock:
            self._answer_parts.append(delta)

    def _on_draft_delta(self, delta: str) -> None:
        with self._stream_lock:
            self._draft_parts.append(delta)

    def _flush_stream(self) -> None:
        """在 UI 线程合并增量，原先位于底部时才跟随新内容。"""
        with self._stream_lock:
            if not (self._draft_parts or self._answer_parts):
                return
            draft_parts, self._draft_parts = self._draft_parts, []
            answer_parts, self._answer_parts = self._answer_parts, []

        chat = self.query_one("#chat", VerticalScroll)
        follow = chat.is_vertical_scroll_end
        if draft_parts:
            self._draft_text += "".join(draft_parts)
            if self._draft_coll is None:
                self._draft_body = Static(self._draft_text, classes="thinking", markup=False)
                self._draft_coll = Collapsible(
                    self._draft_body, title="内部草稿（生成中…）", collapsed=False
                )
                chat.mount(self._draft_coll)
            else:
                self._draft_body.update(self._draft_text)
        if answer_parts:
            self._live_text += "".join(answer_parts)
            if self._live is None:
                self._live = Static(self._live_text, classes="assistant live", markup=False)
                chat.mount(self._live)
                self._status("回答生成中…")
            else:
                self._live.update(self._live_text)
        if follow:
            chat.scroll_end(animate=False)

    def _fetch_artifacts(self, text: str) -> str:
        """在工作线程里抓取回答中的产物链接（失败不影响回答展示）。"""
        try:
            if not _artifact_auto_fetch(self.ctx["current"]):
                return ""
            return artifact_note(fetch_artifacts(text))
        except Exception as e:  # 抓取永远不该打断对话
            return f"[抓取产物出错] {type(e).__name__}: {e}"

    # ---------- 完成 / 出错 ----------

    async def _show_reply(self, reply: str, thinking: dict, note: str = "") -> None:
        self._stream_timer.pause()
        self._flush_stream()
        chat = self.query_one("#chat", VerticalScroll)
        answer = Markdown(reply or "（空回复）", classes="assistant")
        # 保留预览的位置，等 Markdown 解析并挂载正文后再交换。
        answer.display = False
        await chat.mount(answer)
        follow = chat.is_vertical_scroll_end
        with self.batch_update():
            self._add_thinking(thinking, before=self._live if self._live is not None else answer)
            answer.display = True
            if self._live is not None:
                await self._live.remove()
                self._live = None
                self._live_text = ""
            if self._draft_coll is not None:
                self._draft_coll.title = "内部草稿"
                self._draft_coll.collapsed = True
                self._draft_coll = None
                self._draft_body = None
                self._draft_text = ""
            if note:
                self._append(Static(Text(note, style="cyan"), classes="thinking"), scroll=False)
            self._status("")
            self.query_one("#prompt", Input).focus()
            self._busy = False
        if follow:
            chat.scroll_end(animate=False)

    def _show_error(self, message: str) -> None:
        self._stream_timer.pause()
        self._flush_stream()
        chat = self.query_one("#chat", VerticalScroll)
        follow = chat.is_vertical_scroll_end
        if self._live is not None:
            self._live.remove_class("live")
        if self._draft_coll is not None:
            self._draft_coll.title = "内部草稿（已中断）"
        self._append(Static(Text(f"[出错] {message}", style="bold red"), classes="user"), scroll=follow)
        self._busy = False
        self._status("")

    # ---------- 思考过程收纳 ----------

    def _add_thinking(self, thinking: dict, *, before: Widget) -> None:
        parts = []
        if thinking.get("reasoning"):
            parts.append(("思考过程（模型 reasoning）", thinking["reasoning"]))
        if thinking.get("draft") and self._draft_coll is None:  # 流式时草稿已实时展示过
            parts.append(("内部草稿（chat 两阶段）", thinking["draft"]))
        trace = thinking.get("trace") or []
        if trace:
            lines = []
            for i, t in enumerate(trace, 1):
                args = ", ".join(f"{k}={v}" for k, v in t["args"].items()) or "-"
                lines.append(f"{i}. {t['tool']}({args})\n   → {t['result']}")
            parts.append((f"工具调用轨迹（{len(trace)} 次）", "\n".join(lines)))
        for title, body in parts:
            self._append(Collapsible(
                Static(Text(body), classes="thinking"),
                title=title, collapsed=True,
            ), before=before, scroll=False)

    # ---------- shell 审批 ----------

    def _ask_approval(self, req, q: "queue.Queue") -> None:
        self._flush_stream()
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

    def _session_command(self, text: str) -> str:
        previous = self.ctx["session_id"]
        out = session_command(text, self.ctx)
        self._refresh_sessions()
        if self.ctx["session_id"] != previous:
            self._reload_chat_area()
        return out

    def _run_command(self, text: str) -> None:
        cmd = text.split(None, 1)[0].lower()
        if self._busy and cmd not in _READ_ONLY_COMMANDS:
            self._notice(_BUSY_MESSAGE)
            return
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
            self._notice(self._session_command(text))
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
        if self._busy and cmd not in _READ_ONLY_COMMANDS:
            return _BUSY_MESSAGE
        if cmd == "/session":
            return self._session_command(text)
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
