import asyncio
import sqlite3
from contextlib import closing
from threading import Event
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, ListView, Markdown, Static

import chatui
import main
from chatstore import ChatStore
from chatui import ChatApp


def make_context(store, sid):
    history = store.load_messages(sid)
    return {
        "store": store,
        "session_id": sid,
        "chat_history": history,
        "saved_message_count": len(history),
        "current": "default",
        "model": object(),
        "profiles": {"default": {"model": "test-model"}},
        "custom_prompts": {},
        "attachments": [],
    }


@pytest.mark.parametrize("two_stage", ["0", "1"])
def test_failed_model_turn_leaves_history_unchanged(monkeypatch, two_stage):
    monkeypatch.setenv("CHAT_TWO_STAGE", two_stage)
    history = [HumanMessage(content="旧问题"), AIMessage(content="旧回答")]
    old_history = history.copy()
    calls = []

    def fail_on_answer(_model, _prompt, messages, *_args, **_kwargs):
        calls.append(messages)
        if two_stage == "1" and len(calls) == 1:
            return "草稿", ""
        raise RuntimeError("请求失败")

    monkeypatch.setattr(main, "_chat_loop", fail_on_answer)
    with pytest.raises(RuntimeError, match="请求失败"):
        main.chat_reply(object(), history, "新问题")

    assert len(calls) == int(two_stage) + 1
    assert history == old_history


def test_session_turn_rolls_back_if_saving_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    store = ChatStore(db_path)
    sid = store.create_session()
    ctx = make_context(store, sid)
    monkeypatch.setenv("CHAT_TWO_STAGE", "0")
    monkeypatch.setattr(main, "_chat_loop", lambda *args, **kwargs: ("回答", ""))
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("""
            CREATE TRIGGER fail_answer BEFORE INSERT ON chat_messages
            WHEN NEW.role = 'ai'
            BEGIN SELECT RAISE(ABORT, 'write failed'); END;
        """)

    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        main.session_chat_reply(ctx, "首个问题")

    assert ctx["chat_history"] == []
    assert store.load_messages(sid) == []
    assert store.get_title(sid) == "新会话"

    with closing(sqlite3.connect(db_path)) as db:
        db.execute("DROP TRIGGER fail_answer")

    reply, _ = main.session_chat_reply(ctx, "首个问题\n附加说明")
    assert reply == "回答"
    assert [m.content for m in store.load_messages(sid)] == ["首个问题\n附加说明", "回答"]
    assert [m.content for m in ctx["chat_history"]] == ["首个问题\n附加说明", "回答"]
    assert store.get_title(sid) == "首个问题"


def test_history_save_only_changes_modified_rows(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = ChatStore(db_path)
    sid = store.create_session("手动标题")
    first = [HumanMessage(content="旧问题"), AIMessage(content="旧回答")]
    store.replace_messages(sid, first)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE writes (op TEXT, idx INTEGER)")
        for op, row in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
            db.execute(f"""
                CREATE TRIGGER audit_{op} AFTER {op} ON chat_messages
                BEGIN INSERT INTO writes VALUES ('{op}', {row}.idx); END;
            """)

        store.replace_messages(sid, [*first, HumanMessage(content="新问题"), AIMessage(content="新回答")])
        assert db.execute("SELECT op, idx FROM writes ORDER BY idx, op").fetchall() == [
            ("insert", 2), ("insert", 3),
        ]
        with db:
            db.execute("DELETE FROM writes")

        store.replace_messages(sid, [first[0], AIMessage(content="更正")])
        assert db.execute("SELECT op, idx FROM writes ORDER BY idx, op").fetchall() == [
            ("update", 1), ("delete", 2), ("delete", 3),
        ]
    assert [m.content for m in store.load_messages(sid)] == ["旧问题", "更正"]
    assert store.get_title(sid) == "手动标题"


@pytest.mark.parametrize("entry", ["command", "sidebar", "shortcut", "browser"])
def test_session_switch_is_blocked_while_reply_is_running(tmp_path, monkeypatch, entry):
    store = ChatStore(tmp_path / "sessions.db")
    sid = store.create_session("当前会话")
    other = store.create_session("另一会话")
    store.replace_messages(sid, [HumanMessage(content="当前历史")])
    ctx = make_context(store, sid)
    app = ChatApp("chat", ctx)
    messages = []
    monkeypatch.setattr(app, "_append", messages.append)
    monkeypatch.setattr(app, "_refresh_sessions", lambda: None)
    monkeypatch.setattr(app, "_reload_chat_area", lambda: None)
    monkeypatch.setattr(app, "_update_title", lambda: None)
    number = next(i for i, session in enumerate(store.list_sessions(), 1) if session["id"] == other)
    command = f"/session {number}"
    actions = {
        "command": lambda: app._run_command(command),
        "sidebar": lambda: app.on_session_clicked(SimpleNamespace(item=SimpleNamespace(name=other))),
        "shortcut": app.action_new_session,
        "browser": lambda: app._run_command_from_browser(command),
    }

    app._busy = True
    actions[entry]()
    assert ctx["session_id"] == sid
    assert [m.content for m in ctx["chat_history"]] == ["当前历史"]
    assert {s["id"] for s in store.list_sessions()} == {sid, other}

    app._busy = False
    actions[entry]()
    assert ctx["session_id"] != sid
    assert ctx["chat_history"] == []
    assert ctx["saved_message_count"] == 0


def test_stale_session_does_not_overwrite_newer_turn(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    store = ChatStore(db_path)
    sid = store.create_session()
    first_window = make_context(store, sid)
    second_window = make_context(ChatStore(db_path), sid)
    monkeypatch.setenv("CHAT_TWO_STAGE", "0")
    monkeypatch.setattr(main, "_chat_loop", lambda *args, **kwargs: ("回答", ""))

    main.session_chat_reply(first_window, "窗口一的问题")
    with pytest.raises(RuntimeError, match="其他窗口"):
        main.session_chat_reply(second_window, "窗口二的问题")

    assert [m.content for m in second_window["chat_history"]] == ["窗口一的问题", "回答"]
    second_window["chat_history"].append(HumanMessage(content="模型交接说明"))
    main.session_chat_reply(second_window, "窗口二的问题")
    assert [m.content for m in store.load_messages(sid)] == [
        "窗口一的问题", "回答", "模型交接说明", "窗口二的问题", "回答",
    ]
    assert store.get_title(sid) == "窗口一的问题"


@pytest.mark.parametrize("entry", ["browser", "sidebar"])
def test_switch_renders_selected_session(tmp_path, entry):
    db_path = tmp_path / "sessions.db"
    store = ChatStore(db_path)
    current = store.create_session("旧会话")
    target = store.create_session("目标会话")
    store.replace_messages(current, [HumanMessage(content="旧历史")])
    store.replace_messages(target, [HumanMessage(content="目标历史")])
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE chat_sessions SET updated = '9998' WHERE session_id = ?", (target,))
    ctx = make_context(store, current)
    app = ChatApp("chat", ctx)

    async def run():
        async with app.run_test() as pilot:
            if entry == "browser":
                app._run_command_from_browser("/session 1")
            else:
                app.pop_screen()
                await pilot.pause()
                sidebar = app.query_one("#sessions", ListView)
                item = sidebar.children[0]
                # 另一窗口更新后，已显示的条目仍应按会话 ID 切换。
                with closing(sqlite3.connect(db_path)) as db, db:
                    db.execute("UPDATE chat_sessions SET updated = '9999' WHERE session_id = ?", (current,))
                app.on_session_clicked(ListView.Selected(sidebar, item, 0))
            await pilot.pause()
            assert ctx["session_id"] == target
            assert [str(widget.render()) for widget in app.query("#chat .user")] == ["目标历史"]

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["draft", "answer"])
def test_short_stream_delta_is_visible_before_turn_finishes(tmp_path, monkeypatch, stage):
    store = ChatStore(tmp_path / "sessions.db")
    app = ChatApp("chat", make_context(store, store.create_session()))
    emitted = Event()
    finish = Event()
    delta = "[星图]"

    def reply(ctx, question, *, on_draft, on_answer):
        (on_draft if stage == "draft" else on_answer)(delta)
        emitted.set()
        assert finish.wait(5)
        return "# 星图\n\n- 正式回答", {"draft": delta if stage == "draft" else None}

    monkeypatch.setattr(chatui, "session_chat_reply", reply)
    monkeypatch.setattr(app, "_fetch_artifacts", lambda _: "")

    async def run():
        async with app.run_test() as pilot:
            app.pop_screen()
            await pilot.pause()
            app._ask("查看星图")
            try:
                assert await asyncio.to_thread(emitted.wait, 3)
                await pilot.pause(0.25)
                selector = "#chat Collapsible Static.thinking" if stage == "draft" else "#chat .live"
                previews = list(app.query(selector))
                assert [str(widget.render()) for widget in previews] == [delta]
            finally:
                finish.set()
                await app.workers.wait_for_complete()
            await pilot.pause()
            assert not list(app.query("#chat .live"))
            assert len(app.query("#chat Markdown")) == 1
            assert len(app.query("#chat MarkdownH1")) == 1
            drafts = list(app.query("#chat Collapsible"))
            assert [widget.collapsed for widget in drafts] == ([True] if stage == "draft" else [])

    asyncio.run(run())


@pytest.mark.parametrize("follow_on_finish", [True, False])
def test_stream_follows_bottom_without_interrupting_history_reading(tmp_path, monkeypatch, follow_on_finish):
    store = ChatStore(tmp_path / "sessions.db")
    app = ChatApp("chat", make_context(store, store.create_session()))
    gates = [Event(), Event(), Event()]
    emitted = [Event(), Event(), Event()]
    chunks = [f"阶段 {n}\n" * 45 for n in range(3)]

    def reply(ctx, question, *, on_draft, on_answer):
        for index, chunk in enumerate(chunks):
            on_answer(chunk)
            emitted[index].set()
            assert gates[index].wait(5)
        return "\n\n".join(f"## 观测结果 {i}" for i in range(100)), {}

    monkeypatch.setattr(chatui, "session_chat_reply", reply)
    monkeypatch.setattr(app, "_fetch_artifacts", lambda _: "")

    async def run():
        async with app.run_test(size=(90, 24)) as pilot:
            app.pop_screen()
            await pilot.pause()
            chat = app.query_one("#chat", VerticalScroll)
            app._ask("分段输出")
            try:
                assert await asyncio.to_thread(emitted[0].wait, 3)
                await pilot.pause(0.25)
                assert chat.scroll_y == chat.max_scroll_y > 0
                chat.scroll_home(animate=False)
                await pilot.pause()
                assert chat.scroll_y == 0

                gates[0].set()
                assert await asyncio.to_thread(emitted[1].wait, 3)
                await pilot.pause(0.25)
                assert str(app.query_one("#chat .live", Static).render()) == "".join(chunks[:2])
                assert chat.scroll_y == 0

                chat.scroll_end(animate=False)
                await pilot.pause()
                gates[1].set()
                assert await asyncio.to_thread(emitted[2].wait, 3)
                await pilot.pause(0.25)
                assert str(app.query_one("#chat .live", Static).render()) == "".join(chunks)
                assert chat.scroll_y == chat.max_scroll_y
                if not follow_on_finish:
                    chat.scroll_home(animate=False)
                    await pilot.pause()
            finally:
                for gate in gates:
                    gate.set()
                await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert app.query_one("#chat Markdown", Markdown).display
            assert chat.scroll_y == (chat.max_scroll_y if follow_on_finish else 0)

    asyncio.run(run())
