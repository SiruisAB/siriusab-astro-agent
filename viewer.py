"""终端文件查看：rich 渲染 md / 代码 / csv / json，树形列目录。

两层使用方：
- main.py 的 /view、/tree 命令（纯展示层，人看，不进模型上下文——
  agent 自己读文件走 FilesystemMiddleware 的文件工具）
- browser.py 的双栏预览（复用 to_renderable，保证两处渲染一致）
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent

# 目录树中不展示的噪声目录（环境/缓存/版本库内部）
EXCLUDE_DIRS = {".conda", ".venv", "__pycache__", ".git", "node_modules", ".idea", ".vscode"}

MAX_CSV_ROWS = 30  # csv 不带行号参数时默认显示的数据行数

# 纯文本类扩展名：直接输出，不做语法高亮
TEXT_SUFFIXES = {".txt", ".log", ".env", ".cfg", ".ini"}


def _resolve(path: str) -> Path:
    """相对路径一律相对项目根目录解析，/view tools.py 在任何模式下都能用。"""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (PROJECT_DIR / p)


def parse_range(arg: str) -> tuple[int, int]:
    """解析行号参数："100" → (100, 139)（该行起共 40 行，含两端），"100-150" → (100, 150)。"""
    if "-" in arg:
        a, b = arg.split("-", 1)
        return max(int(a), 1), int(b)
    n = int(arg)
    return max(n, 1), n + 39


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(text.splitlines()))


def _csv_table(rows: list[list[str]], name: str,
               row_range: tuple[int, int] | None = None,
               max_rows: int | None = MAX_CSV_ROWS) -> Table | None:
    """csv 行 → rich 表格。row_range 按原始行切片；否则默认前 max_rows 行数据。空文件返回 None。"""
    if not rows:
        return None
    ncols = len(rows[0])
    if row_range:
        shown = rows[row_range[0] - 1 : row_range[1]]
    elif max_rows is None:
        shown = rows[1:]
    else:
        shown = rows[1 : max_rows + 1]
    table = Table(show_header=True, header_style="bold",
                  title=f"{name}（共 {len(rows) - 1} 行数据）")
    for col in rows[0]:
        table.add_column(str(col) or "")
    for r in shown:
        table.add_row(*((list(r) + [""] * ncols)[:ncols]))  # 补齐/截断到表头列数
    return table


def to_renderable(path: Path, text: str, start_line: int = 1):
    """按扩展名把文件文本转成 rich 渲染对象——/view 与 /browse 双栏预览共用。

    start_line 仅对带行号的代码视图生效（切片后行号连续）。
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _csv_table(_csv_rows(text), path.name)
    if suffix == ".json":
        return JSON(text)
    if suffix == ".md":
        return Markdown(text)
    if suffix in TEXT_SUFFIXES or not suffix:
        return text
    try:  # 代码文件：语法高亮 + 行号
        return Syntax(text, suffix.lstrip(".") or "text", line_numbers=True,
                      start_line=start_line, theme="ansi_dark", word_wrap=True)
    except Exception:  # 未知扩展名 pygments 不认识时退回纯文本
        return Syntax(text, "text", line_numbers=True, start_line=start_line, word_wrap=True)


def view_file(path: str, lines: tuple[int, int] | None = None) -> None:
    """按扩展名选择渲染方式查看文件。lines 为 (起始行, 结束行)，可选。"""
    p = _resolve(path)
    if not p.is_file():
        console.print(f"[red]文件不存在: {p}[/red]")
        console.print("[dim]提示: /tree 查看目录，/view results/xxx.md 查看落盘结果[/dim]")
        return
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        console.print(f"[red]读取失败: {e}[/red]")
        return

    suffix = p.suffix.lower()
    if suffix == ".csv":
        rows = _csv_rows(text)
        table = _csv_table(rows, p.name, row_range=lines)
        if table is None:
            console.print("[dim]（空文件）[/dim]")
            return
        console.print(table)
        if not lines and len(rows) - 1 > MAX_CSV_ROWS:
            console.print(f"[dim]… 仅显示前 {MAX_CSV_ROWS} 行；全部查看: /view {p.name} 1-{len(rows)}[/dim]")
        return

    start = lines[0] if lines else 1
    if lines:
        text = "\n".join(text.splitlines()[start - 1 : lines[1]])

    renderable = to_renderable(p, text, start_line=start)
    if suffix in TEXT_SUFFIXES or not suffix:
        console.print(f"[dim]{p}[/dim]\n{renderable}")
    else:
        console.print(renderable)


def list_tree(root: str = ".", depth: int = 2) -> None:
    """树形展示目录（跳过环境/缓存目录，目录在前、按名排序）。"""
    base = _resolve(root)
    if not base.is_dir():
        console.print(f"[red]目录不存在: {base}[/red]")
        return
    tree = Tree(f"[bold blue]{base.name or base}[/bold blue]", guide_style="dim")
    _fill(tree, base, depth)
    console.print(tree)


def _fill(node, directory: Path, depth: int) -> None:
    if depth <= 0:
        return
    try:
        entries = sorted(directory.iterdir(),
                         key=lambda e: (e.is_file(), e.name.lower()))
    except OSError:
        return
    for entry in entries:
        if entry.name in EXCLUDE_DIRS:
            continue
        if entry.is_dir():
            _fill(node.add(f"[bold blue]{entry.name}/[/bold blue]"), entry, depth - 1)
        else:
            node.add(entry.name)
