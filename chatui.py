"""对话式主界面（LM Studio 风格，Textual TUI）。

- 消息流：用户消息与助手 Markdown 回答按时间排列，整体滚动
- 思考过程收纳：推理模型的 reasoning_content、chat 两阶段草稿、
  agent 的工具调用轨迹 → Collapsible 折叠块（默认收起，点击/回车展开）
- 后台线程 worker 调 agent / chat_reply，界面不冻结；shell 审批在对话内完成
  （y=批准 / n=拒绝 / 输入替代命令）
- 斜杠命令与 REPL 相同：/mode /model /prompt /help 走 handle_command；
  /view /tree 直接渲染进对话流；/browse 以全屏 Screen 打开（escape 返回）
- ctrl+q 退出；main.py 以 --repl 启动旧式逐行交互
"""

import contextlib
import io
import queue

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Input, Markdown, Static

from main import (
    MODE_CONFIGS,
    _msg_reasoning,
    build_agent,
    chat_reply,
    handle_command,
)
from viewer import make_tree, parse_range, view_renderable
from browser import BrowserScreen


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
    #chat { height: 1fr; padding: 0 1; }
    #bottom { dock: bottom; height: auto; }
    #status { height: 1; padding: 0 1; color: $warning; display: none; }
    #status.visible { display: block; }
    #prompt { height: 3; }
    .user { margin: 1 0 0 0; border-left: thick $accent; padding: 0 1; }
    .assistant { margin: 0 0 1 0; }
    .thinking { margin: 0; color: $text-muted; }
    """

    BINDINGS = [("ctrl+q", "quit", "退出")]

    def __init__(self, mode: str, ctx: dict) -> None:
        super().__init__()
        self.mode = mode
        self.ctx = ctx  # profiles/current/model/agents/chat_history/custom_prompts
        self._busy = False
        self.awaiting_approval: dict | None = None  # {"queue": Queue} 挂起审批

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="chat"):
            yield Static("", id="welcome")
        with Vertical(id="bottom"):
            yield Static("", id="status")
            yield Input(placeholder="提问…（/help 查看命令；ctrl+q 退出）", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._update_title()
        prof = self.ctx["profiles"].get(self.ctx["current"], {})
        self._append(Static(Text(
            "天文 Agent 对话模式。四档模式 /model 切模型 /view 看文件，"
            f"/help 看全部。\n当前: {self.mode} 模式 — {MODE_CONFIGS[self.mode]['desc']}\n"
            f"模型: {self.ctx['current']} ({prof.get('model', '?')})",
            style="dim",
        ), classes="thinking"))
        self.query_one("#prompt", Input).focus()

    # ---------- UI 基础 ----------

    def _append(self, widget) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(widget)
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
        self._status("思考中…（ctrl+q 退出）")
        self._run_turn(text)

    # ---------- 后台执行 ----------

    @work(thread=True)
    def _run_turn(self, question: str) -> None:
        try:
            if self.mode == "chat":
                reply, thinking = chat_reply(
                    self.ctx["model"], self.ctx["chat_history"], question,
                    custom_prompt=self._custom("chat"),
                )
                self.call_from_thread(self._show_reply, reply, thinking)
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
            result = rec["agent"].invoke(
                {"messages": [{"role": "user", "content": question}]},
                config=rec["config"],
            )
            while "__interrupt__" in result:  # shell 审批：界面内完成
                req = result["__interrupt__"][0].value
                q: "queue.Queue" = queue.Queue()
                self.call_from_thread(self._ask_approval, req, q)
                decision = q.get()  # worker 阻塞等待，界面照常可用
                result = rec["agent"].invoke(Command(resume=[decision]), config=rec["config"])
            answer, thinking = _agent_turn_extract(result["messages"])
            self.call_from_thread(self._show_reply, answer, thinking)
        except Exception as e:  # 任何错误都进对话流，不打断界面
            self.call_from_thread(self._show_error, str(e))

    def _show_reply(self, reply: str, thinking: dict) -> None:
        self._add_thinking(thinking)
        self._append(Markdown(reply or "（空回复）", classes="assistant"))
        self._busy = False
        self._status("")
        self.query_one("#prompt", Input).focus()

    def _show_error(self, message: str) -> None:
        self._append(Static(Text(f"[出错] {message}", style="bold red"), classes="user"))
        self._busy = False
        self._status("")

    # ---------- 思考过程收纳 ----------

    def _add_thinking(self, thinking: dict) -> None:
        parts = []
        if thinking.get("reasoning"):
            parts.append(("思考过程（模型 reasoning）", thinking["reasoning"]))
        if thinking.get("draft"):
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
            ))

    # ---------- shell 审批 ----------

    def _ask_approval(self, req, q: "queue.Queue") -> None:
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
            decision = {"type": "accept"}
        elif choice.lower() == "n":
            decision = {"type": "response",
                        "message": "用户拒绝了这条命令，请改用其他方式完成，或询问用户怎么办。"}
        else:
            decision = {"type": "response", "message": f"请改用这条命令: {choice}"}
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
                self._append(Static(
                    view_renderable(args[1], parse_range(args[2]) if len(args) > 2 else None),
                    classes="assistant",
                ))
            return
        if cmd == "/tree":
            args = text.split()
            depth = int(args[1]) if len(args) > 1 and args[1].isdigit() else 2
            self._append(Static(make_tree(".", depth), classes="assistant"))
            return
        if cmd == "/browse":
            self.push_screen(BrowserScreen())
            return
        # 其余命令（/mode /model /prompt /help）走 handle_command，捕获其输出展示
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mode = handle_command(text, self.mode, self.ctx["custom_prompts"], self.ctx)
        out = buf.getvalue().rstrip()
        if out:
            self._append(Static(Text(out), classes="thinking"))
        self._update_title()


def run_chat(mode: str, ctx: dict) -> None:
    """启动对话式界面（阻塞直至 ctrl+q 退出）。"""
    ChatApp(mode, ctx).run()
