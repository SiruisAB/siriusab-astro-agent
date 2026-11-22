"""天文 Agent 主程序。

默认启动对话式界面（chatui，LM Studio 风格）；--repl 切换为旧式逐行交互。

四种模式（运行中用 /mode 切换，或启动时 --mode <名称>，--chat 等价于 --mode chat）：

- agent    通用模式：查天体数据、算坐标、执行 shell（需审批），工具循环
- research 科研构思：提出有新意的科学 idea、设计验证路径（仅查询类工具）
- code     代码工程：读代码、改代码、运行验证（含 shell，需审批）
- chat     学术问答：两阶段深度思考后一次性输出，可查 arXiv/ADS/SIMBAD

每个模式是独立的 agent 实例：各自的提示词、工具子集、对话记忆互不共享。

模型支持多档案热切换：.env 中用 MODEL_NAME_<名字> 定义附加档案，运行中 /model <名字>
切换（或启动时 --model <名字>）。对话历史存在 sessions.db，与模型无关——新模型读取
完整历史即可无缝衔接此前进展。

运行前：复制 .env.example 为 .env 并填入你的 API key。
"""

import base64
import io
import mimetypes
import os
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from deepagents.backends import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelRetryMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
# 用 DeepAgents 的摘要中间件而不是 langchain 的：前者接收 backend，会把被驱逐的
# 历史原文完整 offload 到 conversation_history/<session>.md，并在摘要里写明文件路径，
# 与 tools.py 的手动 compress 归档一起构成「原文不丢」的闭环。
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from tools import get_tools
from viewer import list_tree, parse_range, view_file

# ========== 各模式的系统提示词 ==========

SYSTEM_PROMPT = (
    "你是一个严谨的天文助手。回答天体数据问题时，必须先调用工具查询，"
    "不要凭记忆编造坐标、星等、距离等数值。"
    "用户用中文提问时用中文回答；天体名称传给工具时用英文名或星表编号"
    "（如织女星用 Vega，仙女座星系用 M31）。"
    "查询结果要做简单解读，而不只是罗列数字。\n"
    "讨论文献时可调用 search_arxiv / search_ads 检索论文，"
    "引用时给出 arXiv ID 或 bibcode。\n"
    "你拥有文件工具（read_file/write_file/edit_file 等），可以读写项目目录内的文件；"
    "大的查询结果会自动存入 results/ 目录并以文件路径引用。\n"
    "当对话变得很长、早期内容不再需要逐字保留时，调用 compress 压缩旧历史"
    "（摘要必须保留数值结果、文件路径、关键决策），需要细节时 decompress 恢复。\n"
    "你还可以调用 run_powershell / run_bash 执行 shell 命令，用于文件操作、"
    "运行脚本、调用本地工具等。注意：每条 shell 命令执行前用户都会被要求确认，"
    "若用户拒绝，请改用其他方式或直接询问用户意图；不要重复提交同一条被拒绝的命令。"
)

RESEARCH_PROMPT = (
    "你是一位资深的天体物理科研合作者，与用户平等讨论科研问题。\n"
    "你的核心价值是提出真正有新意的科学想法，而不是复述教科书结论。"
    "你配有文献检索工具（search_arxiv 查 preprint、search_ads 查正式期刊论文）"
    "和天体数据查询工具（SIMBAD）：\n"
    "- 查新优先：提出或讨论任何新想法前，先用 search_arxiv / search_ads "
    "多角度检索相关工作（关键词、物理机制、天体类别分别检索），"
    "确认新意点确实未被做过；已有相近工作时明确指出与它们的差异，"
    "或诚实地建议调整方向\n"
    "- 想法必须给出五要素：科学问题、核心创新点（明确说清与已有工作的区别）、"
    "为什么现在可行（数据/方法/观测设施的窗口期）、可执行的验证路径、主要风险与替代方案\n"
    "- 鼓励大胆的跨领域组合和未被检验的假设，但用物理合理性把关：能量量级、时标、"
    "统计陷阱（选择效应、参数简并、后验偏差）；发现用户想法的漏洞要直说并给出修正\n"
    "- 涉及具体天体参数（坐标、距离、星等）时调用查询工具核实，不得编造数值\n"
    "- 引用文献时给出准确的 arXiv ID 或 bibcode 与发表年份；"
    "检索不到相关文献时明确说明'未检索到，此说法待核实'，绝不臆造文献\n"
    "用中文讨论，专业术语保留英文。"
)

CODE_PROMPT = (
    "你是一名严谨的软件工程师，直接动手完成任务而不是纸上谈兵。\n"
    "工作方式：\n"
    "- 优先使用文件工具（read_file/write_file/edit_file/glob）查看和修改代码，"
    "edit_file 做精确文本替换；shell 仅用于运行命令和文件工具做不到的操作\n"
    "- 修改前先读现有代码，遵循项目现有的风格与命名习惯\n"
    "- 做最小必要改动；改完主动运行命令验证（编译/测试/执行），"
    "用真实输出说明结果，失败就修到通过\n"
    "- 删除、覆盖、批量修改等危险操作会先经用户审批；被拒绝后不要重试同一条命令\n"
    "- 需求有歧义时先问清再动手；完成后简述改了什么、如何验证\n"
    "用中文交流，代码与注释遵循项目现有语言习惯。"
)

# chat 模式：两阶段生成（均可自主调用文献/数据检索工具）。
CHAT_DRAFT_PROMPT = (
    "你是一位天体物理学者，正在为一次正式回答撰写内部草稿。草稿不直接展示给用户，"
    "请自由、深入地推理，不要担心结构：\n"
    "- 列出问题涉及的所有物理机制、相关定律与典型量级\n"
    "- 做数量级估算（Fermi 估算）检验每一种说法是否站得住\n"
    "- 主动寻找反例、边界条件和大众常见误解\n"
    "- 需要文献支撑或最新进展时，调用 search_arxiv / search_ads 检索；"
    "涉及具体天体参数时调用 SIMBAD 查询工具核实\n"
    "- 明确标注哪些是已确立结论、哪些处于学术争议、哪些你自己不确定\n"
    "直接输出草稿内容，不要寒暄。"
)

CHAT_PROMPT = (
    "你是一位理论功底扎实的天体物理学者。你可以调用检索工具核实信息："
    "search_arxiv / search_ads 查文献（引用时给出 arXiv ID 或 bibcode），"
    "SIMBAD 查询工具核实天体参数。用户消息中可能附带一份内部思考草稿，"
    "它是原始分析材料：请核查并修正其中的错误，取其精华，输出更严谨的最终回答。\n"
    "回答结构：\n"
    "1. 结论先行：一两句话直接回答核心问题\n"
    "2. 理论依据：引用具体的物理定律、公式或量级估算支撑判断"
    "（如维恩位移定律、Stefan-Boltzmann 定律、维里定理、哈勃定律），"
    "给出推理链条，关键判断配 Fermi 估算；引用文献时给出来源标识\n"
    "3. 全面性：涵盖主要影响因素、边界条件与不同情形下的差异；"
    "存在学术争议时列出各方观点与证据强度\n"
    "4. 准确性：明确区分'已确立的结论'、'活跃争议'与'个人推断'；"
    "不确定处标注置信度（高/中/低）及原因，绝不编造精确数值；"
    "工具检索不到的信息如实说明，不臆造文献\n"
    "5. 无法用工具核实的信息，在末尾建议用户 /mode research 深入讨论\n"
    "用中文回答，专业术语保留英文。"
)

# ========== 附件（chat 模式上传文件给模型）==========
# chat 模式不走 agent graph，因此拿不到 FilesystemMiddleware 的文件工具。这里直接
# 在提问时把文件内容（或图片 data URL）拼进 HumanMessage，等价于"把文件递给模型"。
# 支持：文本类（源码/CSV/JSON/LaTeX…）、图片（多模态 image_url）、PDF（需 pypdf）。

_ATTACH_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".dat", ".xml", ".html", ".tex", ".bib",
    ".py", ".js", ".ts", ".c", ".h", ".cpp", ".f90", ".f", ".for", ".fits.txt",
}
_ATTACH_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_ATTACH_PDF_SUFFIXES = {".pdf"}

# Office OOXML（docx/xlsx/pptx）本质是 zip 容器，必须按文档提取而不是解包注入。
# 按内部结构判定真实类型（word/ xl/ ppt/ 目录 + [Content_Types].xml），
# 所以改名的 docx.txt 也能被正确识别。
_OOXML_SUFFIXES = {".docx", ".xlsx", ".pptx"}

_ATTACH_MAX_CHARS = 60_000       # 单个文本文件注入上限（超出截断并注明）
_ATTACH_TOTAL_CHARS = 120_000    # 一次提问所有文本附件总量上限
_ATTACH_MAX_BYTES = 12 * 1024 * 1024   # 图片/PDF 单文件字节上限
_ATTACH_MAX_IMAGES = 8

# 压缩包：本地解包后按类型分发（没有 API 能直接吃 zip）
_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".tbz2", ".txz"}
_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024          # 压缩包本体上限
_ARCHIVE_MAX_UNCOMPRESSED = 256 * 1024 * 1024  # 解压总量上限（防 zip bomb）
_ARCHIVE_MAX_ENTRIES = 2000                    # 条目数上限
_ARCHIVE_MAX_FILES = 60                        # 实际注入的文件数上限
_ARCHIVE_MAX_FILE_CHARS = 20_000               # 单个文件注入上限
# 压缩包里常见的噪声目录/文件，直接跳过
_ARCHIVE_SKIP_PARTS = {
    ".git", ".svn", "__pycache__", "node_modules", ".venv", ".conda",
    ".idea", ".vscode", ".DS_Store", ".ipynb_checkpoints",
}
_ARCHIVE_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# PDF 传输方式：
#   text   （默认）用 pypdf 提取文本再拼进消息——任何 OpenAI 兼容端点都能用，
#          数值/编号是精确字符串，token 最省。
#   native 直接把 PDF 作为 file 内容块发送（OpenAI Chat Completions 的
#          {"type":"file","file":{"file_data":"data:application/pdf;base64,..."}}）。
#          能保留版式、表格与扫描页（模型"看"页面），但只有实现了 file 块的端点
#          才接受；DeepSeek 之类的纯文本端点会直接 400。
# 开关：.env 里 PDF_NATIVE=1（全局）或 PDF_NATIVE_<档案名>=1（按模型档案）。
_PDF_NATIVE = False

# PDF 页面渲染（把 PDF 每页画成 PNG 走视觉通道）——中转站/纯转发端点唯一可行的
# "直接读 PDF" 方式：它不需要 input_file 或 file 内容块，只要端点支持视觉。
# polyglot 投递（sandbox 模式）：把任意文件包成 PNG+原字节，走图片通道送进
# 对方沙箱。实测该端点的图片附件会落到 /mnt/data/，模型自带 bash+python 可检查。
_SANDBOX_MAX_BYTES = 20 * 1024 * 1024   # 附加负载上限
_SANDBOX_CANVAS = (640, 200)

_PDF_IMAGE_MAX_PAGES = 10       # 一次最多渲染页数
_PDF_IMAGE_SCALE = 2.0          # 渲染倍率（2.0 ≈ 144dpi）
_PDF_IMAGE_MAX_EDGE = 1600      # 长边上限，超出等比缩小以控 token


def _sandbox_mode(profile_name: str | None = None) -> bool:
    """是否启用 polyglot 投递：任意文件 → PNG+原字节 → 图片通道。"""
    def truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    if profile_name:
        per = os.environ.get(f"SANDBOX_MODE_{profile_name.upper()}")
        if per is not None:
            return truthy(per)
    return truthy(os.environ.get("SANDBOX_MODE", "0"))


def _polyglot_png(payload: bytes, label: str) -> bytes:
    """构建 PNG+payload polyglot。

    PNG 解码器读到 IEND 就停止，尾部字节被忽略但仍留在文件里；
    ZIP 的中央目录在末尾，所以 zipfile 能从后往前找到 EOCD。
    这样同一份字节既是合法图片，又能被解包。
    """
    from PIL import Image, ImageDraw

    w, h = _SANDBOX_CANVAS
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, w - 8, h - 8], outline="black", width=2)
    d.text((24, 40), "POLYGLOT DELIVERY", fill="black")
    d.text((24, 70), label[:48], fill="black")
    d.text((24, 100), f"payload: {len(payload)} bytes", fill="black")
    d.text((24, 130), "trailing bytes after IEND carry the original file",
           fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue() + payload


def _pdf_mode(profile_name: str | None = None) -> str:
    """当前 PDF 处理方式：'text'（提文本）/ 'file'（原生块）/ 'image'（渲染成图）。

    优先级：PDF_MODE_<档案> > PDF_MODE > PDF_NATIVE（旧开关，等价于 'file'）。
    """
    def get(var):
        return os.environ.get(var)

    raw = None
    if profile_name:
        raw = get(f"PDF_MODE_{profile_name.upper()}")
    if raw is None:
        raw = get("PDF_MODE")
    if raw:
        v = raw.strip().lower()
        if v in ("image", "img", "page", "render"):
            return "image"
        if v in ("file", "native"):
            return "file"
        return "text"
    return "file" if _pdf_native_enabled(profile_name) else "text"


def _pdf_native_enabled(profile_name: str | None = None) -> bool:
    """是否对当前模型档案启用原生 PDF 块。"""
    def truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    if profile_name:
        per = os.environ.get(f"PDF_NATIVE_{profile_name.upper()}")
        if per is not None:
            return truthy(per)
    return truthy(os.environ.get("PDF_NATIVE", "0"))


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# 文件头魔法字节 → 真实类型。不信扩展名：把 zip 改名成 .txt 是很常见的操作
# （打包工具、邮件网关、上传白名单都会这么干），按名字判断只会读到乱码。
_MAGIC = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
)


def _sniff_kind(path: Path) -> str | None:
    """按文件头判断真实类型：'archive' / 'pdf' / 'image' / None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind in ("zip", "gzip", "bzip2", "xz"):
                return "archive"
            if kind == "pdf":
                return "pdf"
            return "image"
    # RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    return None


def _looks_binary(data: bytes) -> bool:
    """粗判二进制：前 8KB 出现 NUL，或解码失败率高。"""
    if b"\x00" in data[:8192]:
        return True
    try:
        data[:8192].decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # 少量坏字节可容错，大面积失败才当二进制
        return e.start > 512


def _is_archive_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(".zip") or low.endswith(".tar") or low.endswith(".tar.gz") \
        or low.endswith(".tgz") or low.endswith(".tar.bz2") or low.endswith(".tar.xz")


def _archive_verdict(name: str) -> str:
    """判断压缩包内某个条目要不要注入：'skip：<原因>' 或 'ok'。"""
    from pathlib import PurePosixPath
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts:
        return "skip:空路径"
    if name.startswith("/") or parts[0] == "/" or ".." in parts:
        return "skip:路径越界"        # zip slip：绝不接受跳出归档根的路径
    if any(p in _ARCHIVE_SKIP_PARTS for p in parts):
        return "skip:噪声目录"
    if PurePosixPath(name).name in _ARCHIVE_SKIP_NAMES:
        return "skip:系统文件"
    return "ok"


def _render_pdf_pages(path: Path) -> dict:
    """把 PDF 每页渲染成 PNG，返回 data URL 列表（供视觉模型直接读版式）。

    用途：端点不支持 file/input_file 内容块时的唯一可行路径。
    相比提取文本，能保留表格、公式、图注、扫描页。
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"ok": False, "path": str(path),
                "note": "PDF 转图片需要 pypdfium2："
                        ".conda\\Scripts\\python.exe -m pip install pypdfium2"}
    try:
        doc = pdfium.PdfDocument(str(path))
        total = len(doc)
        pages = min(total, _PDF_IMAGE_MAX_PAGES)
        images = []
        for i in range(pages):
            page = doc[i]
            pil = page.render(scale=_PDF_IMAGE_SCALE).to_pil()
            # 长边超限则等比缩小，避免单页 token 过高
            w, h = pil.size
            longest = max(w, h)
            if longest > _PDF_IMAGE_MAX_EDGE:
                ratio = _PDF_IMAGE_MAX_EDGE / longest
                pil = pil.resize((int(w * ratio), int(h * ratio)))
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            images.append({"name": f"{path.name} 第{i + 1}页",
                           "data_url": f"data:image/png;base64,{data}"})
        note = f"{path.name}（PDF {total} 页"
        note += f"，渲染 {pages} 页为图片）" if pages < total else "，全部渲染为图片）"
        return {"ok": True, "path": str(path), "kind": "pdfimages",
                "images": images, "note": note}
    except Exception as e:
        return {"ok": False, "path": str(path), "note": f"PDF 渲染失败: {e}"}


def _pdf_from_bytes(data: bytes, name: str) -> dict:
    """处理内存中的 PDF（压缩包内条目）。

    压缩包里嵌套 PDF 很常见，不能像二进制那样跳过。
    按 _pdf_mode() 决定提文本还是渲染页图；'file' 模式对包内条目无意义，退回文本。
    """
    mode = _pdf_mode()
    if mode == "image":
        try:
            import pypdfium2 as pdfium
        except ImportError:
            mode = "text"          # 没有渲染库就退回文本，不整条丢弃
        else:
            try:
                doc = pdfium.PdfDocument(io.BytesIO(data))
                total = len(doc)
                pages = min(total, _PDF_IMAGE_MAX_PAGES)
                imgs = []
                for i in range(pages):
                    pil = doc[i].render(scale=_PDF_IMAGE_SCALE).to_pil()
                    w, h = pil.size
                    longest = max(w, h)
                    if longest > _PDF_IMAGE_MAX_EDGE:
                        ratio = _PDF_IMAGE_MAX_EDGE / longest
                        pil = pil.resize((int(w * ratio), int(h * ratio)))
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG", optimize=True)
                    imgs.append({"name": f"{name} 第{i + 1}页",
                                 "data_url": "data:image/png;base64,"
                                             + base64.b64encode(buf.getvalue()).decode()})
                return {"ok": True, "kind": "images", "images": imgs,
                        "text": "", "note": f"{name}（PDF {total} 页，渲染 {pages} 页）"}
            except Exception as e:
                return {"ok": False, "note": f"{name} PDF 渲染失败: {e}"}

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        if not text.strip():
            return {"ok": False, "note": f"{name}（PDF {len(reader.pages)} 页无可提取文本，"
                                        f"可用 PDF_MODE=image 渲染）"}
        return {"ok": True, "kind": "text", "text": text, "note": ""}
    except Exception as e:
        return {"ok": False, "note": f"{name} PDF 解析失败: {e}"}


def _ooxml_kind(data: bytes) -> str | None:
    """按内部结构判定 OOXML 真实类型：docx / xlsx / pptx / None。

    PK\x03\x04 头同时属于普通 zip 和 OOXML，单看文件头无法区分；
    OOXML 的标志是根目录有 [Content_Types].xml 加对应前缀的部件目录。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except Exception:
        return None
    if "[Content_Types].xml" not in names:
        return None
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    return None


def _docx_text(data: bytes, name: str) -> dict:
    """提取 Word：正文段落 + 表格（行内单元格用制表符连接）。"""
    try:
        from docx import Document
    except ImportError:
        return {"ok": False, "note": f"{name} 需要 python-docx："
                                     ".conda\\Scripts\\python.exe -m pip install python-docx"}
    try:
        doc = Document(io.BytesIO(data))
        parts = [t.strip() for t in (p.text for p in doc.paragraphs) if t]
        for tb in doc.tables:
            for row in tb.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append("\t".join(cells))
        text = "\n".join(parts)
        return {"ok": True, "kind": "text", "text": text,
                "note": (f"Word，{len(doc.paragraphs)} 段 {len(doc.tables)} 表，"
                         f"提取 {len(text)} 字符")}
    except Exception as e:
        return {"ok": False, "note": f"{name} Word 解析失败: {e}"}


def _xlsx_text(data: bytes, name: str) -> dict:
    """提取 Excel：逐工作表逐行，空行跳过，单元格用制表符连接。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {"ok": False, "note": f"{name} 需要 openpyxl："
                                     ".conda\\Scripts\\python.exe -m pip install openpyxl"}
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"[工作表 {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if any(vals):
                    lines.append("\t".join(vals))
        text = "\n".join(lines)
        return {"ok": True, "kind": "text", "text": text,
                "note": f"Excel，{len(wb.worksheets)} 个工作表，提取 {len(text)} 字符"}
    except Exception as e:
        return {"ok": False, "note": f"{name} Excel 解析失败: {e}"}


def _pptx_text(data: bytes, name: str) -> dict:
    """提取 PPT：逐幻灯片收集文本框与表格。"""
    try:
        from pptx import Presentation
    except ImportError:
        return {"ok": False, "note": f"{name} 需要 python-pptx："
                                     ".conda\\Scripts\\python.exe -m pip install python-pptx"}
    try:
        prs = Presentation(io.BytesIO(data))
        lines = []
        for i, slide in enumerate(prs.slides, 1):
            lines.append(f"[幻灯片 {i}]")
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    t = shape.text_frame.text.strip()
                    if t:
                        lines.append(t)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            lines.append("\t".join(cells))
        text = "\n".join(lines)
        n = sum(1 for _ in prs.slides)
        return {"ok": True, "kind": "text", "text": text,
                "note": f"PPT，{n} 页幻灯片，提取 {len(text)} 字符"}
    except Exception as e:
        return {"ok": False, "note": f"{name} PPT 解析失败: {e}"}


def _ooxml_extract(data: bytes, name: str) -> dict:
    """OOXML 提取分发。note 不含文件名（调用方决定怎么拼）。"""
    kind = _ooxml_kind(data)
    if kind == "docx":
        return _docx_text(data, name)
    if kind == "xlsx":
        return _xlsx_text(data, name)
    if kind == "pptx":
        return _pptx_text(data, name)
    return {"ok": False, "note": f"{name} 不是可识别的 OOXML 文档"}


def _read_archive(path: Path) -> dict:
    """解包压缩包，把里面的文本/图片整理成可注入的结构（纯内存，不落盘）。"""
    import io
    import zipfile

    if path.stat().st_size > _ARCHIVE_MAX_BYTES:
        return {"ok": False, "path": str(path),
                "note": f"压缩包过大（{_fmt_bytes(path.stat().st_size)}）"}

    entries: list[tuple[str, bytes]] = []
    try:
        # 按内容而不是扩展名判断容器格式（改名的 zip/tar 也能正确处理）
        is_zip = zipfile.is_zipfile(str(path))
        if is_zip:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                declared = sum(i.file_size for i in infos)
                if declared > _ARCHIVE_MAX_UNCOMPRESSED:
                    return {"ok": False, "path": str(path),
                            "note": f"解压后达 {_fmt_bytes(declared)}，超过 "
                                    f"{_fmt_bytes(_ARCHIVE_MAX_UNCOMPRESSED)} 上限（疑似 zip bomb）"}
                if len(infos) > _ARCHIVE_MAX_ENTRIES:
                    return {"ok": False, "path": str(path),
                            "note": f"条目数 {len(infos)} 超过 {_ARCHIVE_MAX_ENTRIES} 上限"}
                for info in infos:
                    if info.is_dir():
                        continue
                    # 符号链接：external_attr 高 16 位的 S_IFLNK
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        continue
                    entries.append((info.filename, None, info.file_size, zf))
                # 逐个读（读完就释放，避免整包驻留内存）
                loaded = []
                for name, _, size, _zf in entries:
                    with zf.open(name) as fh:
                        loaded.append((name, fh.read(), size))
                entries = loaded
        else:
            import tarfile
            with tarfile.open(path) as tf:
                members = tf.getmembers()
                declared = sum(m.size for m in members if m.isfile())
                if declared > _ARCHIVE_MAX_UNCOMPRESSED:
                    return {"ok": False, "path": str(path),
                            "note": f"解压后达 {_fmt_bytes(declared)}，超过上限（疑似压缩炸弹）"}
                if len(members) > _ARCHIVE_MAX_ENTRIES:
                    return {"ok": False, "path": str(path),
                            "note": f"条目数 {len(members)} 超过 {_ARCHIVE_MAX_ENTRIES} 上限"}
                loaded = []
                for m in members:
                    if not m.isfile() or m.issym() or m.islnk():
                        continue
                    fh = tf.extractfile(m)
                    if fh is None:
                        continue
                    loaded.append((m.name, fh.read(), m.size))
                entries = loaded
    except Exception as e:
        return {"ok": False, "path": str(path), "note": f"解包失败: {e}"}

    texts, images, manifest_lines, skipped = [], [], [], 0
    for name, data, size in sorted(entries, key=lambda x: x[0]):
        verdict = _archive_verdict(name)
        if verdict != "ok":
            manifest_lines.append(f"  ✗ {name}（{verdict.split(':',1)[1]}）")
            skipped += 1
            continue

        base = name.rsplit("/", 1)[-1]
        suffix = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""

        if suffix in _ATTACH_IMAGE_SUFFIXES:
            if len(images) >= _ATTACH_MAX_IMAGES:
                manifest_lines.append(f"  ✗ {name}（图片数超上限）")
                skipped += 1
                continue
            if len(data) > _ATTACH_MAX_BYTES:
                manifest_lines.append(f"  ✗ {name}（图片过大）")
                skipped += 1
                continue
            mime = mimetypes.guess_type(base)[0] or "image/png"
            images.append({"name": name,
                           "data_url": f"data:{mime};base64,"
                                       f"{base64.b64encode(data).decode('ascii')}"})
            manifest_lines.append(f"  ✓ {name}（图片 {_fmt_bytes(size)}）")
        elif suffix in _ATTACH_PDF_SUFFIXES or data[:5] == b"%PDF-":
            got = _pdf_from_bytes(data, name)
            if not got["ok"]:
                manifest_lines.append(f"  ✗ {got['note']}")
                skipped += 1
            elif got["kind"] == "text":
                texts.append({"name": name, "text": got["text"]})
                manifest_lines.append(f"  ✓ {name}（PDF 文本 {len(got['text'])} 字符）")
            else:
                for img in got["images"]:
                    if len(images) < _ATTACH_MAX_IMAGES:
                        images.append({"name": img["name"], "data_url": img["data_url"]})
                manifest_lines.append(f"  ✓ {name}（PDF 渲染 {len(got['images'])} 页）")
        elif data[:4] == b"PK\x03\x04" and _ooxml_kind(data):
            got = _ooxml_extract(data, name)
            if got["ok"]:
                text = got["text"]
                if len(text) > _ARCHIVE_MAX_FILE_CHARS:
                    text = text[:_ARCHIVE_MAX_FILE_CHARS] + "\n...(文件过长已截断)"
                texts.append({"name": name, "text": text})
                manifest_lines.append(f"  ✓ {name}（{got['note']}）")
            else:
                manifest_lines.append(f"  ✗ {got['note']}")
                skipped += 1
        elif _is_archive_name(name):
            # 先判嵌套压缩包：它们本身是二进制，但原因要说清（不递归解包）
            manifest_lines.append(f"  ✗ {name}（嵌套压缩包，不递归）")
            skipped += 1
        elif _looks_binary(data):
            manifest_lines.append(f"  ✗ {name}（二进制，跳过）")
            skipped += 1
        else:
            if len(texts) >= _ARCHIVE_MAX_FILES:
                manifest_lines.append(f"  ✗ {name}（文件数超上限）")
                skipped += 1
                continue
            text = data.decode("utf-8", errors="replace")
            if len(text) > _ARCHIVE_MAX_FILE_CHARS:
                text = text[:_ARCHIVE_MAX_FILE_CHARS] + "\n...(文件过长已截断)"
            texts.append({"name": name, "text": text})
            manifest_lines.append(f"  ✓ {name}（文本 {len(text)} 字符）")

    total = len(entries)
    manifest = (
        f"===== 压缩包 {path.name}（共 {total} 个条目："
        f"注入 {len(texts)} 个文本 / {len(images)} 张图片，跳过 {skipped}）=====\n"
        + "\n".join(manifest_lines[:200])
        + ("\n  ...（清单过长已截断）" if len(manifest_lines) > 200 else "")
        + "\n===== 清单结束 ====="
    )
    return {"ok": True, "path": str(path), "kind": "archive",
            "manifest": manifest, "texts": texts, "images": images,
            "note": f"{path.name}（压缩包，{total} 个条目，注入文本 {len(texts)} / 图片 {len(images)}）"}


def _read_attachment(path_str: str) -> dict:
    """读一个附件。返回 dict：{ok, path, kind, text, data_url, note}。

    kind: "text" | "image" | "pdf"。失败时 ok=False，note 说明原因（不抛异常）。
    """
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    p = p.resolve()
    if not p.is_file():
        return {"ok": False, "path": str(p), "note": f"文件不存在: {p}"}
    size = p.stat().st_size
    suffix = p.suffix.lower()

    # sandbox 模式：把非图片文件包成 polyglot，走图片通道投递
    if _sandbox_mode() and _sniff_kind(p) != "image":
        if size > _SANDBOX_MAX_BYTES:
            return {"ok": False, "path": str(p),
                    "note": f"文件过大（{_fmt_bytes(size)} > {_fmt_bytes(_SANDBOX_MAX_BYTES)}），"
                            f"polyglot 投递已跳过"}
        try:
            png = _polyglot_png(p.read_bytes(), p.name)
        except ImportError:
            return {"ok": False, "path": str(p),
                    "note": "polyglot 需要 Pillow："
                            ".conda\\Scripts\\python.exe -m pip install pillow"}
        except Exception as e:
            return {"ok": False, "path": str(p), "note": f"polyglot 构建失败: {e}"}
        return {
            "ok": True, "path": str(p), "kind": "image",
            "data_url": "data:image/png;base64,"
                        + base64.b64encode(png).decode("ascii"),
            "note": (f"{p.name}（polyglot 投递：合法 PNG，IEND 之后附了原始 "
                     f"{size} 字节；可用 python 扫描 PK\\x03\\x04 提取）"),
        }

    # OOXML（docx/xlsx/pptx）本质是 zip，但必须按文档提取，优先于压缩包处理
    sniffed = _sniff_kind(p)
    if sniffed == "archive" or suffix in _OOXML_SUFFIXES:
        data = p.read_bytes()
        okind = _ooxml_kind(data)
        if okind:
            got = _ooxml_extract(data, p.name)
            if not got["ok"]:
                return {"ok": False, "path": str(p), "note": got["note"]}
            if suffix not in _OOXML_SUFFIXES:
                got["note"] += f"（按文件头识别：扩展名 {suffix or '无'} 与内容不符）"
            return {"ok": True, "path": str(p), "kind": "text",
                    "text": got["text"], "note": got["note"]}

    # 扩展名与真实内容不一致时以内容为准（例如 zip 被改名成 .txt）
    corrected = None
    if sniffed == "archive" and not _is_archive_name(p.name):
        corrected = "archive"
    elif sniffed == "pdf" and suffix not in _ATTACH_PDF_SUFFIXES:
        corrected = "pdf"
    elif sniffed == "image" and suffix not in _ATTACH_IMAGE_SUFFIXES:
        corrected = "image"

    if corrected == "archive":
        got = _read_archive(p)
        if got.get("ok"):
            got["note"] = f"{got['note']}（按文件头识别：扩展名 {suffix or '无'} 与内容不符）"
        return got
    if corrected == "pdf":
        suffix = ".pdf"          # 交给下面的 PDF 分支
    elif corrected == "image":
        suffix = ".png"          # 只是为了让后面按图片处理；MIME 由嗅探结果决定


    if _is_archive_name(p.name):
        return _read_archive(p)

    if suffix in _ATTACH_IMAGE_SUFFIXES:
        if size > _ATTACH_MAX_BYTES:
            return {"ok": False, "path": str(p),
                    "note": f"图片过大（{_fmt_bytes(size)} > {_fmt_bytes(_ATTACH_MAX_BYTES)}）"}
        mime = mimetypes.guess_type(p.name)[0] if not corrected else None
        if not mime or not mime.startswith("image/"):
            mime = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif"}.get(
                sniffed if sniffed != "image" else "png", "image/png")
            if corrected == "image":
                with open(p, "rb") as fh:
                    h = fh.read(16)
                if h.startswith(b"\x89PNG"):
                    mime = "image/png"
                elif h.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif h.startswith(b"GIF8"):
                    mime = "image/gif"
                elif h[:4] == b"RIFF":
                    mime = "image/webp"
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return {"ok": True, "path": str(p), "kind": "image",
                "data_url": f"data:{mime};base64,{data}",
                "note": f"{p.name}（图片，{_fmt_bytes(size)}）"}

    if suffix in _ATTACH_PDF_SUFFIXES:
        if _pdf_mode() == "image":
            return _render_pdf_pages(p)
        if _pdf_mode() == "file":
            # 原生 file 块：不提取文本，把 PDF 字节直接交给模型
            if size > _ATTACH_MAX_BYTES:
                return {"ok": False, "path": str(p),
                        "note": f"PDF 过大（{_fmt_bytes(size)} > {_fmt_bytes(_ATTACH_MAX_BYTES)}）"}
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            pages = "?"
            try:
                from pypdf import PdfReader
                pages = len(PdfReader(str(p)).pages)
            except Exception:
                pass
            return {"ok": True, "path": str(p), "kind": "file",
                    "block": {
                        "type": "file",
                        "source_type": "base64",
                        "mime_type": "application/pdf",
                        "data": data,
                        "filename": p.name,
                    },
                    "note": f"{p.name}（PDF 原生块，{pages} 页，{_fmt_bytes(size)}）"}
        try:
            from pypdf import PdfReader
        except ImportError:
            return {"ok": False, "path": str(p),
                    "note": "读取 PDF 需要 pypdf：.conda\\Scripts\\python.exe -m pip install pypdf"}
        try:
            reader = PdfReader(str(p))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            return {"ok": False, "path": str(p), "note": f"PDF 解析失败: {e}"}
        if not text.strip():
            return {"ok": False, "path": str(p),
                    "note": f"{p.name} 没有可提取文本（可能是扫描版，需先 OCR）"}
        return {"ok": True, "path": str(p), "kind": "text", "text": text,
                "note": f"{p.name}（PDF，{len(reader.pages)} 页，提取 {len(text)} 字符）"}

    # 其余按文本读
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "path": str(p), "note": f"读取失败: {e}"}
    return {"ok": True, "path": str(p), "kind": "text", "text": text,
            "note": f"{p.name}（文本，{len(text)} 字符）"}


def _compose_question(question: str, attachments: list[str], extra: str = ""):
    """把问题与附件组装成消息 content。

    无图片时返回 str；有图片时返回 OpenAI 多模态 content block 列表。
    """
    texts = [question] if question else []
    images: list[str] = []
    files: list[dict] = []
    notes: list[str] = []
    used = 0

    for path in attachments or []:
        got = _read_attachment(path)
        if not got["ok"]:
            notes.append(f"[附件未加载] {got['note']}")
            continue
        if got["kind"] == "image":
            if len(images) < _ATTACH_MAX_IMAGES:
                images.append(got["data_url"])
                notes.append(f"[已附加图片] {got['note']}")
            else:
                notes.append(f"[附件跳过] {got['note']} 超过 {_ATTACH_MAX_IMAGES} 张上限")
            continue
        if got["kind"] == "file":
            files.append(got["block"])
            notes.append(f"[已附加] {got['note']}")
            continue
        if got["kind"] == "pdfimages":
            for img in got["images"]:
                if len(images) < _ATTACH_MAX_IMAGES:
                    images.append(img["data_url"])
            notes.append(f"[已附加] {got['note']}")
            continue
        if got["kind"] == "archive":
            # 先给清单（模型据此知道包里有什么），再给各文件正文
            texts.append(got["manifest"])
            for item in got["texts"]:
                remain = min(_ATTACH_MAX_CHARS, _ATTACH_TOTAL_CHARS - used)
                if remain <= 0:
                    break
                body = item["text"][:remain]
                truncated = len(item["text"]) > remain
                used += len(body)
                texts.append(
                    f"===== {item['name']}"
                    f"{'（已截断）' if truncated else ''} =====\n{body}\n===== 文件结束 ====="
                )
            for img in got["images"]:
                if len(images) < _ATTACH_MAX_IMAGES:
                    images.append(img["data_url"])
            notes.append(f"[已附加] {got['note']}")
            continue

        body = got["text"]
        remain = min(_ATTACH_MAX_CHARS, _ATTACH_TOTAL_CHARS - used)
        if remain <= 0:
            notes.append(f"[附件跳过] {got['note']} 超出总量上限")
            continue
        truncated = len(body) > remain
        body = body[:remain]
        used += len(body)
        texts.append(
            f"===== 附件：{os.path.basename(got['path'])}"
            f"{'（已截断）' if truncated else ''} =====\n{body}\n===== 附件结束 ====="
        )
        notes.append(f"[已附加] {got['note']}" + ("，内容已截断" if truncated else ""))

    if extra:
        texts.append(extra)

    header = ("\n".join(notes) + "\n\n") if notes else ""
    body_text = header + "\n\n".join(texts)

    if not images and not files:
        return body_text
    blocks: list = [{"type": "text", "text": body_text}]
    blocks.extend(files)  # 原生 PDF 块放在文本之后
    for url in images:
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return blocks


def enrich_error(exc: Exception, attachments: list[str] | None = None) -> str:
    """把端点拒绝原生 PDF 块这类错误翻译成可操作的提示。

    不支持 file 块的端点会回 400 "Invalid value: 'file'. Supported values are:
    'text' and 'image_url'." —— 直接抛给用户很难懂，这里补上怎么改。
    """
    msg = str(exc)
    lowered = msg.lower()
    has_pdf = any(str(a).lower().endswith(".pdf") for a in (attachments or []))
    if has_pdf and ("'file'" in msg or '"file"' in msg or "file_data" in lowered
                    or ("invalid" in lowered and "content" in lowered)):
        return (
            f"{msg}\n\n[建议] 当前模型端点不接受原生 PDF 文件块。"
            "在 .env 里把 PDF_NATIVE 设为 0（或删掉该行），改用 pypdf 提取文本后再发送。"
        )
    return msg


# ========== 抓取模型产物（下载方向）==========
# 实测：模型在沙箱里生成的产物会以公开 URL 形式回传，形如
#   https://files.chat01.ai/python-generations/<uuid>/out.zip
# 该链接无需鉴权即可下载 —— 因此只抓已知产物主机，避免把正文里的普通链接当附件拉。

_ARTIFACT_HOSTS = ("files.chat01.ai",)
_ARTIFACT_URL_RE = re.compile(r"https?://[^\s\)\]\>\}\"'，。、]+")
_ARTIFACT_MAX_FILES = 5
_ARTIFACT_MAX_BYTES = 100 * 1024 * 1024
_ARTIFACT_TIMEOUT = 60
_ARTIFACT_DIR = Path(__file__).resolve().parent / "results" / "artifacts"


def _artifact_auto_fetch(profile_name: str | None = None) -> bool:
    """是否在回答后自动抓取产物链接。ARTIFACT_AUTO_FETCH=0 可关闭。"""
    def truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    if profile_name:
        per = os.environ.get(f"ARTIFACT_AUTO_FETCH_{profile_name.upper()}")
        if per is not None:
            return truthy(per)
    return truthy(os.environ.get("ARTIFACT_AUTO_FETCH", "1"))


def _artifact_urls(text: str) -> list[str]:
    """从回答里挑出产物下载链接（仅限已知主机，去重保序）。"""
    seen: list[str] = []
    for m in _ARTIFACT_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip('.,;:!?')
        host = url.split("://", 1)[-1].split("/", 1)[0].lower()
        if any(host == h or host.endswith("." + h) for h in _ARTIFACT_HOSTS):
            if url not in seen:
                seen.append(url)
    return seen


def _safe_artifact_name(url: str, fallback: str) -> str:
    """从 URL 取文件名并清洗，杜绝路径穿越与奇怪字符。"""
    from urllib.parse import unquote, urlsplit

    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or fallback
    name = os.path.basename(name.replace("\\", "/"))
    name = "".join(c for c in name if c.isalnum() or c in "._-")
    return (name or fallback)[:120]


def fetch_artifacts(text: str, dest_dir: Path | None = None,
                    max_files: int | None = None) -> list[dict]:
    """扫描回答中的产物链接并下载到本地。

    返回 [{"ok", "url", "name", "path", "size", "note"}]；失败项 ok=False 并带 note。
    任何异常都不抛出——抓取失败不应打断一次正常回答。
    """
    urls = _artifact_urls(text)
    if not urls:
        return []
    limit = max_files or _ARTIFACT_MAX_FILES
    urls = urls[:limit]

    out_dir = Path(dest_dir) if dest_dir else _ARTIFACT_DIR
    results: list[dict] = []
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [{"ok": False, "url": u, "name": "", "path": None, "size": 0,
                 "note": f"无法创建目录 {out_dir}: {e}"} for u in urls]

    import requests

    for i, url in enumerate(urls, 1):
        name = _safe_artifact_name(url, f"artifact_{i}")
        try:
            with requests.get(url, stream=True, timeout=_ARTIFACT_TIMEOUT,
                              allow_redirects=True) as r:
                if r.status_code != 200:
                    results.append({"ok": False, "url": url, "name": name,
                                    "path": None, "size": 0,
                                    "note": f"HTTP {r.status_code}"})
                    continue

                declared = int(r.headers.get("Content-Length") or 0)
                if declared > _ARTIFACT_MAX_BYTES:
                    results.append({"ok": False, "url": url, "name": name,
                                    "path": None, "size": declared,
                                    "note": f"超过 {_fmt_bytes(_ARTIFACT_MAX_BYTES)} 上限"})
                    continue

                target = out_dir / name
                written = 0
                with open(target, "wb") as fh:
                    for chunk in r.iter_content(65536):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > _ARTIFACT_MAX_BYTES:
                            fh.close()
                            target.unlink(missing_ok=True)
                            raise ValueError("下载中途超过大小上限")
                        fh.write(chunk)

            results.append({"ok": True, "url": url, "name": name,
                            "path": str(target), "size": written,
                            "note": _fmt_bytes(written)})
        except Exception as e:
            results.append({"ok": False, "url": url, "name": name,
                            "path": None, "size": 0,
                            "note": f"{type(e).__name__}: {e}"})
    return results


def artifact_note(results: list[dict]) -> str:
    """把抓取结果整理成一行提示（给终端/对话界面显示）。"""
    if not results:
        return ""
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    parts = []
    if ok:
        names = "、".join(f"{r['name']}({r['note']})" for r in ok)
        parts.append(f"[已下载 {len(ok)} 个产物] {names}")
    if bad:
        parts.append("[抓取失败] " + "；".join(f"{r['name'] or r['url']} → {r['note']}"
                                              for r in bad))
    return "\n".join(parts)


def _history_note(question: str, attachments: list[str]) -> str:
    """写进对话历史的轻量版：只留文件名，不重复存全文（避免历史无限膨胀）。"""
    if not attachments:
        return question
    names = ", ".join(os.path.basename(a) for a in attachments)
    return f"{question}\n\n[本轮附加了文件：{names}]"


# ========== 模式配置：提示词 + 工具子集 ==========

MODE_CONFIGS = {
    "agent": {
        "desc": "通用模式：查天体数据、算坐标、执行 shell（需审批）",
        "prompt": SYSTEM_PROMPT,
        "tools": "all",
    },
    "code": {
        "desc": "代码工程：读代码、改代码、运行验证（含 shell，需审批）",
        "prompt": CODE_PROMPT,
        "tools": "all",
    },
    "chat": {
        "desc": "学术问答（两阶段深度思考，可查 arXiv/ADS/SIMBAD，无 shell）",
        "prompt": CHAT_PROMPT,
        "tools": "query",
    },
    "research": {
        "desc": "科研构思：查新优先（提 idea 前先检索 arXiv/ADS 确认新颖性），五要素结构化",
        "prompt": RESEARCH_PROMPT,
        "tools": "research",
    },
}

HELP_TEXT = """
命令：
  /mode [agent|research|code|chat]  切换模式
  /mode                             查看所有模式与当前选择
  /model [档案名]                   切换底层模型（档案在 .env 用 MODEL_NAME_<名字> 配置）
  /model                            查看已配置的模型档案与当前模型
  /view <文件> [起始|起始-结束]     查看文件：md 渲染 / 代码高亮+行号 / csv 表格 / json
  /tree [深度]                      树形查看项目目录（默认 2 层）
  /browse                           双栏文件浏览器（左目录树 / 右预览，q 退回对话）
                                    浏览器内按 t 呼出终端面板，可直接执行 PowerShell 命令
  /prompt <内容>                    为当前模式设置附加提示词（可有可无，叠加在默认提示词上）
  /file <路径> [更多…]              附加文件给模型（chat 模式：文本/源码/CSV/JSON/PDF/图片）
  /file                             查看已附加文件
  /file clear                       清除全部附件（移除单个: /file -1）
  /prompt off                       清除当前模式的附加提示词
  /prompt                           查看当前模式的附加提示词
  /help                             显示本帮助
  q                                 退出
"""


def compose_system(default_prompt: str, custom_prompt: str) -> str:
    """组装系统提示词：默认提示词 + 可选的用户附加要求。"""
    if custom_prompt:
        return f"{default_prompt}\n\n[用户附加要求]\n{custom_prompt}"
    return default_prompt


def list_model_profiles() -> dict:
    """扫描 .env 中配置的全部模型档案，返回 档案名 -> {model, api_key, base_url}。

    约定：主档案无后缀（MODEL_NAME / API_KEY / BASE_URL），档案名固定 default；
    附加档案加任意后缀，如 MODEL_NAME_DEEPSEEK / API_KEY_DEEPSEEK / BASE_URL_DEEPSEEK，
    档案名即后缀的小写（deepseek）。API_KEY_<名字> 缺省时沿用主档案的 API_KEY。
    """
    load_dotenv()
    profiles: dict = {}
    for raw_key, value in os.environ.items():
        key = raw_key.upper()
        if not value:
            continue
        if key == "MODEL_NAME":
            profiles["default"] = {
                "model": value,
                "api_key": os.environ.get("API_KEY"),
                "base_url": os.environ.get("BASE_URL") or None,
            }
        elif key.startswith("MODEL_NAME_"):
            name = key[len("MODEL_NAME_"):].lower()
            profiles[name] = {
                "model": value,
                "api_key": os.environ.get(f"API_KEY_{name.upper()}") or os.environ.get("API_KEY"),
                "base_url": os.environ.get(f"BASE_URL_{name.upper()}") or None,
            }
    # 只填了 API_KEY 没填 MODEL_NAME 时，主档案用默认模型名兜底
    if "default" not in profiles and os.environ.get("API_KEY"):
        profiles["default"] = {
            "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
            "api_key": os.environ.get("API_KEY"),
            "base_url": os.environ.get("BASE_URL") or None,
        }
    return profiles


def build_model(profile: dict | None = None) -> ChatOpenAI:
    """构建模型实例（OpenAI 兼容协议）。profile 缺省时读 .env 主档案。"""
    load_dotenv()
    if profile is None:
        profile = {
            "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
            "api_key": os.environ.get("API_KEY"),
            "base_url": os.environ.get("BASE_URL") or None,
        }
    if not profile.get("api_key"):
        raise ValueError(
            f"模型档案 {profile.get('model', '?')} 缺少 API_KEY"
            "（检查 .env 中的 API_KEY 或 API_KEY_<档案名>）"
        )
    return ChatOpenAI(
        model=profile["model"],
        api_key=profile["api_key"],
        base_url=profile.get("base_url") or None,  # OpenAI 官方可不填
        temperature=0,  # 天文数据要准确，不发挥
    )


_CHECKPOINTER = None


def _get_checkpointer() -> SqliteSaver:
    """持久记忆：会话存 sessions.db，重启后可恢复（懒初始化单例）。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        conn = sqlite3.connect("sessions.db", check_same_thread=False)
        _CHECKPOINTER = SqliteSaver(conn)
    return _CHECKPOINTER


def build_agent(model: ChatOpenAI, mode: str, custom_prompt: str = ""):
    """构建指定模式的 agent 实例，返回 (agent, config)。各模式记忆独立。

    middleware 组合（全部现成组件）：
    - HumanInTheLoopMiddleware   shell 命令人工审批
    - ModelRetryMiddleware       模型限流/超时自动重试
    - ToolRetryMiddleware        工具失败重试
    - TodoListMiddleware         任务清单复述（对抗长上下文目标漂移）
    - FilesystemMiddleware       结构化文件工具（read/write/edit/glob）+
                                 超 20K token 的工具结果自动落盘驱逐
    - SummarizationMiddleware    历史超限时结构化摘要兜底
    """
    cfg = MODE_CONFIGS[mode]
    project_dir = os.path.dirname(os.path.abspath(__file__))
    _fs_backend = FilesystemBackend(root_dir=project_dir, virtual_mode=True)
    agent = create_agent(
        model=model,
        tools=get_tools(cfg["tools"]) if cfg["tools"] else [],
        system_prompt=compose_system(cfg["prompt"], custom_prompt),
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={"run_powershell": True, "run_bash": True}
            ),
            ModelRetryMiddleware(max_retries=3),
            ToolRetryMiddleware(max_retries=2),
            TodoListMiddleware(),
            FilesystemMiddleware(
                backend=_fs_backend,
                tools=["ls", "read_file", "write_file", "edit_file", "glob", "grep"],
            ),
            # 显式给 trigger：新版默认 trigger=None 会让摘要永不触发。
            # backend 让被驱逐的历史原文落盘到 conversation_history/，可 read_file 取回。
            SummarizationMiddleware(
                model=model,
                backend=_fs_backend,
                trigger=("fraction", 0.8),
                keep=("messages", 20),
            ),
        ],
        checkpointer=_get_checkpointer(),
    )
    # 每个模式独立 thread_id，对话记忆互不污染
    return agent, {"configurable": {"thread_id": f"mode-{mode}"}}


CHAT_MAX_TOOL_ROUNDS = 6  # 两阶段各自的工具调用轮数上限，防止失控


def _msg_reasoning(resp) -> str:
    """提取模型自带的思考过程（deepseek-reasoner / GLM thinking 等返回的 reasoning_content）。"""
    kw = getattr(resp, "additional_kwargs", {}) or {}
    r = kw.get("reasoning_content")
    return r.strip() if isinstance(r, str) and r.strip() else ""


def _stream_round(model_bt, convo, on_delta=None):
    """跑一轮对话（bind_tools 后的模型）。

    on_delta 传入时用 stream 模式：文本增量逐段回调（reasoning_content 静默累积，
    不回调）；否则用 invoke。返回 (content, reasoning 原始累积, tool_calls, 本轮完整消息)。
    """
    if on_delta is None:
        resp = model_bt.invoke(convo)
        kw = getattr(resp, "additional_kwargs", {}) or {}
        return resp.content, (kw.get("reasoning_content") or ""), (resp.tool_calls or []), resp
    final = None
    reasoning = ""
    for chunk in model_bt.stream(convo):
        final = chunk if final is None else final + chunk  # AIMessageChunk 可累加
        kw = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning += kw.get("reasoning_content") or ""
        delta = chunk.content
        if isinstance(delta, str) and delta:
            on_delta(delta)
    if final is None:
        return "", "", [], None
    return final.content, reasoning, (final.tool_calls or []), final


def _chat_loop(
    model: ChatOpenAI, system_content: str, messages: list, tools: list,
    trace: list | None = None, on_delta=None,
):
    """chat 模式的内部对话循环：模型可自主调用文献/数据检索工具。

    与 agent 模式的区别：只挂查询+文献类工具（无 shell），轮数有上限，
    整个循环的中间过程（工具调用与结果）不进入对话历史。
    trace 传入列表时记录每次工具调用（对话界面展示思考过程用）。
    on_delta 传入时流式回调每轮文本增量（对话界面逐段渲染用）。
    返回 (回复文本, reasoning_content 或 "")；回复为各轮文本的拼接
    （通常只有最后一轮有文本，中间轮的简短说明也会保留）。
    """
    if not tools:
        content, reasoning, _, _ = _stream_round(
            model, [SystemMessage(content=system_content), *messages], on_delta)
        return content, reasoning.strip()

    model_bt = model.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    convo = [SystemMessage(content=system_content), *messages]
    reasoning = ""
    texts: list = []
    for _ in range(CHAT_MAX_TOOL_ROUNDS):
        content, round_reasoning, tool_calls, msg = _stream_round(model_bt, convo, on_delta)
        reasoning = reasoning or round_reasoning.strip()
        if isinstance(content, str) and content:
            texts.append(content)
        if not tool_calls:
            return "\n\n".join(texts), reasoning
        convo.append(msg)
        for tc in tool_calls:
            tool = by_name.get(tc["name"])
            result = tool.invoke(tc["args"]) if tool else f"未知工具: {tc['name']}"
            convo.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            if trace is not None:
                trace.append({
                    "tool": tc["name"],
                    "args": {k: str(v)[:80] for k, v in (tc["args"] or {}).items()},
                    "result": str(result)[:150],
                })
    return "\n\n".join(texts), reasoning  # 轮数上限兜底


def chat_reply(
    model: ChatOpenAI, chat_history: list, question: str, custom_prompt: str = "",
    attachments: list[str] | None = None,
    on_draft=None, on_answer=None,
):
    """学术问答模式：两阶段生成，两阶段均可自主调用文献/数据检索工具。

    对用户而言仍是一次性输出；草稿与工具调用的中间过程不进入对话历史。
    custom_prompt 为可选附加提示词，草稿和终答阶段都会叠加。
    attachments 为待附加的文件路径列表（文本拼进提问、图片转多模态 block）；
    附件全文只出现在本轮的模型请求里，对话历史只留文件名，避免历史无限膨胀。
    on_draft / on_answer：草稿、终答阶段的流式增量回调（对话界面传，
    逐段渲染；REPL 不传即无流式）。
    环境变量 CHAT_TWO_STAGE=0 时切换为单阶段直答（1~2 次模型调用，更省）。
    返回 (回复文本, 思考过程 dict)：
    thinking = {"draft": 两阶段草稿或 None, "trace": 工具调用轨迹,
                "reasoning": 模型自带 reasoning_content 或 ""}
    """
    tools = get_tools("query")  # 查询类 + 文献类（arXiv/ADS），无 shell
    attach = [a for a in (attachments or []) if a]
    # 本轮真正发给模型的消息（含附件全文/图片），与写进历史的轻量版分开
    human_now = HumanMessage(content=_compose_question(question, attach))
    chat_history.append(HumanMessage(content=_history_note(question, attach)))
    trace: list = []
    thinking = {"draft": None, "trace": trace, "reasoning": ""}
    if os.environ.get("CHAT_TWO_STAGE", "1").lower() in ("0", "false", "no", "off"):
        # 单阶段直答：一次调用，模型仍可自主查文献/数据
        reply, thinking["reasoning"] = _chat_loop(
            model,
            compose_system(CHAT_PROMPT, custom_prompt),
            [*chat_history[:-1], human_now],  # 历史 + 含附件的本次提问
            tools,
            trace=trace,
            on_delta=on_answer,
        )
    else:
        # 阶段一：内部草稿，自由推理试错，可查文献/天体数据
        draft, _ = _chat_loop(
            model,
            compose_system(CHAT_DRAFT_PROMPT, custom_prompt),
            [human_now],
            tools,
            trace=trace,
            on_delta=on_draft,
        )
        thinking["draft"] = draft
        # 阶段二：基于草稿输出结构化、标注置信度的最终回答
        reply, thinking["reasoning"] = _chat_loop(
            model,
            compose_system(CHAT_PROMPT, custom_prompt),
            [
                *chat_history[:-1],  # 此前的对话历史
                HumanMessage(content=_compose_question(
                    question, [],
                    extra=f"--- 内部思考草稿（供核查、修正、取舍）---\n{draft}",
                )),
            ],
            tools,
            trace=trace,
            on_delta=on_answer,
        )
    chat_history.append(AIMessage(content=reply))
    return reply, thinking


def handle_command(text: str, mode: str, custom_prompts: dict, ctx: dict | None = None) -> str:
    """处理斜杠命令，返回（可能更新后的）模式。

    custom_prompts: 模式名 -> 附加提示词。/prompt 命令直接修改此字典；
    agent 实例的重建由 main 循环检测字典变化后自动进行。
    ctx: 运行时共享状态（profiles/current/model/agents/chat_history），
    /model 热切换通过它完成；传 None 时 /model 不可用。
    """
    parts = text.split()
    cmd = parts[0].lower()
    if cmd == "/mode":
        if len(parts) == 1:
            print(f"当前模式: {mode}")
            for name, cfg in MODE_CONFIGS.items():
                mark = "  ← 当前" if name == mode else ""
                print(f"  {name:<9} {cfg['desc']}{mark}")
            print("切换: /mode <名称>")
        elif parts[1] in MODE_CONFIGS:
            if parts[1] != mode:
                print(f"已切换到 {parts[1]} 模式（各模式记忆独立）")
            return parts[1]
        else:
            print(f"未知模式。可用: {'/'.join(MODE_CONFIGS)}")
    elif cmd == "/model":
        if ctx is None:
            print("/model 仅在交互主循环中可用（需要运行时上下文）")
        elif len(parts) == 1:
            cur = ctx["current"]
            print(f"当前模型: {cur} ({ctx['profiles'].get(cur, {}).get('model', '?')})")
            for name, p in ctx["profiles"].items():
                mark = "  ← 当前" if name == cur else ""
                print(f"  {name:<10} {p['model']}{mark}")
            print("切换: /model <档案名>（在 .env 中配置，如 MODEL_NAME_DEEPSEEK=deepseek-chat）")
        elif parts[1] in ctx["profiles"]:
            name = parts[1]
            if name == ctx["current"]:
                print(f"当前已是 {name} ({ctx['profiles'][name]['model']})")
                return mode
            try:
                new_model = build_model(ctx["profiles"][name])
            except ValueError as e:
                print(f"[切换失败] {e}")
                return mode
            old_name = ctx["current"]
            old_model = ctx["profiles"][old_name]["model"]
            ctx["model"] = new_model
            ctx["current"] = name
            note = (
                f"[系统提示] 底层模型已从 {old_model} 切换为 {ctx['profiles'][name]['model']}。"
                "以上历史由前一个模型生成，请无缝衔接当前任务，保持已有结论一致，"
                "不要重复已完成的工作。"
            )
            # agent 模式：交接说明直接写入持久历史（update_state 只改状态，不触发模型调用）
            for rec in ctx["agents"].values():
                try:
                    state = rec["agent"].get_state(rec["config"])
                    if state.values.get("messages"):
                        rec["agent"].update_state(
                            rec["config"], {"messages": [HumanMessage(content=note)]}
                        )
                except Exception:
                    pass
            ctx["agents"].clear()  # 各模式下次使用时用新模型重建；历史在 checkpointer 中不丢失
            if ctx["chat_history"]:  # chat 模式的内存历史同样注入交接说明
                ctx["chat_history"].append(HumanMessage(content=note))
            print(f"已切换模型: {old_name}({old_model}) → {name}({ctx['profiles'][name]['model']})")
            print("对话历史已保留：新模型会读取此前全部进展（含一条交接说明）。")
        else:
            print(
                f"未知模型档案。可用: {'/'.join(ctx['profiles'])}"
                "；先在 .env 中配置 MODEL_NAME_<名字>"
            )
    elif cmd == "/prompt":
        if len(parts) == 1:
            current = custom_prompts.get(mode, "")
            print(f"[{mode}] 附加提示词: {current if current else '（未设置）'}")
            print("设置: /prompt <内容> | 清除: /prompt off")
        elif parts[1].lower() == "off":
            if custom_prompts.pop(mode, None):
                print(f"已清除 {mode} 模式的附加提示词")
            else:
                print(f"{mode} 模式本就没有附加提示词")
        else:
            content = text.split(None, 1)[1].strip()
            custom_prompts[mode] = content
            print(f"已为 {mode} 模式设置附加提示词（对后续回答生效）:\n  {content}")
    elif cmd == "/file":
        # 附件管理：chat 模式把文件内容递给模型；其他模式本就有 read_file 工具，仅提示
        files = ctx.setdefault("attachments", []) if ctx is not None else []
        rest = text.split(None, 1)
        sub = rest[1].strip() if len(rest) > 1 else ""

        if not sub:
            if files:
                print(f"已附加 {len(files)} 个文件：")
                for i, f in enumerate(files, 1):
                    print(f"  {i}. {f}")
                print("移除: /file -<编号>   全部清除: /file clear")
            else:
                print("当前没有附加文件。")
            print("附加: /file <路径> [更多路径…]   （文本/源码/CSV/JSON/PDF/图片）")
            print("清除: /file clear          移除单个: /file -1")
        elif sub.lower() == "clear":
            files.clear()
            print("已清除全部附加文件")
        elif sub.startswith("-") and sub[1:].isdigit():
            idx = int(sub[1:]) - 1
            if 0 <= idx < len(files):
                print(f"已移除: {files.pop(idx)}")
            else:
                print(f"编号超出范围（当前 {len(files)} 个）")
        else:
            added, failed = [], []
            for raw in sub.split():
                path = raw.strip().strip('"').strip("'")
                got = _read_attachment(path)
                if got["ok"]:
                    files.append(got["path"])
                    added.append(got["note"])
                else:
                    failed.append(got["note"])
            for a in added:
                print(f"[已附加] {a}")
            for f in failed:
                print(f"[跳过] {f}")
            if added:
                print(f"提问时会一并发送（共 {len(files)} 个）。清除: /file clear")
                if mode != "chat":
                    print(f"提示：当前是 {mode} 模式，附件只在 chat 模式生效；"
                          f"{mode} 模式可用 read_file 工具直接读文件。")
    elif cmd == "/view":
        rest = text.split(None, 1)
        if len(rest) < 2:
            print("用法: /view <文件> [起始行|起始-结束]    例: /view tools.py 100-150")
        else:
            args = rest[1].split()
            lines = None
            if len(args) > 1:
                lines = parse_range(args[1])
                if lines is None:
                    print(f"行号参数无效: {args[1]!r}  例: /view tools.py 100-150")
                    return mode
            view_file(args[0], lines)
    elif cmd == "/tree":
        depth = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2
        list_tree(".", depth)
    elif cmd == "/browse":
        try:
            from browser import run_browser  # 懒导入：未装 textual 不影响主程序启动

            run_browser()
        except Exception as e:
            print(f"[浏览器无法启动] {e}\n提示: ./.conda/python.exe -m pip install textual")
    elif cmd == "/help":
        print(HELP_TEXT)
    else:
        print("未知命令。可用: /mode [模式名]、/model [档案名]、/view [文件]、/tree、/browse、/prompt [内容]、/help")
    return mode


def main():
    profiles = list_model_profiles()
    agents = {}  # 模式名 -> (agent, config)，首次使用时懒创建

    # 启动参数：--chat 等价于 --mode chat；--mode <名称> 进入指定模式；
    # --model <档案名> 指定初始模型；--prompt "<内容>" 为启动模式预置附加提示词；
    # --repl 使用旧式逐行交互（默认是对话式界面）
    mode = "agent"
    if "--chat" in sys.argv:
        mode = "chat"
    for i, arg in enumerate(sys.argv):
        if arg == "--mode" and i + 1 < len(sys.argv) and sys.argv[i + 1] in MODE_CONFIGS:
            mode = sys.argv[i + 1]
    start_profile = "default"
    for i, arg in enumerate(sys.argv):
        if arg == "--model" and i + 1 < len(sys.argv) and sys.argv[i + 1] in profiles:
            start_profile = sys.argv[i + 1]

    try:
        model = build_model(profiles.get(start_profile))
    except ValueError as e:
        raise SystemExit(f"{e}——请先复制 .env.example 为 .env，填入 API_KEY 后再运行。")

    custom_prompts: dict[str, str] = {}  # 模式名 -> 附加提示词（可有可无）
    for i, arg in enumerate(sys.argv):
        if arg == "--prompt" and i + 1 < len(sys.argv):
            custom_prompts[mode] = sys.argv[i + 1]

    ctx = {  # 共享运行时状态：REPL 与对话界面共用，handle_command 直接修改其中的字段
        "profiles": profiles,
        "current": start_profile,
        "model": model,
        "agents": agents,
        "chat_history": [],  # chat 模式的独立历史
        "custom_prompts": custom_prompts,
        "attachments": [],  # chat 模式的待附加文件（/file 管理）
    }

    if "--repl" in sys.argv:
        _repl(mode, ctx)
        return
    try:
        from chatui import run_chat  # 懒导入：textual 缺失时给出可读提示
    except ImportError as e:
        raise SystemExit(f"对话界面需要 textual：./.conda/python.exe -m pip install textual\n({e})")
    run_chat(mode, ctx)


def _repl(mode: str, ctx: dict) -> None:
    """旧式逐行交互（--repl 启动）。默认入口是对话式界面（chatui）。"""
    custom_prompts = ctx["custom_prompts"]
    chat_history = ctx["chat_history"]
    agents = ctx["agents"]

    print("=== 天文 Agent（REPL 模式）===")
    cur_prof = ctx["profiles"].get(ctx["current"], {})
    print(f"当前模型: {ctx['current']} ({cur_prof.get('model', '?')})")
    print(f"当前模式: {mode} — {MODE_CONFIGS[mode]['desc']}")
    if mode in custom_prompts:
        print(f"附加提示词: {custom_prompts[mode]}")
    print("所有模式:")
    for name, cfg in MODE_CONFIGS.items():
        print(f"  {name:<9} {cfg['desc']}")
    print("输入 /help 查看命令（含 /prompt 自定义提示词），q 退出\n")

    while True:
        question = input(f"[{mode}] 你: ").strip()
        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            break
        if question.startswith("/"):
            mode = handle_command(question, mode, custom_prompts, ctx)
            continue
        try:
            if mode == "chat":
                reply, _ = chat_reply(
                    ctx["model"], chat_history, question, custom_prompts.get("chat", ""),
                    attachments=ctx.get("attachments") or [],
                )
                print(f"\n天文Agent: {reply}\n")
                if _artifact_auto_fetch(ctx.get("current")):
                    try:
                        note = artifact_note(fetch_artifacts(reply))
                        if note:
                            print(f"{note}\n")
                    except Exception as e:
                        print(f"[抓取产物出错] {type(e).__name__}: {e}\n")
            else:
                # agent 实例懒创建；附加提示词或模型变化后自动重建（历史保留在同 thread_id）
                current_custom = custom_prompts.get(mode, "")
                if (
                    mode not in agents
                    or agents[mode]["custom"] != current_custom
                    or agents[mode]["model"] != ctx["current"]
                ):
                    agent, config = build_agent(ctx["model"], mode, current_custom)
                    agents[mode] = {
                        "agent": agent,
                        "config": config,
                        "custom": current_custom,
                        "model": ctx["current"],
                    }
                agent = agents[mode]["agent"]
                config = agents[mode]["config"]
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": question}]},
                    config=config,
                )
                # shell 工具触发人工审批：agent 暂停，循环处理直到无待审批项
                while "__interrupt__" in result:
                    req = result["__interrupt__"][0].value
                    print("\n[请求执行命令，请审批]")
                    print(req.get("description") or req)
                    choice = input("y=批准 / n=拒绝 / 直接输入替代命令: ").strip()
                    if choice.lower() == "y":
                        decision = {"type": "approve"}
                    elif choice.lower() == "n":
                        decision = {
                            "type": "reject",
                            "message": "用户拒绝了这条命令，请改用其他方式完成，或询问用户怎么办。",
                        }
                    else:  # 用户输入了修改后的命令
                        _reqs = (req or {}).get("action_requests") or []
                        _tool = _reqs[0].get("name", "run_powershell") if _reqs else "run_powershell"
                        decision = {"type": "edit",
                                    "edited_action": {"name": _tool, "args": {"command": choice}}}
                    result = agent.invoke(Command(resume={"decisions": [decision]}), config=config)
                _ans = result['messages'][-1].content
                print(f"\n天文Agent: {_ans}\n")
                if _artifact_auto_fetch(ctx.get("current")):
                    try:
                        _note = artifact_note(fetch_artifacts(str(_ans)))
                        if _note:
                            print(f"{_note}\n")
                    except Exception as e:
                        print(f"[抓取产物出错] {type(e).__name__}: {e}\n")
        except Exception as e:
            print(f"\n[出错] {enrich_error(e, ctx.get('attachments') or [])}\n")


if __name__ == "__main__":
    main()
