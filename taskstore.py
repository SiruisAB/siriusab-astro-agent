"""Durable task metadata, checkpoints and a workspace-wide execution lock."""

import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

def _now():
    return datetime.now().isoformat(timespec="seconds")


class TaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / ".workbench" / "tasks.db"
        self.path.parent.mkdir(exist_ok=True)
        with closing(self._conn()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_runs (
                mode TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, question TEXT NOT NULL, profile TEXT NOT NULL,
                model_json TEXT NOT NULL, expected_count INTEGER NOT NULL,
                status TEXT NOT NULL, pending_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
                PRIMARY KEY(mode, session_id))""")

    def _conn(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def get(self, mode, sid):
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM task_runs WHERE mode=? AND session_id=?", (mode, sid)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["pending"] = json.loads(result.pop("pending_json"))
        result["model"] = json.loads(result.pop("model_json"))
        return result

    def active(self, mode):
        with closing(self._conn()) as db:
            rows = db.execute("SELECT session_id FROM task_runs WHERE mode=? AND status IN ('running','waiting')", (mode,)).fetchall()
        return {sid for (sid,) in rows}

    def start(self, mode, sid, question, profile, model, expected_count):
        run_id = uuid.uuid4().hex
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM task_runs WHERE mode=? AND session_id=?", (mode, sid)).fetchone()
            if row and row[0] in {"running", "waiting"}:
                raise ValueError("当前会话有未完成任务，请先继续审批或放弃本轮。")
            db.execute("""INSERT INTO task_runs
                (mode,session_id,run_id,thread_id,question,profile,model_json,expected_count,status,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mode,session_id) DO UPDATE SET
                run_id=excluded.run_id,thread_id=excluded.thread_id,question=excluded.question,
                profile=excluded.profile,model_json=excluded.model_json,expected_count=excluded.expected_count,
                status=excluded.status,pending_json='[]',error='',updated=excluded.updated""",
                (mode, sid, run_id, f"workbench-{mode}-{sid}-{run_id}", question, profile,
                 json.dumps(model), expected_count, "running", _now()))
        return self.get(mode, sid)

    def update(self, mode, sid, run_id, status, *, pending=None, error=""):
        with closing(self._conn()) as db, db:
            result = db.execute("UPDATE task_runs SET status=?,pending_json=?,error=?,updated=? "
                                "WHERE mode=? AND session_id=? AND run_id=?",
                                (status, json.dumps(pending or [], ensure_ascii=False), error, _now(), mode, sid, run_id))
            if not result.rowcount:
                raise ValueError("任务状态已更新，请重新打开会话。")

    @contextmanager
    def checkpoint(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        with closing(self._conn()) as connection:
            yield SqliteSaver(connection)

    @contextmanager
    def execution_lock(self):
        # OS locks release on process termination, including while awaiting approval.
        with (self.path.parent / "execution.lock").open("a+b") as lock:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("工作区正在执行或等待审批另一轮任务，请先关闭原连接或完成审批。") from exc
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
