"""天文工具集：agent 的"手"。

每个函数就是一个工具，@tool 装饰器会把函数签名和 docstring
告诉大模型，模型决定何时调用、传什么参数。
所有工具返回字符串（出错也返回错误说明，让模型自己纠正）。
"""

import astropy.units as u
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated

from astropy.utils.iers import conf as iers_conf
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from langchain_core.messages import (
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import InjectedToolCallId
from langchain.tools import ToolRuntime, tool
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

# ========== 防线一：可恢复引用（Manus 思想）==========
# 大结果落盘 results/，上下文只留"摘要前缀 + 引用路径"——
# 即使之后被压缩/摘要，原始数据也始终可在磁盘读回。

_RESULTS_DIR = Path(__file__).parent / "results"


def _save_and_reference(name: str, content: str, keep_chars: int = 500) -> str:
    """结果落盘，返回"摘要前缀 + 引用路径"。

    文件名清洗为安全字符后带 uuid 后缀——原先用秒级时间戳，同一秒内两次同名
    查询（如连续查两次 Vega）会互相覆盖，底层结果静默丢失。
    """
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:60]
    ts = datetime.now().strftime("%m%d_%H%M%S")
    path = _RESULTS_DIR / f"{safe}_{ts}_{uuid.uuid4().hex[:8]}.txt"
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
        # ADS 的 /v1/search/query 只接受 GET（POST 会返回 405 METHOD NOT ALLOWED），
        # 参数走 query string，复数参数用逗号分隔。
        r = requests.get(
            "https://api.adsabs.harvard.edu/v1/search/query",
            params={
                "q": query,
                "rows": max(1, min(max_results, 10)),
                "fl": "title,author,pubdate,bibcode,abstract",
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


# ========== 防线二：模型自主压缩/恢复/搜索（无损归档 + 有损摘要 + 按需精确恢复）==========
# 原则：压缩只能减少模型看到的 token，不能让原始信息真的丢失。
#
#   完整历史 ──无损归档──► SQLite（原始消息 JSON + SHA-256 校验，按 thread_id 隔离）
#            ──有损摘要──► 几百 token 的结构化摘要（进入上下文）
#            ──按需恢复──► decompress(block_id) 取回完整原文 / search_context 免解压检索
#
# 与旧实现（results/context_blocks.json）的关键差异：
#   1. 不再 str(m.content)[:3000] 截断——用 messages_to_dict 原样序列化
#   2. 不再只存 type+content——工具调用参数、tool_call_id、元数据完整保留
#   3. 按 thread_id 隔离——四个模式互不可见
#   4. 摘要落库前做序列化往返校验 + 写入后校验 SHA-256，坏数据不静默使用
#   5. 归档失败时不删除原始消息（旧实现在归档异常时会丢掉历史）
#
# 实现约束（append-only，保护 prompt cache）：绝不原地改写消息，只通过
# RemoveMessage + 追加摘要块这一 langgraph 官方机制操作。

_BLOCKS_DB = _RESULTS_DIR / "context_blocks.db"

_BLOCKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_blocks (
    block_id      TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    summary       TEXT NOT NULL,
    original_json TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    created       TEXT NOT NULL,
    token_est     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocks_thread ON context_blocks(thread_id, created);
"""

_PROTECT_TURNS = 2  # 保护区：最近 N 个「用户对话轮次」不压缩（按 HumanMessage 计数）


def _blocks_conn() -> sqlite3.Connection:
    """打开归档库；表结构与目录首次调用时自动创建。"""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(_BLOCKS_DB)
    db.executescript(_BLOCKS_SCHEMA)
    return db


def _thread_of(runtime: ToolRuntime | None) -> str:
    """从工具运行时取出 thread_id（各模式独立，用于会话隔离）。"""
    cfg = (getattr(runtime, "config", None) or {}) if runtime is not None else {}
    tid = (cfg.get("configurable") or {}).get("thread_id")
    return str(tid) if tid else "unknown"


def _archive_block(thread_id: str, summary: str, messages: list) -> str:
    """把消息无损归档，返回 block_id。任何一步失败都抛异常（调用方据此中止压缩）。"""
    raw = json.dumps(
        messages_to_dict(messages),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    # 写入前验证可完整往返：防止存进去的东西解不出来
    decoded = json.loads(raw)
    if messages_to_dict(messages_from_dict(decoded)) != decoded:
        raise ValueError("消息序列化往返校验失败")

    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    block_id = f"b{uuid.uuid4().hex[:12]}"
    created = datetime.now().isoformat(timespec="seconds")
    token_est = sum(len(str(m.content)) for m in messages) // 3

    with closing(_blocks_conn()) as db:
        with db:
            db.execute(
                "INSERT INTO context_blocks"
                " (block_id, thread_id, summary, original_json, checksum, created, token_est)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (block_id, thread_id, summary, raw, checksum, created, token_est),
            )
    return block_id


def _load_block(thread_id: str, block_id: str) -> list:
    """取回并校验归档消息；不存在/校验失败/解码失败都抛异常。"""
    with closing(_blocks_conn()) as db:
        row = db.execute(
            "SELECT original_json, checksum FROM context_blocks"
            " WHERE thread_id = ? AND block_id = ?",
            (thread_id, block_id),
        ).fetchone()
    if row is None:
        raise KeyError(f"压缩块 {block_id} 不存在或不属于当前会话")
    raw, checksum = row
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != checksum:
        raise ValueError(f"压缩块 {block_id} 完整性校验失败（归档数据已被改动）")
    return messages_from_dict(json.loads(raw))


def _safe_cutoff(msgs: list, cut: int) -> int:
    """把切点向前挪到安全位置，避免拆开 AI 工具调用与对应结果。

    若切点正好落在一个 ToolMessage 上，说明它的发起方 AI 消息会被删掉而结果留下，
    这会让上下文出现孤立的工具结果。向前回退直到该处不再是工具结果。
    """
    while cut > 0 and isinstance(msgs[cut], ToolMessage):
        cut -= 1
    # 切点前一条若是带 tool_calls 的 AI 消息，说明这一对还没走完，一并保留
    while cut > 0 and getattr(msgs[cut - 1], "tool_calls", None):
        cut -= 1
    return cut


def _plan_compression(msgs: list) -> tuple[list, str]:
    """决定压缩哪些消息。返回 (待压缩消息, 说明)。

    按「用户轮次」而不是「消息条数」保护最近内容：一轮 agent 调用会产生
    多条 AI/Tool 消息，用固定条数做保护区很容易切在半路。
    """
    turn_starts = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
    if len(turn_starts) <= _PROTECT_TURNS:
        return [], f"只有 {len(turn_starts)} 个对话轮次（保护区 {_PROTECT_TURNS} 轮），无需压缩"

    cut = _safe_cutoff(msgs, turn_starts[-_PROTECT_TURNS])
    if cut <= 0:
        return [], "没有可压缩的早期历史（切点落在保护区起点）"

    candidates = [m for m in msgs[:cut] if not isinstance(m, SystemMessage)]
    if not candidates:
        return [], "早期历史只有系统消息，无需压缩"
    return candidates, ""


@tool
def compress(
    summary: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    runtime: ToolRuntime = None,
) -> Command:
    """把较早的对话历史压缩为一个摘要块，释放上下文空间。

    summary 必须保留：全部数值结果与天体坐标、文件路径、关键决策及理由、
    未完成的任务、报错信息。最近两个对话轮次和最后一条用户消息不会被压缩。
    被压缩的完整原文会无损归档，可用 decompress(block_id) 取回，
    或用 search_context 免解压检索。
    """
    msgs = list(state.get("messages", []))
    thread_id = _thread_of(runtime)

    targets, reason = _plan_compression(msgs)
    if not targets:
        return Command(
            update={"messages": [ToolMessage(reason, tool_call_id=tool_call_id)]}
        )

    # 只归档「能删掉的」消息：没有 id 的消息无法 RemoveMessage，若归档了却不删，
    # 摘要与原文会同时在上下文里出现，后续 decompress 还会重复一遍。
    removable = [m for m in targets if getattr(m, "id", None)]
    if not removable:
        return Command(update={"messages": [ToolMessage(
            "压缩中止：待压缩的消息都没有 id，无法从历史中移除。",
            tool_call_id=tool_call_id,
        )]})
    skipped = len(targets) - len(removable)

    # 先归档，成功后才动上下文；归档失败则一条消息都不删
    try:
        block_id = _archive_block(thread_id, summary, removable)
    except Exception as e:
        return Command(update={"messages": [ToolMessage(
            f"压缩中止：原文归档失败（{type(e).__name__}: {e}）。"
            "为免丢失历史，未删除任何消息。可改用更小的范围重试。",
            tool_call_id=tool_call_id,
        )]})

    updates: list = [RemoveMessage(id=m.id) for m in removable]
    updates.append(HumanMessage(
        content=(
            f"[压缩块 {block_id}]（{len(removable)} 条历史已无损归档；"
            f"decompress('{block_id}') 取回完整原文，search_context 可免解压检索）\n"
            f"{summary}"
        )
    ))
    updates.append(ToolMessage(
        content=(
            f"已归档并压缩 {len(removable)} 条消息为块 {block_id}"
            f"（原始 {sum(len(str(m.content)) for m in removable)} 字符完整保存）。"
            + (f" 另有 {skipped} 条无 id 的消息未处理。" if skipped else "")
        ),
        tool_call_id=tool_call_id,
    ))
    return Command(update={"messages": updates})


@tool
def decompress(block_id: str, runtime: ToolRuntime = None) -> str:
    """按 block_id（如 b1a2b3c4d5e6）取回某个压缩块的完整原文，不截断。

    恢复会占用较多上下文，只在确实需要核对细节（原始数值、完整工具输出、
    报错全文）时调用；只想确认某件事是否存在时先用 search_context。
    """
    thread_id = _thread_of(runtime)
    try:
        messages = _load_block(thread_id, block_id)
    except KeyError:
        with closing(_blocks_conn()) as db:
            rows = db.execute(
                "SELECT block_id FROM context_blocks WHERE thread_id = ? ORDER BY created",
                (thread_id,),
            ).fetchall()
        ids = ", ".join(r[0] for r in rows) or "无"
        return f"未找到块 {block_id}。当前会话已有块: {ids}"
    except Exception as e:
        return f"恢复块 {block_id} 失败：{type(e).__name__}: {e}"

    parts = []
    for i, m in enumerate(messages, 1):
        role = type(m).__name__
        body = str(m.content)
        calls = getattr(m, "tool_calls", None)
        extra = ""
        if calls:
            extra = "\n  工具调用: " + json.dumps(calls, ensure_ascii=False)
        parts.append(f"[{i}] {role}{extra}\n{body}")
    total = sum(len(str(m.content)) for m in messages)
    return (
        f"块 {block_id} 完整原文（{len(messages)} 条消息，共 {total} 字符，未截断）：\n\n"
        + "\n\n".join(parts)
    )


@tool
def search_context(keyword: str, runtime: ToolRuntime = None) -> str:
    """在当前会话已归档的历史中搜索关键词（免解压），返回命中片段。

    适合确认「之前算过的那个数值/引用过的那篇文献/那条报错」是否存在。
    只返回片段，取全文请用 decompress。
    """
    thread_id = _thread_of(runtime)
    kw = (keyword or "").strip()
    if not kw:
        return "请给出要搜索的关键词"
    like = f"%{kw}%"
    with closing(_blocks_conn()) as db:
        rows = db.execute(
            "SELECT block_id, summary, original_json FROM context_blocks"
            " WHERE thread_id = ? AND (summary LIKE ? OR original_json LIKE ?)"
            " ORDER BY created",
            (thread_id, like, like),
        ).fetchall()
    if not rows:
        return f"当前会话的归档中未找到: {kw}"

    hits = []
    low = kw.lower()
    for block_id, summary, original_json in rows:
        if low in summary.lower():
            hits.append(f"[{block_id} 摘要] {summary[:200]}")
        try:
            messages = messages_from_dict(json.loads(original_json))
        except Exception:
            continue
        for i, m in enumerate(messages, 1):
            content = str(m.content)
            pos = content.lower().find(low)
            if pos >= 0:
                start = max(0, pos - 40)
                hits.append(
                    f"[{block_id} 第{i}条 {type(m).__name__}] "
                    f"...{content[start:pos + len(kw) + 80]}..."
                )
    return "\n".join(hits[:12])


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
