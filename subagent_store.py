"""Scoped job records and replay-safe completed research delegations. Engine-free startup."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


MAX_JOBS = 3


class SubagentStore:
    def __init__(self, root):
        self.path = Path(root) / ".workbench" / "subagents.db"
        self.path.parent.mkdir(exist_ok=True)
        with closing(self._conn()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS subagent_jobs (
                job_id TEXT PRIMARY KEY, mode TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                call_key TEXT NOT NULL, kind TEXT NOT NULL, request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                status TEXT NOT NULL, report_json TEXT, created TEXT NOT NULL, completed TEXT,
                UNIQUE(mode,session_id,run_id,call_key))""")

    def _conn(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def reserve(self, mode, sid, run_id, call_key, kind, request):
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256((kind + raw).encode()).hexdigest()
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM subagent_jobs WHERE mode=? AND session_id=? AND run_id=? AND call_key=?",
                             (mode, sid, run_id, call_key)).fetchone()
            if row:
                if row["request_hash"] != checksum:
                    raise ValueError("委派调用标识已使用，不能更换参数后重放")
                return dict(row), False
            count = db.execute("SELECT COUNT(*) FROM subagent_jobs WHERE mode=? AND session_id=? AND run_id=?", (mode, sid, run_id)).fetchone()[0]
            if count >= MAX_JOBS:
                raise ValueError("本轮已达到 3 个子 Agent 任务的上限")
            jid, created = "j" + uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO subagent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (jid, mode, sid, run_id, call_key, kind, checksum, raw, "running", None, created, None))
        return self.get(mode, sid, jid), True

    def finish(self, jid, report):
        with closing(self._conn()) as db, db:
            result = db.execute("UPDATE subagent_jobs SET status=?,report_json=?,completed=? WHERE job_id=? AND status='running'",
                                (report["status"], json.dumps(report, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), jid))
            if not result.rowcount:
                raise RuntimeError("子任务状态已改变")

    def lookup_call(self, mode, sid, run_id, call_key):
        with closing(self._conn()) as db:
            row = db.execute("SELECT job_id FROM subagent_jobs WHERE mode=? AND session_id=? AND run_id=? AND call_key=?",
                             (mode, sid, run_id, call_key)).fetchone()
        return self.get(mode, sid, row["job_id"]) if row else None

    def get(self, mode, sid, jid):
        with closing(self._conn()) as db:
            row = db.execute("SELECT * FROM subagent_jobs WHERE mode=? AND session_id=? AND job_id=?", (mode, sid, jid)).fetchone()
        if row is None:
            raise LookupError("子 Agent 任务不存在或不属于当前会话")
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        report = result.pop("report_json")
        result["report"] = json.loads(report) if report else None
        return result

    def list(self, mode, sid):
        with closing(self._conn()) as db:
            rows = db.execute("SELECT job_id,kind,status,created,completed,run_id FROM subagent_jobs WHERE mode=? AND session_id=? ORDER BY created DESC LIMIT 100",
                              (mode, sid)).fetchall()
        return [dict(row) for row in rows]
