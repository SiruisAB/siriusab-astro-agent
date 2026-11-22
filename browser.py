"""双栏文件浏览器：左侧目录树、右侧内容预览（/browse 进入，q / esc 退回对话）。

基于 Textual（rich 同门框架）；预览渲染复用 viewer.to_renderable，
与 /view 命令的渲染规则保持一致。浏览是纯查看行为，不影响 agent 状态与对话历史。
"""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DirectoryTree, Footer, Static

from viewer import EXCLUDE_DIRS, PROJECT_DIR, to_renderable

MAX_PREVIEW_BYTES = 1_000_000  # 超过 1MB 的文件不在双栏里整读，提示用 /view 切片


class ProjectTree(DirectoryTree):
    """项目目录树：跳过环境/缓存/版本库噪声目录。"""

    def filter_paths(self, paths):
        return [p for p in paths if p.name not in EXCLUDE_DIRS]


class Browser(App):
    CSS = """
    #body { height: 1fr; }
    ProjectTree { width: 1fr; border-right: heavy $primary; }
    #preview { width: 2fr; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "退出"),
        ("escape", "quit", "退出"),
        ("r", "reload", "刷新目录"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield ProjectTree(PROJECT_DIR, id="tree")
            with VerticalScroll(id="preview"):
                yield Static(
                    "[dim]← 方向键浏览目录，选中文件即预览；q 退回对话，r 刷新[/dim]",
                    id="content",
                )
        yield Footer()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.show_file(event.path)

    def action_reload(self) -> None:
        self.query_one("#tree", ProjectTree).reload()

    def show_file(self, path: Path) -> None:
        """右侧预览：渲染规则与 /view 一致（viewer.to_renderable）。"""
        content = self.query_one("#content", Static)
        if not path.is_file():
            content.update(f"[red]无法读取: {path}[/red]")
            return
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            name = path.name
            content.update(
                f"[yellow]{name} 超过 1MB，双栏不整读。[/yellow]\n"
                f"[dim]退回对话后用 /view {name} 1-200 分段查看[/dim]"
            )
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content.update(f"[red]读取失败: {e}[/red]")
            return
        content.update(to_renderable(path, text))
        self.query_one("#preview", VerticalScroll).scroll_home(animate=False)


def run_browser() -> None:
    """运行双栏浏览器（阻塞直至用户按 q 退出，随后回到对话主循环）。"""
    Browser().run()
