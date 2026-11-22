"""上下文归档完整性验收测试。

覆盖「无损原文归档 + 有损语义摘要 + 按需精确恢复」的七个关键场景：
  1. 10,000 字符长消息，恢复后与原文逐字一致（旧实现 [:3000] 会丢 70%）
  2. 含多个 tool_call 的消息，参数/ID/结果完整还原（旧实现只存 type+content）
  3. 不同 thread 之间不能交叉检索或解压（旧实现全局 json）
  4. 归档写入失败时，一条消息都不删除（旧实现会先删后写）
  5. 进程重启后仍能解压（数据在 SQLite，不在内存）
  6. 摘要触发后被移除的历史仍可找回
  7. 归档被篡改时校验失败并明确报错，而不是静默使用

直接运行：.conda\\Scripts\\python.exe tests\\test_context_archive.py
不依赖网络、不依赖真实 API key。
"""

import json
import os
import sqlite3
import sys
import types
from contextlib import closing
from pathlib import Path

# 让 tests/ 下的脚本能 import 项目根目录的 tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("API_KEY", "dummy")

import tools  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import HumanInTheLoopMiddleware  # noqa: E402
from langgraph.types import Command  # noqa: E402

PASS, FAIL = [], []


def check(cond, name, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


def mid_msgs(msgs):
    """给裸构造的消息补上 id——真实 LangGraph 线程里每条消息都带 id，
    compress 依赖 id 才能做 RemoveMessage。"""
    for i, m in enumerate(msgs):
        if not getattr(m, "id", None):
            m.id = f"m{i:04d}"
    return msgs


def rt(thread_id):
    """伪造工具运行时：tools._thread_of 只读 config.configurable.thread_id。

    直接调用 .func 时 pydantic 不做类型校验，用 SimpleNamespace 即可。
    """
    return types.SimpleNamespace(config={"configurable": {"thread_id": thread_id}})


def fresh_db(tmp_name):
    """把归档库指到临时文件，测试之间互不污染。"""
    p = Path(__file__).resolve().parent / f"_test_{tmp_name}.db"
    if p.exists():
        p.unlink()
    tools._BLOCKS_DB = p
    return p


# ---------------------------------------------------------------- 1. 长消息无损
def test_long_message():
    print("\n[1] 10,000 字符长消息完整恢复")
    fresh_db("long")
    body = "数值结果 " + "A1B2C3D4" * 1250  # 10,000+ 字符
    assert len(body) > 10000
    msgs = [HumanMessage(content="长问题"), AIMessage(content=body)]
    bid = tools._archive_block("mode-agent", "摘要", msgs)

    restored = tools._load_block("mode-agent", bid)
    check(len(restored) == 2, "消息条数一致")
    check(restored[1].content == body, "10,000+ 字符逐字一致",
          f"长度 {len(restored[1].content)} vs 原始 {len(body)}")
    check(len(restored[1].content) > 3000, "未被 3000 字符截断",
          f"实际 {len(restored[1].content)} 字符")


# ---------------------------------------------------- 2. 工具调用结构完整
def test_tool_call_structure():
    print("\n[2] 含多个 tool_call 的消息结构完整")
    fresh_db("toolcall")
    calls = [
        {"name": "search_arxiv", "args": {"query": "all:fast radio burst", "max_results": 3}, "id": "call_a"},
        {"name": "angular_separation", "args": {"name1": "Vega", "name2": "M31"}, "id": "call_b"},
    ]
    msgs = [
        HumanMessage(content="查文献并算角距"),
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content="arXiv 命中 3 篇", tool_call_id="call_a"),
        ToolMessage(content="66.526°", tool_call_id="call_b"),
    ]
    bid = tools._archive_block("mode-agent", "摘要", msgs)
    restored = tools._load_block("mode-agent", bid)

    ai = restored[1]
    check(isinstance(ai, AIMessage), "AI 消息类型还原")
    slim = [{k: c[k] for k in ("name", "args", "id")} for c in ai.tool_calls]
    check(slim == calls, "tool_calls 参数与 ID 完整", str(slim))
    tm = [m for m in restored if isinstance(m, ToolMessage)]
    check(len(tm) == 2, "两条工具结果都在")
    check({m.tool_call_id for m in tm} == {"call_a", "call_b"}, "tool_call_id 未被丢弃")


# -------------------------------------------------------- 3. 会话隔离
def test_thread_isolation():
    print("\n[3] 不同会话（模式）互不交叉")
    fresh_db("isolation")
    bid = tools._archive_block("mode-agent", "agent 的机密", [AIMessage(content="Vega RA=279.23473")])

    check(len(tools._load_block("mode-agent", bid)) == 1, "同 thread 可解压")
    try:
        tools._load_block("mode-research", bid)
        check(False, "跨 thread 解压应失败")
    except KeyError:
        check(True, "跨 thread 解压被拒绝")

    out = tools.decompress.func(**{"block_id": bid, "runtime": rt("mode-research")})
    check("未找到块" in out or "不存在或不属于当前会话" in out, "跨 thread decompress 明确拒绝", out[:80])

    hit = tools.search_context.func(**{"keyword": "279.23473", "runtime": rt("mode-research")})
    check("未找到" in hit, "跨 thread 检索搜不到", hit[:80])
    hit2 = tools.search_context.func(**{"keyword": "279.23473", "runtime": rt("mode-agent")})
    check("279.23473" in hit2, "同 thread 检索能命中")

    # 另一个 thread 用同名关键词存档，不应污染
    tools._archive_block("mode-research", "research 摘要", [AIMessage(content="Vega RA=279.23473")])
    rows = sqlite3.connect(tools._BLOCKS_DB).execute(
        "SELECT thread_id, COUNT(*) FROM context_blocks GROUP BY thread_id").fetchall()
    check(len(rows) == 2 and all(c == 1 for _, c in rows), "两会话各存一块，互不覆盖", str(rows))


# ------------------------------------------------ 4. 归档失败不删消息
def test_archive_failure_keeps_messages():
    print("\n[4] 归档写入失败时不删除任何消息")
    fresh_db("fail")
    msgs = mid_msgs([HumanMessage(content=f"问题{i}") for i in range(6)] + [AIMessage(content="答")])
    state = {"messages": msgs}

    orig = tools._archive_block
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error (模拟)")
    tools._archive_block = boom
    try:
        cmd = tools.compress.func(summary="摘要", state=state, tool_call_id="tc1",
                                  runtime=rt("mode-agent"))
        removed = [m for m in cmd.update["messages"] if m.__class__.__name__ == "RemoveMessage"]
        check(removed == [], "归档失败时没有产生任何 RemoveMessage", f"实际 {len(removed)} 条")
        note = str(cmd.update["messages"][-1].content)
        check("归档失败" in note and "未删除任何消息" in note, "返回明确的失败说明", note[:90])
    finally:
        tools._archive_block = orig

    # 归档正常时才删
    cmd = tools.compress.func(summary="摘要", state=state, tool_call_id="tc2",
                              runtime=rt("mode-agent"))
    removed = [m for m in cmd.update["messages"] if m.__class__.__name__ == "RemoveMessage"]
    check(len(removed) > 0, "归档成功时才删除消息", f"删了 {len(removed)} 条")


# -------------------------------------------------------- 5. 重启后仍可解压
def test_persistence_across_restart():
    print("\n[5] 重启后仍能解压（数据在 SQLite 而非内存）")
    db = fresh_db("persist")
    body = "持久化内容 " + "Z" * 8000
    bid = tools._archive_block("mode-agent", "摘要", [AIMessage(content=body)])

    # 模拟重启：丢掉模块级缓存，重新读库
    import importlib
    importlib.reload(tools)
    tools._BLOCKS_DB = db
    restored = tools._load_block("mode-agent", bid)
    check(restored[0].content == body, "重启后完整取出 8000+ 字符")
    check(len(restored[0].content) == len(body), "长度一致")


# -------------------------------------------------- 6. 摘要后历史仍可找回
def test_replaced_history_recoverable():
    print("\n[6] 摘要替换历史后，被移除的内容仍可找回")
    fresh_db("recover")
    # 需要超过保护区（最近 2 个用户轮次）才有可压缩内容，构造 4 轮
    msgs = []
    for i in range(4):
        msgs.append(HumanMessage(content=f"第{i}个问题：FRB 能量来源？"))
        msgs.append(AIMessage(content=f"第{i}轮结论：能量量级 1e40 erg"))
    state = {"messages": mid_msgs(msgs)}
    cmd = tools.compress.func(summary="FRB 研究进行中", state=state, tool_call_id="tc",
                              runtime=rt("mode-agent"))
    removed = [m for m in cmd.update["messages"] if m.__class__.__name__ == "RemoveMessage"]
    check(len(removed) > 0, f"确实移除了 {len(removed)} 条历史")

    # 从上下文里找 block_id
    human_note = [m for m in cmd.update["messages"] if isinstance(m, HumanMessage)][0].content
    import re
    bid = re.search(r"压缩块 (b[0-9a-f]+)", human_note).group(1)
    got = tools.decompress.func(block_id=bid, runtime=rt("mode-agent"))
    check("1e40 erg" in got, "被移除的数值仍能取回")
    check("第1轮结论" in got and "第0轮结论" in got, "被移除的多轮内容都在")
    check("第3轮结论" not in got, "受保护的最近轮次未被归档")
    hit = tools.search_context.func(keyword="1e40", runtime=rt("mode-agent"))
    check("1e40" in hit, "免解压检索也能命中")


# -------------------------------------------------------- 7. 篡改检测
def test_tamper_detection():
    print("\n[7] 归档被篡改时校验失败")
    fresh_db("tamper")
    bid = tools._archive_block("mode-agent", "摘要", [AIMessage(content="原始正确内容")])
    with closing(sqlite3.connect(tools._BLOCKS_DB)) as db:
        with db:
            db.execute("UPDATE context_blocks SET original_json = ? WHERE block_id = ?",
                       (json.dumps([{"type": "ai", "data": {"content": "篡改后的内容", "type": "ai"}}]), bid))
    try:
        tools._load_block("mode-agent", bid)
        check(False, "篡改后应校验失败")
    except ValueError as e:
        check("完整性校验失败" in str(e), "明确报错而非静默使用", str(e)[:60])

    out = tools.decompress.func(block_id=bid, runtime=rt("mode-agent"))
    check("失败" in out, "decompress 把校验失败回报给模型", out[:80])


# ------------------------------------------- 附加：切点安全 / 轮次边界 / 文件名
def test_cutoff_and_naming():
    print("\n[8] 压缩切点保护工具调用对 + 轮次边界 + 文件名不撞")
    msgs = [
        HumanMessage(content="轮1"),
        AIMessage(content="", tool_calls=[{"name": "run_bash", "args": {"command": "ls"}, "id": "c1"}]),
        ToolMessage(content="结果", tool_call_id="c1"),
        HumanMessage(content="轮2"),
        AIMessage(content="答2"),
        HumanMessage(content="轮3"),
        AIMessage(content="答3"),
    ]
    cut = tools._safe_cutoff(msgs, 2)  # 2 正好落在 ToolMessage 上
    check(cut <= 1, "切点不会落在孤立 ToolMessage 上", f"cut={cut}")
    targets, _ = tools._plan_compression(msgs)
    kinds = [type(m).__name__ for m in targets]
    check("ToolMessage" not in kinds or kinds.index("AIMessage") < kinds.index("ToolMessage"),
          "待压缩集合里工具结果不会与其调用拆散", str(kinds))
    check(all(not isinstance(m, SystemMessage) for m in targets), "系统消息不被压缩")

    # 轮次太少时不压缩
    few = [HumanMessage(content="只有一轮"), AIMessage(content="答")]
    empty, reason = tools._plan_compression(few)
    check(empty == [], "轮次不足时不压缩", reason)

    # 文件名唯一性（秒级时间戳会撞）
    fresh = Path(__file__).resolve().parent / "_t_results"
    tools._RESULTS_DIR = fresh
    a = tools._save_and_reference("simbad_Vega", "内容A")
    b = tools._save_and_reference("simbad_Vega", "内容B")
    fa, fb = a.split("results/")[-1].rstrip("]"), b.split("results/")[-1].rstrip("]")
    check(fa != fb, "同一秒两次同名查询不再覆盖", f"{fa} vs {fb}")
    for f in fresh.glob("*"):
        f.unlink()
    fresh.rmdir()
    tools._RESULTS_DIR = Path(tools.__file__).parent / "results"


if __name__ == "__main__":
    print("=" * 66)
    print("上下文归档完整性验收")
    print("=" * 66)
    test_long_message()
    test_tool_call_structure()
    test_thread_isolation()
    test_archive_failure_keeps_messages()
    test_persistence_across_restart()
    test_replaced_history_recoverable()
    test_tamper_detection()
    test_cutoff_and_naming()

    # 清理测试库
    for p in Path(__file__).resolve().parent.glob("_test_*.db"):
        p.unlink()

    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
