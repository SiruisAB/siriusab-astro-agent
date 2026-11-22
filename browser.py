"""双栏文件浏览器 + 终端面板：左目录树、右内容预览、底部可呼出终端。

/browse 进入，q / esc 退回对话。终端面板按 t 呼出/收起：
- 输入条直接执行 PowerShell 命令（项目语义：PowerShell 每台 Windows 都有），
  输出显示在上方日志区，与目录树/预览同屏
- cd 在 Python 侧记录当前目录（每条命令独立进程，靠 cwd 参数保持位置）
- 执行在线程 worker 里跑，长命令不会冻结界面；60 秒超时兜底
预览渲染复用 viewer.to_renderable，与 /view 命令一致。浏览是纯查看行为，
不影响 agent 状态与对话历史。
"""

import subprocess
from pathlib import Path
from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Input, RichLog, Static

from viewer import EXCLUDE_DIRS, PROJECT_DIR, to_renderable

MAX_PREVIEW_BYTES = 1_000_000  # 超过 1MB 的文件不在双栏里整读，提示用 /view 切片
SHELL_TIMEOUT = 60  # 终端命令超时（秒）


class ProjectTree(DirectoryTree):
    """项目目录树：跳过环境/缓存/版本库噪声目录。"""

    def filter_paths(self, paths):
        return [p for p in paths if p.name not in EXCLUDE_DIRS]


class BrowserScreen(Screen):
    """双栏浏览器 + 终端面板。

    作为 Screen 设计：既能被 chatui push（escape/q 返回对话），
    也能由 _BrowserApp 独立运行（--repl 模式的 /browse）。
    """

    CSS = """
    #body { height: 1fr; }
    ProjectTree { width: 1fr; border-right: heavy $primary; }
    #preview { width: 2fr; padding: 0 1; }
    #term { height: 40%; border-top: heavy $primary; display: none; }
    #term.visible { display: block; }
    #termlog { height: 1fr; padding: 0 1; }
    #termin { height: 3; border-top: solid $primary-lighten-2; }
    """

    BINDINGS = [
        ("q", "quit", "退出"),
        ("escape", "quit", "退出"),
        ("r", "reload", "刷新目录"),
        ("t", "toggle_term", "终端"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cwd = str(PROJECT_DIR)  # 终端面板的当前目录（cd 持久化在这里）

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield ProjectTree(PROJECT_DIR, id="tree")
            with VerticalScroll(id="preview"):
                yield Static(
                    "[dim]← 方向键浏览目录，选中文件即预览；t 呼出终端；q 退回对话[/dim]",
                    id="content",
                )
        with Vertical(id="term"):
            yield RichLog(id="termlog", markup=False, wrap=True)
            yield Input(
                placeholder="终端命令（PowerShell；cd 支持相对/绝对路径）",
                id="termin",
            )
        yield Footer()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.show_file(event.path)

    def action_reload(self) -> None:
        self.query_one("#tree", ProjectTree).reload()

    def action_toggle_term(self) -> None:
        term = self.query_one("#term", Vertical)
        if term.has_class("visible"):
            term.remove_class("visible")
            self.query_one("#tree", ProjectTree).focus()
        else:
            term.add_class("visible")
            self.query_one("#termin", Input).focus()
            log = self.query_one("#termlog", RichLog)
            log.write(Text(f"浏览器终端就绪，当前目录: {self.cwd}", style="dim"))

    def show_file(self, path: Path) -> None:
        """右侧预览：渲染规则与 /view 一致（viewer.to_renderable）。"""
        content = self.query_one("#content", Static)
        if not path.is_file():
            content.update(f"[red]无法读取: {path}[/red]")
            return
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            content.update(
                f"[yellow]{path.name} 超过 1MB，双栏不整读。[/yellow]\n"
                f"[dim]退回对话后用 /view {path.name} 1-200 分段查看[/dim]"
            )
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content.update(f"[red]读取失败: {e}[/red]")
            return
        content.update(to_renderable(path, text))
        self.query_one("#preview", VerticalScroll).scroll_home(animate=False)

    @on(Input.Submitted, "#termin")
    def on_command(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.clear()
        if not cmd:
            return
        log = self.query_one("#termlog", RichLog)
        log.write(Text(f"PS> {cmd}", style="bold cyan"))
        if cmd == "cd" or cmd.startswith("cd ") or cmd.startswith("cd\t"):
            self._handle_cd(cmd[2:].strip(), log)
            return
        self._exec(cmd)  # 线程 worker，不冻结界面

    def _handle_cd(self, target: str, log: RichLog) -> None:
        """cd 持久化：只记录目标目录，真实执行交给后续命令的 cwd 参数。"""
        if not target:
            self.cwd = str(PROJECT_DIR)
        else:
            cand = Path(target.strip().strip('"').strip("'"))
            if not cand.is_absolute():
                cand = Path(self.cwd) / cand
            cand = cand.resolve()
            if not cand.is_dir():
                log.write(Text(f"[目录不存在] {cand}", style="red"))
                return
            self.cwd = str(cand)
        log.write(Text(f"[目录] {self.cwd}", style="dim"))

    @work(thread=True)
    def _exec(self, cmd: str) -> None:
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                cwd=self.cwd, capture_output=True, timeout=SHELL_TIMEOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            self.call_from_thread(self._log_result, cmd,
                                  proc.stdout or "", proc.stderr or "", proc.returncode)
        except subprocess.TimeoutExpired:
            self.call_from_thread(self._log_result, cmd, "",
                                  f"[超时] 超过 {SHELL_TIMEOUT} 秒已终止", -1)
        except FileNotFoundError:
            self.call_from_thread(self._log_result, cmd, "", "[找不到 powershell.exe]", -1)

    def _log_result(self, cmd: str, out: str, err: str, code: int) -> None:
        log = self.query_one("#termlog", RichLog)
        if out:
            log.write(out.rstrip())
        if err:
            log.write(Text(err.rstrip(), style="red"))
        if code:
            log.write(Text(f"(退出码 {code})", style="dim red"))

    def action_quit(self) -> None:
        """被聊天应用 push 时返回对话；独立运行时退出应用。"""
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()
        else:
            self.app.exit()


class _BrowserApp(App):
    """REPL 模式独立运行浏览器的壳。"""

    def on_mount(self) -> None:
        self.push_screen(BrowserScreen())


def run_browser() -> None:
    """独立运行双栏浏览器（--repl 模式的 /browse；阻塞直至用户按 q 退出）。"""
    _BrowserApp().run()
