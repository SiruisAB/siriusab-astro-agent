"""chat 多会话存储：sessions.db 中的 chat_sessions / chat_messages 两张表。

- chat_sessions：会话元数据（标题、创建/更新时间）
- chat_messages：消息（session_id + idx 保序，role=human/ai，content 为纯文本——
  多模态块只存文本部分，历史里本来就是轻量版）

与 SqliteSaver 共用 sessions.db（agent 模式的 checkpoint 也在里面，互不冲突）。
写路径用 replace_messages 保存快照。对话轮次校验已存历史的位置后，
只追加新消息，防止多个窗口互相覆盖。
连接允许跨线程（chatui 的 worker 线程写），WAL + timeout 降低与 SqliteSaver
并发时的锁冲突概率；所有连接用 closing() 显式关闭，不留文件锁。
"""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '新会话',
    created    TEXT NOT NULL,
    updated    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    session_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);
"""

PLACEHOLDER_TITLE = "新会话"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ChatHistoryChanged(RuntimeError):
    """另一窗口已保存新消息，当前回复基于旧历史。"""


class ChatStore:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        with closing(self._conn()) as db:
            db.executescript(_SCHEMA)
            # 老库迁移：补 params 列（会话级采样参数，JSON 字符串）
            cols = {r[1] for r in db.execute("PRAGMA table_info(chat_sessions)").fetchall()}
            if "params" not in cols:
                db.execute(
                    "ALTER TABLE chat_sessions ADD COLUMN params TEXT NOT NULL DEFAULT '{}'"
                )
                db.commit()

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    # ---------- 会话元数据 ----------

    def list_sessions(self) -> list[dict]:
        """全部会话，最近更新的在前。"""
        with closing(self._conn()) as db:
            rows = db.execute(
                "SELECT session_id, title, created, updated FROM chat_sessions"
                " ORDER BY updated DESC, created DESC"
            ).fetchall()
        return [{"id": r[0], "title": r[1], "created": r[2], "updated": r[3]}
                for r in rows]

    def create_session(self, title: str = PLACEHOLDER_TITLE) -> str:
        sid = "s" + uuid.uuid4().hex[:12]
        now = _now()
        with closing(self._conn()) as db, db:
            db.execute(
                "INSERT INTO chat_sessions (session_id, title, created, updated)"
                " VALUES (?, ?, ?, ?)",
                (sid, title.strip() or PLACEHOLDER_TITLE, now, now),
            )
        return sid

    def rename_session(self, sid: str, title: str) -> None:
        with closing(self._conn()) as db, db:
            db.execute(
                "UPDATE chat_sessions SET title = ?, updated = ? WHERE session_id = ?",
                (title.strip() or PLACEHOLDER_TITLE, _now(), sid),
            )

    def delete_session(self, sid: str) -> None:
        with closing(self._conn()) as db, db:
            db.execute("DELETE FROM chat_messages WHERE session_id = ?", (sid,))
            db.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid,))

    def get_title(self, sid: str) -> str:
        with closing(self._conn()) as db:
            row = db.execute(
                "SELECT title FROM chat_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        return row[0] if row else ""

    def touch(self, sid: str) -> None:
        with closing(self._conn()) as db, db:
            db.execute(
                "UPDATE chat_sessions SET updated = ? WHERE session_id = ?",
                (_now(), sid),
            )

    # ---------- 会话级参数 ----------

    def get_params(self, sid: str) -> dict:
        with closing(self._conn()) as db:
            row = db.execute(
                "SELECT params FROM chat_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def set_params(self, sid: str, params: dict) -> None:
        with closing(self._conn()) as db, db:
            db.execute(
                "UPDATE chat_sessions SET params = ?, updated = ? WHERE session_id = ?",
                (json.dumps(params, ensure_ascii=False), _now(), sid),
            )

    # ---------- 消息 ----------

    def load_messages(self, sid: str) -> list:
        """恢复某会话的消息列表（按写入顺序）。"""
        with closing(self._conn()) as db:
            rows = db.execute(
                "SELECT role, content FROM chat_messages"
                " WHERE session_id = ? ORDER BY idx",
                (sid,),
            ).fetchall()
        out = []
        for role, content in rows:
            if role == "human":
                out.append(HumanMessage(content=content))
            else:
                out.append(AIMessage(content=content))
        return out

    def replace_messages(
        self, sid: str, messages: list, *, first_question: str = "",
        expected_count: int | None = None,
    ) -> None:
        """保存消息和元数据。指定 expected_count 时校验位置，仅追加新消息。"""
        lines = first_question.strip().splitlines()
        title = lines[0].strip()[:24] if lines else ""
        with closing(self._conn()) as db, db:
            updated = db.execute(
                """UPDATE chat_sessions SET updated = ?, title = CASE
                       WHEN title = ? AND ? <> '' THEN ? ELSE title END
                   WHERE session_id = ?""",
                (_now(), PLACEHOLDER_TITLE, title, title, sid),
            )
            if updated.rowcount == 0:
                raise LookupError(f"会话不存在：{sid}")
            start = 0
            if expected_count is not None:
                # 上面的 UPDATE 已持有写锁，校验与追加之间没有并发写入。
                (next_index,) = db.execute(
                    "SELECT COALESCE(MAX(idx) + 1, 0) FROM chat_messages WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if next_index != expected_count:
                    raise ChatHistoryChanged("会话已在其他窗口更新，请重新发送本次提问。")
                if expected_count > len(messages):
                    raise ValueError("追加保存时不能缩短历史")
                start = expected_count
            db.executemany(
                """INSERT INTO chat_messages (session_id, idx, role, content)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (session_id, idx) DO UPDATE
                   SET role = excluded.role, content = excluded.content
                   WHERE chat_messages.role <> excluded.role
                      OR chat_messages.content <> excluded.content""",
                ((sid, i, "human" if isinstance(m, HumanMessage) else "ai", str(m.content))
                 for i, m in enumerate(messages[start:], start)),
            )
            if expected_count is None:
                db.execute(
                    "DELETE FROM chat_messages WHERE session_id = ? AND idx >= ?",
                    (sid, len(messages)),
                )
