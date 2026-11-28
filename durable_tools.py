"""Preserve completed writes when another tool in the same batch interrupts."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphBubbleUp


class DurableToolResults(AgentMiddleware):
    MUTATING_TOOLS = {"run_powershell", "run_bash", "write_file", "edit_file"}

    def __init__(self, root):
        self.path = Path(root) / ".workbench" / "tool_results.db"
        self.path.parent.mkdir(exist_ok=True)
        self.serde = JsonPlusSerializer()
        with closing(self._conn()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS tool_results (
                thread_id TEXT NOT NULL, call_key TEXT NOT NULL, args_hash TEXT NOT NULL,
                status TEXT NOT NULL, value_type TEXT, value BLOB,
                PRIMARY KEY(thread_id,call_key))""")

    def _conn(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] not in self.MUTATING_TOOLS:
            return handler(request)
        thread = str(request.runtime.config.get("configurable", {}).get("thread_id", ""))
        parent = next((message.id for message in reversed(request.state.get("messages", []))
                       if isinstance(message, AIMessage) and any(item["id"] == call["id"] for item in message.tool_calls)), None)
        key = f"{parent or len(request.state.get('messages', []))}:{call['id']}"
        fingerprint = hashlib.sha256(json.dumps([call["name"], call["args"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT args_hash,status,value_type,value FROM tool_results WHERE thread_id=? AND call_key=?", (thread, key)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise RuntimeError("该工具调用已执行过，不能以同一调用标识修改参数后再次执行。请创建新的操作。")
                if row[1] == "complete":
                    return self.serde.loads_typed((row[2], row[3]))
                raise RuntimeError("此操作已开始执行但未保存完整结果，已阻止自动重跑。请先核实实际执行结果。")
            db.execute("INSERT INTO tool_results (thread_id,call_key,args_hash,status) VALUES (?,?,?,'running')", (thread, key, fingerprint))
        try:
            result = handler(request)
        except GraphBubbleUp:
            # Interrupting tools must put external changes after interrupt(), as documented.
            with closing(self._conn()) as db, db:
                db.execute("DELETE FROM tool_results WHERE thread_id=? AND call_key=?", (thread, key))
            raise
        # Ordinary failures deliberately leave a 'running' record: a partial write
        # cannot be made safe merely by retrying it. Read-only/query tools are unaffected.
        value_type, value = self.serde.dumps_typed(result)
        with closing(self._conn()) as db, db:
            db.execute("UPDATE tool_results SET status='complete',value_type=?,value=? WHERE thread_id=? AND call_key=?",
                       (value_type, value, thread, key))
        return result
