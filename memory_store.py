"""Python-owned, append-only originals and versioned context. No engine imports at startup."""

import difflib
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_messages (
    session_id TEXT NOT NULL, idx INTEGER NOT NULL, message_id TEXT NOT NULL,
    revision INTEGER NOT NULL, PRIMARY KEY(session_id, idx)
);
CREATE TABLE IF NOT EXISTS memory_originals (
    message_id TEXT NOT NULL, revision INTEGER NOT NULL, session_id TEXT NOT NULL,
    idx INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    metadata TEXT NOT NULL, checksum TEXT NOT NULL, created TEXT NOT NULL,
    PRIMARY KEY(message_id, revision)
);
CREATE INDEX IF NOT EXISTS memory_originals_session ON memory_originals(session_id, idx);
CREATE TABLE IF NOT EXISTS memory_versions (
    version_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_id TEXT,
    summary TEXT NOT NULL, prefix_count INTEGER NOT NULL, sources_json TEXT NOT NULL,
    source_hash TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_versions_session ON memory_versions(session_id, created);
CREATE TABLE IF NOT EXISTS memory_heads (
    session_id TEXT PRIMARY KEY, version_id TEXT, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_blocks (
    block_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
    thread_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
    original_json TEXT NOT NULL, checksum TEXT NOT NULL, created TEXT NOT NULL,
    UNIQUE(session_id, run_id, kind, checksum)
);
CREATE INDEX IF NOT EXISTS memory_blocks_session ON memory_blocks(session_id, created);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class MemoryStoreMixin:
    def _memory_init(self, db):
        db.executescript(SCHEMA)
        # A head also marks a migrated session. No scan/rewrite on subsequent startup.
        with db:
            db.execute("BEGIN IMMEDIATE")
            for (sid,) in db.execute("SELECT session_id FROM chat_sessions WHERE session_id NOT IN "
                                     "(SELECT session_id FROM memory_heads)").fetchall():
                self._sync_originals(db, sid)
                old = db.execute("SELECT summary, prefix_count FROM chat_context WHERE session_id=?", (sid,)).fetchone()
                if old and 0 < old[1] <= len(self._sources(db, sid)):
                    self._publish(db, sid, old[0], old[1], model="legacy", kind="migration")
                else:
                    db.execute("INSERT OR IGNORE INTO memory_heads(session_id) VALUES (?)", (sid,))
                    db.execute("DELETE FROM chat_context WHERE session_id=?", (sid,))

    def _require_session(self, db, sid):
        if not db.execute("SELECT 1 FROM chat_sessions WHERE session_id=?", (sid,)).fetchone():
            raise LookupError("会话不存在")

    def _head(self, db, sid):
        row = db.execute("SELECT version_id, revision FROM memory_heads WHERE session_id=?", (sid,)).fetchone()
        return {"active_version": row[0] if row else None, "revision": row[1] if row else 0}

    def _sources(self, db, sid):
        return [{"idx": i, "message_id": mid, "revision": rev, "checksum": checksum}
                for i, mid, rev, checksum in db.execute(
                    "SELECT m.idx, m.message_id, m.revision, o.checksum FROM memory_messages m "
                    "JOIN memory_originals o ON o.message_id=m.message_id AND o.revision=m.revision "
                    "WHERE m.session_id=? ORDER BY m.idx", (sid,))]

    def _sync_originals(self, db, sid):
        rows = db.execute("SELECT idx, role, content, metadata FROM chat_messages WHERE session_id=? ORDER BY idx", (sid,)).fetchall()
        previous = {row[0]: row[1:] for row in db.execute(
            "SELECT m.idx, m.message_id, m.revision, o.checksum FROM memory_messages m "
            "JOIN memory_originals o ON o.message_id=m.message_id AND o.revision=m.revision "
            "WHERE m.session_id=?", (sid,))}
        for idx, role, content, metadata in rows:
            checksum = _hash(_json([role, content, json.loads(metadata)]))
            old = previous.get(idx)
            if old and old[2] == checksum:
                continue
            mid, rev = (old[0], old[1] + 1) if old else ("m" + uuid.uuid4().hex, 1)
            db.execute("INSERT INTO memory_originals VALUES (?,?,?,?,?,?,?,?,?)",
                       (mid, rev, sid, idx, role, content, metadata, checksum, _now()))
            db.execute("INSERT INTO memory_messages VALUES (?,?,?,?) ON CONFLICT(session_id,idx) "
                       "DO UPDATE SET message_id=excluded.message_id, revision=excluded.revision", (sid, idx, mid, rev))
        db.execute("DELETE FROM memory_messages WHERE session_id=? AND idx>=?", (sid, len(rows)))
        head = self._head(db, sid)
        if head["active_version"]:
            version = self._version(db, sid, head["active_version"])
            if not self._compatible(db, sid, version):
                self._activate(db, sid, None)
        db.execute("INSERT OR IGNORE INTO memory_heads(session_id) VALUES (?)", (sid,))

    def _version(self, db, sid, vid):
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM memory_versions WHERE session_id=? AND version_id=?", (sid, vid)).fetchone()
        db.row_factory = None
        if row is None:
            raise LookupError("摘要版本不存在或不属于当前会话")
        return dict(row)

    def _verified_sources(self, db, sid, version):
        sources = json.loads(version["sources_json"])
        if not isinstance(sources, list) or any(not isinstance(source, dict) or
                set(source) != {"idx", "message_id", "revision", "checksum"} for source in sources):
            raise ValueError("摘要来源完整性校验失败")
        if _hash(_json(sources)) != version["source_hash"] or len(sources) != version["prefix_count"]:
            raise ValueError("摘要来源完整性校验失败")
        out = []
        for source in sources:
            row = db.execute("SELECT idx,role,content,metadata,checksum FROM memory_originals "
                             "WHERE session_id=? AND message_id=? AND revision=?",
                             (sid, source["message_id"], source["revision"])).fetchone()
            if (row is None or row[0] != source["idx"] or row[4] != source["checksum"] or
                    _hash(_json([row[1], row[2], json.loads(row[3])])) != row[4]):
                raise ValueError("原文档案完整性校验失败")
            out.append({**source, "role": row[1], "content": row[2], "metadata": json.loads(row[3])})
        return out

    def _compatible(self, db, sid, version):
        self._verified_sources(db, sid, version)
        return self._sources(db, sid)[:version["prefix_count"]] == json.loads(version["sources_json"])

    def _activate(self, db, sid, version):
        db.execute("INSERT INTO memory_heads VALUES (?,?,1) ON CONFLICT(session_id) "
                   "DO UPDATE SET version_id=excluded.version_id, revision=memory_heads.revision+1",
                   (sid, version["version_id"] if version else None))
        if version:
            db.execute("INSERT INTO chat_context VALUES (?,?,?) ON CONFLICT(session_id) "
                       "DO UPDATE SET summary=excluded.summary,prefix_count=excluded.prefix_count",
                       (sid, version["summary"], version["prefix_count"]))
        else:
            db.execute("DELETE FROM chat_context WHERE session_id=?", (sid,))

    def _publish(self, db, sid, summary, prefix_count, *, model="", kind="compact", parent=None):
        sources = self._sources(db, sid)[:prefix_count]
        if len(sources) != prefix_count:
            raise ValueError("摘要覆盖范围无效")
        self._verified_sources(db, sid, {"sources_json": _json(sources),
                                        "source_hash": _hash(_json(sources)), "prefix_count": len(sources)})
        version = {"version_id": "v" + uuid.uuid4().hex, "summary": summary, "prefix_count": prefix_count}
        parent = self._head(db, sid)["active_version"] if parent is None else parent
        db.execute("INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (version["version_id"], sid, parent, summary, prefix_count, _json(sources),
                    _hash(_json(sources)), str(model)[:256], kind, _now()))
        self._activate(db, sid, version)
        return version["version_id"]

    def capture_context(self, sid):
        """Atomic read used across the (slow) model call; token detects same-length edits and ABA."""
        from compact import summary_message

        with closing(self._conn()) as db, db:
            db.execute("BEGIN")
            self._require_session(db, sid)
            messages = self._load_messages(db, sid)
            sources = self._sources(db, sid)
            self._verified_sources(db, sid, {"sources_json": _json(sources),
                                            "source_hash": _hash(_json(sources)), "prefix_count": len(sources)})
            token = {**self._head(db, sid), "history_hash": _hash(_json(sources))}
            if token["active_version"]:
                version = self._version(db, sid, token["active_version"])
                if not self._compatible(db, sid, version):
                    raise ValueError("当前摘要与原文不一致")
                prefix = version["prefix_count"]
                context = [summary_message(version["summary"]), *messages[prefix:]]
            else:
                prefix, context = 0, list(messages)
        return messages, context, prefix, token

    def _check_token(self, db, sid, token):
        from chatstore import ChatHistoryChanged

        head = self._head(db, sid)
        if (not isinstance(token, dict) or type(token.get("revision")) is not int or token.get("revision") != head["revision"] or
                token.get("active_version") != head["active_version"] or
                ("history_hash" in token and token["history_hash"] != _hash(_json(self._sources(db, sid))))):
            raise ChatHistoryChanged("会话或摘要已在其他窗口更新，请刷新后重试。")

    def memory_state(self, sid):
        with closing(self._conn()) as db, db:
            db.execute("BEGIN")
            self._require_session(db, sid)
            versions = []
            for (vid,) in db.execute("SELECT version_id FROM memory_versions WHERE session_id=? ORDER BY created DESC", (sid,)):
                v = self._version(db, sid, vid)
                try:
                    compatible = self._compatible(db, sid, v)
                    integrity = True
                except (ValueError, KeyError, TypeError):
                    compatible, integrity = False, False
                versions.append({key: v[key] for key in ("version_id", "parent_id", "prefix_count", "source_hash", "model", "kind", "created")}
                                | {"compatible": compatible, "integrity": integrity})
            blocks = [{"block_id": bid, "run_id": run, "thread_id": tid, "kind": kind, "created": created}
                      for bid, run, tid, kind, created in db.execute(
                          "SELECT block_id,run_id,thread_id,kind,created FROM memory_blocks WHERE session_id=? ORDER BY created DESC", (sid,))]
            return {**self._head(db, sid), "versions": versions, "blocks": blocks,
                    "message_count": len(self._sources(db, sid))}

    def memory_version(self, sid, vid, *, offset=0, limit=50):
        with closing(self._conn()) as db, db:
            db.execute("BEGIN")
            self._require_session(db, sid)
            v = self._version(db, sid, vid)
            sources = self._verified_sources(db, sid, v)
            parent = self._version(db, sid, v["parent_id"]) if v["parent_id"] else None
            v.pop("sources_json")
            v["compatible"] = self._compatible(db, sid, self._version(db, sid, vid))
            v["diff"] = "".join(difflib.unified_diff([line + "\n" for line in (parent["summary"] if parent else "").splitlines()],
                                                    [line + "\n" for line in v["summary"].splitlines()], fromfile="父版本", tofile="所选版本"))
            v["sources"] = sources[offset:offset + limit]
            v["source_count"] = len(sources)
            return v

    def original_page(self, sid, *, offset=0, limit=50):
        with closing(self._conn()) as db, db:
            db.execute("BEGIN")
            self._require_session(db, sid)
            sources = self._sources(db, sid)
            page = sources[offset:offset + limit]
            verified = self._verified_sources(db, sid, {"sources_json": _json(page),
                                                        "source_hash": _hash(_json(page)), "prefix_count": len(page)})
            return {"sources": verified, "source_count": len(sources), "offset": offset}

    def original_message(self, sid, mid, revision=None):
        with closing(self._conn()) as db, db:
            db.execute("BEGIN")
            self._require_session(db, sid)
            row = db.execute("SELECT revision,idx,checksum FROM memory_originals WHERE session_id=? AND message_id=? "
                             "AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1", (sid, mid, revision, revision)).fetchone()
            if not row:
                raise LookupError("原文消息不存在或不属于当前会话")
            source = [{"message_id": mid, "revision": row[0], "idx": row[1], "checksum": row[2]}]
            return self._verified_sources(db, sid, {"sources_json": _json(source),
                                                    "source_hash": _hash(_json(source)), "prefix_count": 1})[0]

    def revise_memory(self, sid, vid, summary, token):
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 48000:
            raise ValueError("摘要须为 1–48000 字符")
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_session(db, sid)
            self._check_token(db, sid, token)
            version = self._version(db, sid, vid)
            if not self._compatible(db, sid, version):
                raise ValueError("原文已修改，不能启用基于旧原文的摘要；请重新压缩。")
            return self._publish(db, sid, summary.strip(), version["prefix_count"],
                                 model="manual", kind="edit", parent=vid)

    def restore_memory(self, sid, vid, token):
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_session(db, sid)
            self._check_token(db, sid, token)
            version = self._version(db, sid, vid) if vid else None
            if version and not self._compatible(db, sid, version):
                raise ValueError("原文已修改，不能恢复基于旧原文的摘要；请重新压缩。")
            self._activate(db, sid, version)

    def archive_messages(self, sid, messages, *, run_id="", thread_id="", kind="trace", summary=""):
        from langchain_core.messages import messages_from_dict, messages_to_dict

        raw = _json(messages_to_dict(messages))
        if messages_to_dict(messages_from_dict(json.loads(raw))) != json.loads(raw):
            raise ValueError("消息序列化往返校验失败")
        return self._archive_raw(sid, raw, run_id=run_id, thread_id=thread_id, kind=kind, summary=summary)

    def _archive_raw(self, sid, raw, *, run_id, thread_id, kind, summary, block_id=None):
        checksum = _hash(raw)
        bid = block_id or "b" + uuid.uuid4().hex
        with closing(self._conn()) as db, db:
            self._require_session(db, sid)
            old = db.execute("SELECT block_id,original_json FROM memory_blocks WHERE session_id=? AND run_id=? AND kind=? AND checksum=?",
                             (sid, run_id, kind, checksum)).fetchone()
            if old:
                if _hash(old[1]) != checksum or old[1] != raw:
                    raise ValueError("已有原文块完整性校验失败")
                return old[0]
            db.execute("INSERT INTO memory_blocks VALUES (?,?,?,?,?,?,?,?,?)",
                       (bid, sid, run_id, thread_id, kind, summary, raw, checksum, _now()))
        return bid

    def memory_block(self, sid, bid):
        with closing(self._conn()) as db:
            self._require_session(db, sid)
            row = db.execute("SELECT original_json,checksum FROM memory_blocks WHERE session_id=? AND block_id=?", (sid, bid)).fetchone()
        if not row:
            raise LookupError("原文块不存在或不属于当前会话")
        if _hash(row[0]) != row[1]:
            raise ValueError("原文块完整性校验失败")
        return json.loads(row[0])

    def search_memory(self, sid, keyword):
        from memory_retrieval import MemorySearch
        result = MemorySearch(self).search(sid, keyword, limit=12)
        hits = [f"[{hit['reference']}] {hit['snippet']}" for hit in result["hits"]]
        if result["warnings"]:
            hits.append("原文校验提示：" + "；".join(result["warnings"]))
        return "\n".join(hits)

    def import_legacy_blocks(self, sid, path, thread_prefix):
        """Lazy, exact-scope migration. Legacy DB is read-only and remains untouched."""
        path = Path(path)
        if not path.is_file():
            return
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='context_blocks'").fetchone():
                return
            rows = db.execute("SELECT block_id,thread_id,summary,original_json,checksum FROM context_blocks "
                              "WHERE substr(thread_id,1,?)=?", (len(thread_prefix), thread_prefix)).fetchall()
        for bid, tid, summary, raw, checksum in rows:
            if _hash(raw) != checksum:
                raise ValueError("旧原文块完整性校验失败")
            self._archive_raw(sid, raw, run_id=tid, thread_id=tid, kind="legacy", summary=summary, block_id=bid)
