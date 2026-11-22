"""天文工具集：agent 的"手"。

每个函数就是一个工具，@tool 装饰器会把函数签名和 docstring
告诉大模型，模型决定何时调用、传什么参数。
所有工具返回字符串（出错也返回错误说明，让模型自己纠正）。
"""

import astropy.units as u
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Annotated

from astropy.utils.iers import conf as iers_conf
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

# 不在线下载 IERS 地球自转表，用 astropy 内置表（精度到角秒级，天文问答足够用），
# 既避免下载失败刷警告，也让地平坐标计算完全离线可用
iers_conf.auto_download = False
from astropy.time import Time
from astroquery.simbad import Simbad

# CDS 主站（simbad.cds.unistra.fr）正在迁移，/simbad 路径暂时 404。
# 官方公告指定哈佛镜像为替代端点，接口和数据完全一致。
Simbad.server = "simbad.harvard.edu"
from langchain.tools import tool

# ========== 防线一：可恢复引用（Manus 思想）==========
# 大结果落盘 results/，上下文只留"摘要前缀 + 引用路径"——
# 即使之后被压缩/摘要，原始数据也始终可在磁盘读回。

_RESULTS_DIR = Path(__file__).parent / "results"
_BLOCKS_FILE = _RESULTS_DIR / "context_blocks.json"
_PROTECT_RECENT = 8  # compress 保护区：最近 8 条消息不压缩


def _save_and_reference(name: str, content: str, keep_chars: int = 500) -> str:
    """结果落盘，返回"摘要前缀 + 引用路径"。文件名清洗为安全字符。"""
    _RESULTS_DIR.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:60]
    ts = datetime.now().strftime("%m%d_%H%M%S")
    path = _RESULTS_DIR / f"{safe}_{ts}.txt"
    path.write_text(content, encoding="utf-8")
    head = content[:keep_chars].rstrip()
    return f"{head}\n\n[完整结果共 {len(content)} 字符，已存盘: results/{path.name}]"


@tool
def query_object(name: str) -> str:
    """按名称查询天体（恒星/星系/星云等）的基本信息：主名、赤经、赤纬。

    name 用英文通用名或编号，如 "Vega"、"M31"、"NGC 1277"。
    """
    try:
        table = Simbad.query_object(name)
        if table is None or len(table) == 0:
            return f"SIMBAD 中未找到天体: {name}（试试英文名或星表编号，如 M31、HD 172167）"
        row = table[0]
        ra, dec = float(row["ra"]), float(row["dec"])
        coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
        content = (
            f"天体 {name} 的信息：\n"
            f"- SIMBAD 主标识: {row['main_id']}\n"
            f"- 赤经 RA: {coord.ra.to_string(unit=u.hour, sep=':', precision=1)}\n"
            f"- 赤纬 Dec: {coord.dec.to_string(sep=':', precision=1)}\n"
            f"- 十进制坐标: RA={ra:.5f}°, Dec={dec:.5f}°"
        )
        return _save_and_reference(f"simbad_{name.replace(' ', '_')}", content, keep_chars=400)
    except Exception as e:
        return f"查询 {name} 失败: {e}"


@tool
def angular_separation(name1: str, name2: str) -> str:
    """计算两个天体在天球上的角距离，单位为度。参数为天体名称（英文/编号）。"""
    try:
        c1 = SkyCoord.from_name(name1)
        c2 = SkyCoord.from_name(name2)
        sep = c1.separation(c2).to(u.deg)
        return f"{name1} 与 {name2} 的角距离约 {sep.value:.3f}°（{sep.to(u.arcmin).value:.1f} 角分）"
    except Exception as e:
        return f"计算 {name1} 与 {name2} 的角距离失败: {e}"


@tool
def altitude_azimuth(name: str, lat: float, lon: float) -> str:
    """计算天体"此刻"在指定观测地点的地平坐标（高度角/方位角），判断现在是否适合观测。

    name: 天体名称（英文/编号）
    lat / lon: 观测者的纬度、经度，十进制度（东经为正、北纬为正），如北京 lat=39.9, lon=116.4
    """
    try:
        coord = SkyCoord.from_name(name)
        now = Time.now()
        place = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
        horizon = coord.transform_to(AltAz(obstime=now, location=place))
        alt = horizon.alt.deg
        if alt > 0:
            hint = "当前在地平线以上，可以观测"
        else:
            hint = "当前在地平线以下，无法观测"
        return (
            f"{name} 当前（UTC {now.iso[:16]}）的地平坐标：\n"
            f"- 高度角: {alt:.1f}°\n"
            f"- 方位角: {horizon.az.deg:.1f}°（0°=北, 90°=东）\n"
            f"- {hint}"
        )
    except Exception as e:
        return f"计算 {name} 的地平坐标失败: {e}"


# ========== 文献检索工具（arXiv 零配置可用；ADS 需在 .env 配置 ADS_TOKEN） ==========

@tool
def search_arxiv(query: str, max_results: int = 5) -> str:
    """在 arXiv 上检索论文（preprint，天体物理主流渠道）。返回标题/作者/日期/摘要/arXiv ID。

    query 用 arXiv 检索语法：'all:fast radio burst'、
    'cat:astro-ph.HE AND all:magnetar'（cat: 为分类，astro-ph.HE 高能天体物理、
    astro-ph.GA 星系、astro-ph.SR 恒星与太阳、astro-ph.EP 行星）。
    max_results 最多 10。
    """
    try:
        params = urllib.parse.urlencode({
            "search_query": query,
            "start": 0,
            "max_results": max(1, min(max_results, 10)),
            "sortBy": "relevance",
        })
        req = urllib.request.Request(
            f"https://export.arxiv.org/api/query?{params}",
            headers={"User-Agent": "astro-agent/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            root = ET.fromstring(resp.read().decode("utf-8", errors="replace"))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall("a:entry", ns)
        if not entries:
            return f"arXiv 未检索到相关论文: {query}（试试英文关键词或 cat: 分类语法）"
        lines = []
        for i, e in enumerate(entries, 1):
            title = " ".join(e.findtext("a:title", "", ns).split())
            authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
            author_str = ", ".join(authors[:4]) + (" 等" if len(authors) > 4 else "")
            date = e.findtext("a:published", "", ns)[:10]
            aid = e.findtext("a:id", "", ns).split("/abs/")[-1]
            summary = " ".join(e.findtext("a:summary", "", ns).split())[:280]
            lines.append(f"{i}. [{date}] {title}\n   作者: {author_str}\n   arXiv: {aid}\n   摘要: {summary}...")
        return _save_and_reference(f"arxiv_{query[:20]}", "\n\n".join(lines), keep_chars=900)
    except Exception as e:
        return f"arXiv 检索失败: {e}"


@tool
def search_ads(query: str, max_results: int = 5) -> str:
    """在 NASA ADS 检索正式发表的期刊论文（ApJ、MNRAS、A&A 等）。

    query 用 ADS 语法：'title:magnetar author:zhang'、'abs:"fast radio burst"'。
    需要 API token：到 https://ui.adsabs.harvard.edu/user/settings/token 免费申请，
    在 .env 中配置 ADS_TOKEN；未配置时返回引导信息（可先用 search_arxiv 查 preprint）。
    """
    try:
        token = os.environ.get("ADS_TOKEN")
        if not token:
            return (
                "ADS 检索需要 API token（免费）：\n"
                "1. 到 https://ui.adsabs.harvard.edu/user/settings/token 申请\n"
                "2. 在 .env 中添加一行 ADS_TOKEN=你的token\n"
                "当前可先用 search_arxiv 检索 preprint。"
            )
        import requests
        r = requests.post(
            "https://api.adsabs.harvard.edu/v1/search/query",
            json={
                "q": query,
                "rows": max(1, min(max_results, 10)),
                "fl": ["title", "author", "pubdate", "bibcode", "abstract"],
                "sort": "score desc",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        if not docs:
            return f"ADS 未检索到相关论文: {query}"
        lines = []
        for i, doc in enumerate(docs, 1):
            title = (doc.get("title") or ["(无标题)"])[0]
            authors = doc.get("author", [])
            author_str = ", ".join(authors[:4]) + (" 等" if len(authors) > 4 else "")
            lines.append(
                f"{i}. [{doc.get('pubdate', '?')[:7]}] {title}\n"
                f"   作者: {author_str}\n   bibcode: {doc.get('bibcode', '?')}"
            )
        return _save_and_reference(f"ads_{query[:20]}", "\n\n".join(lines), keep_chars=700)
    except Exception as e:
        return f"ADS 检索失败: {e}"


# ========== 防线二：简化版 ACP——模型自主压缩/恢复/搜索历史（append-only 安全） ==========
# 思想来自 ranxianglei/opencode-acp 与 pai-acp：把压缩决策权交给模型，
# 被压缩内容存入块注册表（results/context_blocks.json），可 decompress 恢复、
# search_context 搜索。实现约束：绝不原地改写消息（保护 prompt cache），
# 只通过 RemoveMessage + 追加摘要块的 langgraph 官方机制操作。


def _load_blocks() -> dict:
    if _BLOCKS_FILE.exists():
        return json.loads(_BLOCKS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_blocks(registry: dict) -> None:
    _RESULTS_DIR.mkdir(exist_ok=True)
    _BLOCKS_FILE.write_text(
        json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8"
    )


@tool
def compress(
    summary: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """把较早的对话历史压缩为一个摘要块，释放上下文空间。

    summary 必须保留：全部数值结果与天体坐标、文件路径、关键决策及理由、
    未完成的任务、报错信息。最近几轮对话和最后一条用户消息不会被压缩。
    压缩后的块可用 decompress 恢复原文，或用 search_context 搜索。
    """
    msgs = state.get("messages", [])
    if len(msgs) <= _PROTECT_RECENT + 2:
        return Command(
            update={"messages": [ToolMessage("历史太短，无需压缩", tool_call_id=tool_call_id)]}
        )
    range_msgs = msgs[:-_PROTECT_RECENT]
    if not range_msgs:
        return Command(
            update={"messages": [ToolMessage("没有可压缩的早期历史", tool_call_id=tool_call_id)]}
        )

    registry = _load_blocks()
    bid = f"b{len(registry) + 1}"
    registry[bid] = {
        "summary": summary,
        "created": datetime.now().isoformat(timespec="seconds"),
        "original": [
            {"type": type(m).__name__, "content": str(m.content)[:3000]} for m in range_msgs
        ],
        "compressed_ids": [m.id for m in range_msgs if m.id],
    }
    _save_blocks(registry)

    updates: list = [RemoveMessage(id=m.id) for m in range_msgs if m.id]
    updates.append(
        HumanMessage(
            content=f"[压缩块 {bid}]（{len(range_msgs)} 条历史已压缩，"
            f"decompress 恢复 / search_context 搜索）\n{summary}"
        )
    )
    updates.append(
        ToolMessage(
            content=f"已压缩 {len(range_msgs)} 条消息为块 {bid}。摘要已写入上下文。",
            tool_call_id=tool_call_id,
        )
    )
    return Command(update={"messages": updates})


@tool
def decompress(block_id: str) -> str:
    """恢复某个压缩块的完整原文到上下文（block_id 如 b1、b2）。"""
    registry = _load_blocks()
    if block_id not in registry:
        return f"未找到块 {block_id}。已有块: {', '.join(registry) or '无'}"
    block = registry[block_id]
    text = "\n".join(f"[{m['type']}] {m['content']}" for m in block["original"])
    return f"块 {block_id} 原文（{len(block['original'])} 条消息）：\n{text}"


@tool
def search_context(keyword: str) -> str:
    """在所有压缩块的原文和摘要中搜索关键词（无需解压），返回命中片段。"""
    keyword_l = keyword.lower()
    hits = []
    for bid, block in _load_blocks().items():
        if keyword_l in block["summary"].lower():
            hits.append(f"[{bid} 摘要] {block['summary'][:200]}")
        for m in block["original"]:
            content = m["content"]
            pos = content.lower().find(keyword_l)
            if pos >= 0:
                start = max(0, pos - 40)
                hits.append(f"[{bid} {m['type']}] ...{content[start:pos + 80]}...")
    return "\n".join(hits[:12]) if hits else f"所有压缩块中未找到: {keyword}"


def get_tools(subset: str = "all"):
    """返回工具子集，供不同模式的 agent 组装使用。

    subset="all"      查询 + 文献 + 上下文压缩 + shell（agent/code 模式）
    subset="research" 查询 + 文献 + 上下文压缩（research 模式，无 shell）
    subset="query"    查询 + 文献（chat 模式，无 shell 无压缩——其循环不走 agent state）
    """
    query_tools = [query_object, angular_separation, altitude_azimuth]
    literature_tools = [search_arxiv, search_ads]
    context_tools = [compress, decompress, search_context]
    if subset == "query":
        return [*query_tools, *literature_tools]
    if subset == "research":
        return [*query_tools, *literature_tools, *context_tools]
    return [*query_tools, *literature_tools, *context_tools, run_powershell, run_bash]


# ========== Shell 执行工具（高危能力：main.py 中已配置人工审批） ==========

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_TIMEOUT_SECONDS = 60
_MAX_OUTPUT_CHARS = 4000


def _find_bash() -> str:
    """定位 Git Bash 的 bash.exe。

    Windows 上 PATH 里排在前面的 `bash` 往往是 System32 中 WSL 的转发器
    （WSL 未安装时会报错），所以优先显式找 Git 安装目录下的 bash。
    """
    candidates = []
    git_on_path = shutil.which("git")
    if git_on_path:  # <Git根>/cmd/git.exe -> <Git根>/bin/bash.exe
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(git_on_path)), "bin", "bash.exe"))
    candidates += [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "bash"  # 兜底交给 PATH（Linux/macOS 上本来就叫 bash）


def _run(cmd_list: list, label: str) -> str:
    """统一执行子进程：限制超时、固定工作目录、截断超长输出。"""
    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            cwd=_TOOLS_DIR,  # 固定在项目目录，模型不会跑到别处乱执行
        )
        output = (result.stdout or "") + (result.stderr or "")
        output = output.strip() or "(命令执行完毕，无输出)"
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n...(输出过长已截断，共 {len(output)} 字符)"
        return f"[退出码 {result.returncode}]\n{output}"
    except subprocess.TimeoutExpired:
        return f"命令超时（>{_TIMEOUT_SECONDS} 秒），已中断"
    except Exception as e:
        return f"{label} 执行失败: {e}"


@tool
def run_powershell(command: str) -> str:
    """在 Windows PowerShell 中执行命令并返回输出。适合文件操作、查看系统信息、运行 Windows 程序。
    命令超过 60 秒会被中断；执行前需要用户在终端确认。
    """
    return _run(["powershell", "-NoProfile", "-Command", command], "PowerShell")


@tool
def run_bash(command: str) -> str:
    """在 bash（Git Bash）中执行命令并返回输出。适合文本处理、git 操作、类 Unix 工具链。
    命令超过 60 秒会被中断；执行前需要用户在终端确认。
    """
    return _run([_find_bash(), "-c", command], "bash")
