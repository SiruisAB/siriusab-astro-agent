"""Rebuildable local FTS5 search. Every result is resolved back to its original archive."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_search_chunks (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, reference TEXT NOT NULL,
 checksum TEXT NOT NULL, role TEXT NOT NULL, historical INTEGER NOT NULL,
 start INTEGER NOT NULL, content TEXT NOT NULL, tokens TEXT NOT NULL,
 UNIQUE(session_id,reference,start)
);
CREATE INDEX IF NOT EXISTS memory_search_scope ON memory_search_chunks(session_id);
CREATE TABLE IF NOT EXISTS memory_search_sources (
 session_id TEXT NOT NULL, reference TEXT NOT NULL, checksum TEXT NOT NULL,
 PRIMARY KEY(session_id,reference)
);
CREATE TABLE IF NOT EXISTS memory_search_blocks (
 session_id TEXT NOT NULL, block_id TEXT NOT NULL, checksum TEXT NOT NULL,
 PRIMARY KEY(session_id,block_id)
);
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_search_fts USING fts5(
 content, tokens, content='memory_search_chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_search_insert AFTER INSERT ON memory_search_chunks BEGIN
 INSERT INTO memory_search_fts(rowid,content,tokens) VALUES(new.id,new.content,new.tokens);
END;
CREATE TRIGGER IF NOT EXISTS memory_search_delete AFTER DELETE ON memory_search_chunks BEGIN
 INSERT INTO memory_search_fts(memory_search_fts,rowid,content,tokens) VALUES('delete',old.id,old.content,old.tokens);
END;
"""
IGNORED = {"之前", "现在", "一下", "什么", "如何", "请问", "帮我", "查询", "查看", "能否", "继续", "进行", "the", "and", "what", "this", "with"}
DERIVED_TOOLS = {"read_memory_source", "read_evidence", "decompress", "search_context", "compress", "delegate_literature", "verify_evidence",
                 "list_skills", "load_skill", "read_skill_resource", "delegate_tests", "read_test_source", "ask_web_advisor"}


def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block if isinstance(block, str) else block.get("text", "") for block in content
                     if isinstance(block, str) or isinstance(block, dict) and block.get("type") in {"text", "output_text"} and isinstance(block.get("text"), str))


def terms(text, *, query=False):
    parts = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.casefold())
    out = []
    for part in parts:
        if part in IGNORED:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            out.extend(part[i:i + 2] for i in range(len(part) - 1) if part[i:i + 2] not in IGNORED)
        else:
            out.append(part)
    return list(dict.fromkeys(out))[:(24 if query else 3000)]


def matches_keyword(query, keyword):
    """ASCII identifiers match whole identifiers (M31 != M310); Chinese aliases are literal."""
    query, keyword = query.casefold(), keyword.strip().casefold()
    if not keyword:
        return False
    if re.fullmatch(r"[a-z0-9_]+", keyword):
        return re.search(r"(?<![a-z0-9_])" + re.escape(keyword) + r"(?![a-z0-9_])", query) is not None
    return keyword in query


def resolve_source(store, sid, reference):
    if not isinstance(reference, str) or len(reference) > 100:
        raise ValueError("无效的原文来源标识")
    if re.fullmatch(r"m[a-f0-9]+@[1-9][0-9]*", reference):
        mid, revision = reference.split("@")
        source = store.original_message(sid, mid, int(revision))
        if any(source["metadata"].get(key) for key in ("retrieval_context", "context_summary")):
            raise ValueError("派生上下文不能作为科研知识来源")
        with closing(store._conn()) as db:
            current = db.execute("SELECT 1 FROM memory_messages WHERE session_id=? AND message_id=? AND revision=?",
                                 (sid, mid, int(revision))).fetchone() is not None
        return {"reference": reference, "content": source["content"], "role": source["role"],
                "checksum": source["checksum"], "current": current, "idx": source["idx"]}
    if re.fullmatch(r"b[a-f0-9]+#[1-9][0-9]*", reference):
        bid, position = reference.split("#")
        messages = store.memory_block(sid, bid)
        index = int(position) - 1
        if index >= len(messages):
            raise LookupError("原文块消息不存在")
        message = messages[index]
        data = message["data"]
        calls = {call["id"]: call["name"] for item in messages for call in item["data"].get("tool_calls", [])}
        if data.get("name") in DERIVED_TOOLS or calls.get(data.get("tool_call_id")) in DERIVED_TOOLS:
            raise ValueError("记忆工具的派生输出不能作为新的科研知识来源")
        if message["type"] in {"system", "remove"} or any(data.get("additional_kwargs", {}).get(key) for key in ("retrieval_context", "context_summary")):
            raise ValueError("系统提示词和派生上下文不能作为科研知识来源")
        content = text_content(data.get("content"))
        checksum = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return {"reference": reference, "content": content, "role": message["type"], "checksum": checksum,
                "current": True, "idx": index}
    raise ValueError("来源须为 m消息ID@修订号 或 b原文块ID#消息序号")


class MemorySearch:
    def __init__(self, store):
        self.store = store

    def _schema(self, db):
        db.executescript(SCHEMA)
        had_fts = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_search_fts'").fetchone()
        had_triggers = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ('memory_search_insert','memory_search_delete')").fetchone()[0] == 2
        try:
            db.executescript(FTS_SCHEMA)
            db.execute("SELECT rowid FROM memory_search_fts LIMIT 0")
            if not had_fts or not had_triggers:
                db.execute("INSERT INTO memory_search_fts(memory_search_fts) VALUES('rebuild')")
                db.commit()
            return "fts5"
        except sqlite3.OperationalError as exc:
            if "no such module" not in str(exc).lower():
                raise
            db.execute("DROP TRIGGER IF EXISTS memory_search_insert")
            db.execute("DROP TRIGGER IF EXISTS memory_search_delete")
            db.commit()
            return "literal"

    def _insert_source(self, db, sid, source, historical=0):
        content = source["content"]
        if source["reference"].startswith("b") and db.execute("SELECT 1 FROM memory_search_sources WHERE session_id=? AND checksum=? LIMIT 1",
                                                              (sid, source["checksum"])).fetchone():
            db.execute("INSERT INTO memory_search_sources VALUES(?,?,?)", (sid, source["reference"], source["checksum"]))
            return  # Repeated checkpoints of the same tool message are not extra retrieval candidates.
        for start in range(0, len(content), 1300):
            chunk = content[start:start + 1500]
            db.execute("INSERT INTO memory_search_chunks(session_id,reference,checksum,role,historical,start,content,tokens) "
                       "VALUES(?,?,?,?,?,?,?,?)", (sid, source["reference"], source["checksum"], source["role"], historical,
                                                 start, chunk, " ".join(terms(chunk))))
        db.execute("INSERT INTO memory_search_sources VALUES(?,?,?)", (sid, source["reference"], source["checksum"]))

    def _sync(self, db, sid):
        originals = db.execute("SELECT o.message_id,o.revision,o.idx,o.role,o.content,o.checksum,o.metadata FROM memory_originals o "
                               "LEFT JOIN memory_search_sources s ON s.session_id=o.session_id AND s.reference=o.message_id||'@'||o.revision "
                               "WHERE o.session_id=? AND s.reference IS NULL", (sid,)).fetchall()
        for mid, revision, idx, role, content, checksum, metadata in originals:
            reference = f"{mid}@{revision}"
            if hashlib.sha256(json.dumps([role, content, json.loads(metadata)], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest() != checksum:
                raise ValueError("原文档案完整性校验失败")
            if any(json.loads(metadata).get(key) for key in ("context_summary", "retrieval_context")):
                db.execute("INSERT INTO memory_search_sources VALUES(?,?,?)", (sid, reference, checksum))
                continue
            self._insert_source(db, sid, {"reference": reference, "role": role, "content": content, "checksum": checksum})
        blocks = db.execute("SELECT b.block_id,b.original_json,b.checksum,b.kind FROM memory_blocks b LEFT JOIN memory_search_blocks s "
                            "ON s.session_id=b.session_id AND s.block_id=b.block_id WHERE b.session_id=? AND s.block_id IS NULL", (sid,)).fetchall()
        for bid, raw, checksum, kind in blocks:
            if hashlib.sha256(raw.encode()).hexdigest() != checksum:
                raise ValueError("原文块完整性校验失败")
            messages = json.loads(raw)
            calls = {call["id"]: call["name"] for item in messages for call in item["data"].get("tool_calls", [])}
            for index, message in enumerate(messages, 1):
                data = message["data"]
                if data.get("name") in DERIVED_TOOLS or calls.get(data.get("tool_call_id")) in DERIVED_TOOLS:
                    continue
                if kind != "legacy" and message["type"] != "tool":
                    continue  # Original transcript is indexed once; repeated model snapshots are not another memory source.
                if message["type"] in {"system", "remove"} or any(data.get("additional_kwargs", {}).get(key) for key in ("context_summary", "retrieval_context")):
                    continue
                content = text_content(data.get("content"))
                if not content:
                    continue
                digest = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                self._insert_source(db, sid, {"reference": f"{bid}#{index}", "role": message["type"], "content": content, "checksum": digest},
                                    historical=0 if message["type"] == "tool" else 1)
            db.execute("INSERT INTO memory_search_blocks VALUES(?,?,?)", (sid, bid, checksum))

    def rebuild(self, sid):
        with closing(self.store._conn()) as db, db:
            engine = self._schema(db)
            db.execute("BEGIN IMMEDIATE")
            self.store._require_session(db, sid)
            if engine == "fts5":
                db.execute("INSERT INTO memory_search_fts(memory_search_fts) VALUES('rebuild')")
            for table in ("memory_search_chunks", "memory_search_sources", "memory_search_blocks"):
                db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            self._sync(db, sid)
            count = db.execute("SELECT COUNT(*) FROM memory_search_chunks WHERE session_id=?", (sid,)).fetchone()[0]
            return {"engine": engine, "chunks": count}

    def search(self, sid, query, *, limit=8, historical=False, exclude=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("检索内容须为 1–2000 字符")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("检索数量须为 1–20")
        query = query.strip()
        query_terms = terms(query, query=True)
        with closing(self.store._conn()) as db, db:
            engine = self._schema(db)
            db.execute("BEGIN IMMEDIATE")
            self.store._require_session(db, sid)
            self._sync(db, sid)
            scope_clause = "" if historical else (
                " AND c.historical=0 AND (c.reference LIKE 'b%' OR EXISTS (SELECT 1 FROM memory_messages m "
                "WHERE m.session_id=c.session_id AND c.reference=m.message_id||'@'||m.revision))"
            )
            excluded_candidates = sorted(set(exclude or ()))[:512]
            if excluded_candidates:
                scope_clause += " AND c.reference NOT IN (" + ",".join("?" for _ in excluded_candidates) + ")"
            if engine == "fts5" and query_terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in query_terms)
                rows = db.execute("SELECT c.reference,c.checksum,c.role,c.historical,c.start,c.content,bm25(memory_search_fts) "
                                  "FROM memory_search_fts JOIN memory_search_chunks c ON c.id=memory_search_fts.rowid "
                                  "WHERE memory_search_fts MATCH ? AND c.session_id=?" + scope_clause + " ORDER BY bm25(memory_search_fts),c.id DESC LIMIT 160",
                                  (expression, sid, *excluded_candidates)).fetchall()
            else:
                rows = db.execute("SELECT c.reference,c.checksum,c.role,c.historical,c.start,c.content,0 FROM memory_search_chunks c "
                                  "WHERE c.session_id=? AND instr(lower(c.content),lower(?))>0" + scope_clause + " ORDER BY c.id DESC LIMIT 160",
                                  (sid, query, *excluded_candidates)).fetchall()
        results, warnings, seen, excluded = [], [], set(), set(exclude or ())
        for reference, checksum, role, old_block, start, content, rank in rows:
            if reference in seen or reference in excluded or old_block and not historical:
                continue
            try:
                source = resolve_source(self.store, sid, reference)
                if source["checksum"] != checksum or source["content"][start:start + len(content)] != content:
                    raise ValueError("检索索引与原文不一致，请重建索引")
            except (ValueError, LookupError) as exc:
                warnings.append(str(exc))
                continue
            if not historical and not source["current"]:
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            if digest in seen:
                continue
            seen.update((reference, digest))
            matched = [term for term in query_terms if term in content.casefold()]
            position = min((content.casefold().find(term) for term in matched), default=0)
            left = max(0, position - 120)
            snippet = content[left:left + 900]
            results.append({"reference": reference, "checksum": checksum, "role": role, "current": source["current"],
                            "historical": bool(old_block or not source["current"]), "snippet": snippet, "offset": start + left,
                            "score": len(matched) + (5 if query.casefold() in content.casefold() else 0), "rank": rank})
        results.sort(key=lambda hit: (-hit["score"], hit["rank"]))
        return {"engine": engine, "hits": results[:limit], "warnings": list(dict.fromkeys(warnings))}
