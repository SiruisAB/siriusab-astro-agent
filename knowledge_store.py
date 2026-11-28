"""User-curated, source-grounded research entries. Independent Python implementation."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from memory_retrieval import matches_keyword, resolve_source


class KnowledgeChanged(RuntimeError):
    pass


class KnowledgeStore:
    def __init__(self, root, stores):
        self.path = root / ".workbench" / "knowledge.db"
        self.path.parent.mkdir(exist_ok=True)
        self.stores = stores
        with closing(self._conn()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_entries (
                    entry_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    entry_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
                    keywords_json TEXT NOT NULL, quote TEXT NOT NULL, note TEXT NOT NULL,
                    source_mode TEXT NOT NULL, source_session TEXT NOT NULL, source_reference TEXT NOT NULL,
                    source_checksum TEXT NOT NULL, scope TEXT NOT NULL, priority INTEGER NOT NULL, created TEXT NOT NULL,
                    PRIMARY KEY(entry_id,revision)
                );
            """)

    def _conn(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def _source(self, entry):
        mode = entry["source_mode"]
        if mode not in self.stores:
            raise ValueError("未知的知识来源模式")
        return resolve_source(self.stores[mode], entry["source_session"], entry["source_reference"])

    def _validate(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("知识条目格式无效")
        entry = {name: payload.get(name) for name in ("title", "quote", "note", "source_mode", "source_session", "source_reference", "scope", "priority")}
        for name, maximum in (("title", 120), ("quote", 6000), ("note", 2000)):
            value = entry[name] or ""
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{name} 的长度或类型无效")
            entry[name] = value.strip()
        if not entry["title"] or not entry["quote"]:
            raise ValueError("请填写知识标题和原文摘录")
        keywords = payload.get("keywords")
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20 or any(not isinstance(key, str) or not key.strip() or len(key) > 80 for key in keywords):
            raise ValueError("触发词须为 1–20 个短关键词")
        entry["keywords"] = list(dict.fromkeys(key.strip() for key in keywords))
        if entry["scope"] not in {"session", "project"}:
            raise ValueError("知识范围须为当前会话或项目共享")
        if type(entry["priority"]) is not int or not 0 <= entry["priority"] <= 100:
            raise ValueError("优先级须为 0–100")
        if not isinstance(entry["source_session"], str) or not isinstance(entry["source_mode"], str):
            raise ValueError("知识来源会话无效")
        source = self._source(entry)
        if not source["current"]:
            raise ValueError("来源消息已修订或移除，请选择当前原文")
        if entry["quote"] not in source["content"]:
            raise ValueError("摘录必须逐字存在于来源原文；补充解释请写入备注")
        entry["source_checksum"] = source["checksum"]
        return entry

    def save(self, payload):
        entry = self._validate(payload)
        eid = payload.get("entry_id")
        expected = payload.get("revision")
        if eid is not None and not isinstance(eid, str):
            raise ValueError("知识条目标识无效")
        if eid and type(expected) is not int:
            raise ValueError("缺少知识版本校验信息")
        enabled = payload.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("知识启用状态无效")
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if eid:
                head = db.execute("SELECT revision FROM knowledge_entries WHERE entry_id=?", (eid,)).fetchone()
                if head is None:
                    raise LookupError("知识条目不存在")
                if head[0] != expected:
                    raise KnowledgeChanged("知识条目已更新，请刷新后重试")
                revision = expected + 1
            else:
                eid, revision = "k" + uuid.uuid4().hex, 1
            db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (eid, revision, entry["title"], json.dumps(entry["keywords"], ensure_ascii=False), entry["quote"], entry["note"],
                        entry["source_mode"], entry["source_session"], entry["source_reference"], entry["source_checksum"], entry["scope"],
                        entry["priority"], datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO knowledge_entries VALUES(?,?,?) ON CONFLICT(entry_id) "
                       "DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled", (eid, revision, int(enabled)))
        return eid

    def toggle(self, eid, revision, enabled):
        if not isinstance(eid, str) or type(revision) is not int or type(enabled) is not bool:
            raise ValueError("知识条目状态请求无效")
        with closing(self._conn()) as db, db:
            result = db.execute("UPDATE knowledge_entries SET enabled=?,revision=revision+1 WHERE entry_id=? AND revision=?", (int(enabled), eid, revision))
            if not result.rowcount:
                raise KnowledgeChanged("知识条目已更新，请刷新后重试")
            # Enable/disable is a new immutable metadata revision, not an archive overwrite.
            db.execute("INSERT INTO knowledge_versions SELECT entry_id,revision+1,title,keywords_json,quote,note,source_mode,source_session,source_reference,"
                       "source_checksum,scope,priority,? FROM knowledge_versions WHERE entry_id=? AND revision=?",
                       (datetime.now(timezone.utc).isoformat(), eid, revision))

    def list(self, mode, sid):
        with closing(self._conn()) as db:
            rows = db.execute("SELECT v.*,e.enabled FROM knowledge_entries e JOIN knowledge_versions v ON v.entry_id=e.entry_id AND v.revision=e.revision "
                              "WHERE v.scope='project' OR (v.source_mode=? AND v.source_session=?) ORDER BY v.priority DESC,v.created DESC", (mode, sid)).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            entry["keywords"] = json.loads(entry.pop("keywords_json"))
            entry["enabled"] = bool(entry["enabled"])
            try:
                source = self._source(entry)
                entry["valid"] = source["current"] and source["checksum"] == entry["source_checksum"] and entry["quote"] in source["content"]
                entry["source_role"] = source["role"]
                entry["source_status"] = "原文一致；内容未独立核验" if entry["valid"] else "来源已修订或与原文不一致"
            except (ValueError, LookupError) as exc:
                entry["valid"] = False
                entry["source_status"] = str(exc)
            entries.append(entry)
        return entries

    def activated(self, mode, sid, query):
        entries = []
        for entry in self.list(mode, sid):
            matched = [key for key in entry["keywords"] if matches_keyword(query, key)]
            if entry["enabled"] and entry["valid"] and matched:
                entries.append({**entry, "matched_keywords": matched})
        return entries[:6]
