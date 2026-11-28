"""Local experiment provenance, frozen source snapshots and reproduction preflight.

This module records user-reported execution. It never runs the stored command,
changes an environment, uploads data, or treats matching hashes as scientific proof.
"""

import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from skill_store import no_links, safe_relative
from web_advisor import check_text, secret_values
from workbench_settings import WorkbenchSettings

MODES = {"chat", "agent", "research", "code"}
CODE_EXTENSIONS = {".py", ".jl", ".r", ".cpp", ".cc", ".c", ".h", ".hpp", ".cu", ".cuh", ".rs", ".js", ".ts", ".ipynb", ".toml", ".yaml", ".yml", ".json", ".txt", ".md", ".tex"}
MAX_CODE = 512 * 1024
MAX_CODE_TOTAL = 2 * 1024 * 1024
MAX_DATA = 512 * 1024 * 1024
MAX_DATA_TOTAL = 1024 * 1024 * 1024
SPEC_FIELDS = {"title", "hypothesis", "command", "code_files", "input_files", "parameters", "seed", "units", "hardware_notes", "shared"}


class ExperimentChanged(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"{name}须为不超过 {limit} 字符的文本")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{name}")
    return value


def paths(values, name, required=False):
    if not isinstance(values, list) or len(values) > 16 or any(not isinstance(v, str) for v in values):
        raise ValueError(f"{name}须为最多 16 个项目内文件路径")
    result = []
    for value in values:
        path = safe_relative(value).as_posix()
        parts = Path(path).parts
        if (any(p.casefold() in {"node_modules", "conversation_history", "__pycache__"} or p.casefold().startswith("venv") for p in parts)
                or re.match(r"(?i)^(?:credentials?|secrets?|passwords?|api[-_]?keys?|auth|tokens?|sessions)(?:[._-]|$)", Path(path).name)):
            raise ValueError("不能记录凭证、会话数据库、缓存或依赖目录")
        result.append(path)
    if len(set(result)) != len(result) or required and not result:
        raise ValueError(f"{name}不能重复，且至少选择一个源码文件")
    return result


def object_value(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name}须为 JSON 对象")
    try:
        blob = encoded(value)
    except (TypeError, ValueError, RecursionError):
        raise ValueError(f"{name}须为有限的普通 JSON 数据，不能含 NaN/Infinity") from None
    if len(blob.encode()) > 16 * 1024:
        raise ValueError(f"{name}超过 16 KiB")
    return json.loads(blob)


def validate_spec(value):
    if not isinstance(value, dict) or set(value) - SPEC_FIELDS:
        raise ValueError("实验计划字段不正确")
    result = {"title": text(value.get("title", ""), "实验名称", 160, True),
              "hypothesis": text(value.get("hypothesis", ""), "假设/目标", 6000, True),
              "command": text(value.get("command", ""), "运行命令", 6000, True),
              "code_files": paths(value.get("code_files", []), "源码", True),
              "input_files": paths(value.get("input_files", []), "输入文件"),
              "parameters": object_value(value.get("parameters", {}), "参数"),
              "units": object_value(value.get("units", {}), "单位"),
              "hardware_notes": text(value.get("hardware_notes", ""), "硬件/外部资源说明", 2000),
              "seed": value.get("seed"), "shared": value.get("shared", False)}
    if result["seed"] is not None and (type(result["seed"]) is not int or not 0 <= result["seed"] <= 2**64 - 1):
        raise ValueError("随机种子须为空或 0 到 2^64-1 的整数")
    if type(result["shared"]) is not bool:
        raise ValueError("共享确认须为布尔值")
    if any(Path(path).suffix.lower() not in CODE_EXTENSIONS for path in result["code_files"]):
        raise ValueError("源码须为支持的文本、配置或 Notebook 文件")
    return result


def environment_snapshot():
    # Capture metadata only, never import scientific packages or freeze URL credentials.
    from importlib.metadata import distributions
    packages = {}
    for package in distributions():
        name = package.metadata.get("Name")
        if name and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", name):
            packages[re.sub(r"[-_.]+", "-", name).lower()] = str(package.version)[:160]
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "executable": sys.executable, "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "packages": dict(sorted(packages.items())),
            "package_sha256": digest(packages)}


def git_snapshot(root, selected):
    git = shutil.which("git")
    if not git:
        return {"available": False, "reason": "Git 未安装；只有显式源码快照"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    def run(*args):
        result = subprocess.run([git, "--no-pager", "-c", "core.fsmonitor=false", "-c", "color.ui=false", *args], cwd=root,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.returncode, result.stdout
    try:
        code, top = run("rev-parse", "--show-toplevel")
        if code or Path(os.fsdecode(top).strip()).resolve() != root:
            return {"available": False, "reason": "当前工作区不是独立 Git 仓库；只有显式源码快照"}
        _, head = run("rev-parse", "--verify", "HEAD")
        head = head.decode("ascii", "ignore").strip()
        head = head if re.fullmatch(r"[a-f0-9]{40,64}", head) else None
        _, branch = run("branch", "--show-current")
        _, status = run("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", *selected)
        _, global_status = run("status", "--porcelain=v1", "-z", "--untracked-files=no")
        code, diff = run("diff", "--no-ext-diff", "--no-textconv", "--no-renames", *( ["HEAD"] if head else []), "--", *selected)
        if code or len(diff) > MAX_CODE_TOTAL:
            raise ValueError("Git 差异过大或读取失败，请减少所选源码")
        return {"available": True, "commit": head, "branch": branch.decode("utf-8", "replace").strip()[:160] or None,
                "selected_dirty": bool(status), "selected_status_sha256": hashlib.sha256(status).hexdigest(),
                "working_tree_dirty": bool(global_status), "selected_diff": diff.decode("utf-8"),
                "selected_diff_sha256": hashlib.sha256(diff).hexdigest(), "diff_scope": "仅显式源码；未跟踪源码由完整快照补足"}
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return {"available": False, "reason": "Git 信息采集失败；不声称已保存完整仓库状态"}


class ExperimentStore:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.path = no_links(self.root / ".workbench" / "experiments.db", self.root)
        self.path.parent.mkdir(exist_ok=True)
        with closing(self._conn()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,mode TEXT NOT NULL,sid TEXT NOT NULL,shared INTEGER NOT NULL,
                status TEXT NOT NULL,revision INTEGER NOT NULL,parent_id TEXT,
                spec_json TEXT NOT NULL,baseline_json TEXT NOT NULL,started_json TEXT,outcome_json TEXT,
                created TEXT NOT NULL,updated TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS experiment_events (
                id TEXT NOT NULL,revision INTEGER NOT NULL,event TEXT NOT NULL,content_json TEXT NOT NULL,
                previous_sha256 TEXT NOT NULL,sha256 TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(id,revision))""")
            db.execute("""CREATE TABLE IF NOT EXISTS experiment_code (
                id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,content BLOB NOT NULL,PRIMARY KEY(id,path))""")
            db.execute("CREATE TABLE IF NOT EXISTS experiment_creates (operation_key TEXT PRIMARY KEY,id TEXT NOT NULL,request_sha256 TEXT NOT NULL)")

    def _conn(self):
        no_links(self.path, self.root)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _secrets(self):
        keys = secret_values()
        for model in WorkbenchSettings(self.root).load().get("models", {}).values():
            key = model.get("api_key")
            if isinstance(key, str) and len(key) >= 8 and key != "not-needed": keys.append(key)
        return keys

    def _files(self, names, *, code=False, missing=False):
        items, blobs, total, end = [], {}, 0, time.monotonic() + 30
        for name in names:
            path = no_links(self.root / name, self.root)
            try:
                info = path.stat()
            except FileNotFoundError:
                if missing:
                    items.append({"path":name,"missing":True,"sha256":None,"bytes":None})
                    continue
                raise ValueError("实验文件不存在，请核对显式路径") from None
            limit = MAX_CODE if code else MAX_DATA
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("实验文件不是普通文件或超过预算（源码 512 KiB，数据 512 MiB）")
            fingerprint = hashlib.sha256()
            chunks, count = [], 0
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    count += len(chunk)
                    if count > limit or time.monotonic() > end:
                        raise ValueError("文件在读取中变大或采集超时，未保存不完整校验值")
                    fingerprint.update(chunk)
                    if code: chunks.append(chunk)
                after = os.fstat(source.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("实验文件在采集中发生变化，请重试冻结")
            total += count
            if total > (MAX_CODE_TOTAL if code else MAX_DATA_TOTAL):
                raise ValueError("源码总计超过 2 MiB 或数据总计超过 1 GiB，请减少文件")
            item = {"path":name,"bytes":count,"sha256":fingerprint.hexdigest()}
            if code:
                blob = b"".join(chunks)
                try: decoded = blob.decode("utf-8-sig")
                except UnicodeError: raise ValueError("源码快照须为 UTF-8 文本") from None
                if "\0" in decoded: raise ValueError("源码不是普通文本")
                check_text(decoded, self._secrets())
                blobs[name] = blob
                item["snapshot"] = "code/" + name
            items.append(item)
        return items, blobs

    @staticmethod
    def _scope(mode, sid):
        if mode not in MODES or not isinstance(sid, str) or len(sid) > 160:
            raise ValueError("实验记录所属模式或会话不正确")

    @staticmethod
    def _unpack(row):
        data = dict(row)
        for field in ("spec", "baseline", "started", "outcome"):
            value = data.pop(field + "_json")
            data[field] = json.loads(value) if value else None
        data["shared"] = bool(data["shared"])
        data["execution_observed_by_workbench"] = False
        return data

    def get(self, identifier, mode, sid, *, all_project=False, write=False):
        self._scope(mode, sid)
        with closing(self._conn()) as db:
            row = db.execute("SELECT * FROM experiments WHERE id=?", (identifier,)).fetchone()
        if row is None or (row["mode"] != mode or row["sid"] != sid) and (write or not (row["shared"] or all_project)):
            raise LookupError("实验不存在或不属于当前会话")
        return self._unpack(row)

    def list(self, mode, sid, *, all_project=False):
        self._scope(mode, sid)
        with closing(self._conn()) as db:
            rows = db.execute("SELECT * FROM experiments " + ("" if all_project else "WHERE (mode=? AND sid=?) OR shared=1 ") + "ORDER BY created DESC LIMIT 100",
                              () if all_project else (mode, sid)).fetchall()
        return [{"id":r["id"],"title":json.loads(r["spec_json"])["title"],"status":r["status"],"revision":r["revision"],
                 "mode":r["mode"],"sid":r["sid"],"shared":bool(r["shared"]),"created":r["created"],"parent_id":r["parent_id"]} for r in rows]

    @staticmethod
    def _event(db, identifier, revision, event, content):
        previous = db.execute("SELECT sha256 FROM experiment_events WHERE id=? ORDER BY revision DESC LIMIT 1", (identifier,)).fetchone()
        previous = previous[0] if previous else ""
        stamp = now()
        fingerprint = digest([identifier, revision, event, content, previous, stamp])
        db.execute("INSERT INTO experiment_events VALUES(?,?,?,?,?,?,?)", (identifier,revision,event,encoded(content),previous,fingerprint,stamp))

    def create(self, spec, mode, sid, *, parent_id=None, operation_key=None):
        self._scope(mode, sid)
        spec = validate_spec(spec)
        check_text(encoded(spec), self._secrets())
        if parent_id: self.get(parent_id, mode, sid)
        fingerprint = digest([spec, parent_id])
        def prior(db):
            row = db.execute("SELECT id,request_sha256 FROM experiment_creates WHERE operation_key=?", (operation_key,)).fetchone() if operation_key else None
            if row and row["request_sha256"] != fingerprint:
                raise ExperimentChanged("创建标识已使用，不能换参数覆盖原计划")
            return row["id"] if row else None
        with closing(self._conn()) as db:
            old = prior(db)
        if old: return self.get(old, mode, sid)
        code, blobs = self._files(spec["code_files"], code=True)
        inputs, _ = self._files(spec["input_files"])
        git = git_snapshot(self.root, spec["code_files"])
        check_text(encoded(git), self._secrets())
        environment = environment_snapshot()
        check_text(encoded(environment), self._secrets())
        warnings = ["仅冻结显式源码；导入的其他模块和外部依赖不保证完整", "输入及结果只存校验值，不备份数据", "未自动采集 GPU/驱动/系统包，不保证数值确定性"]
        if not git["available"] or not git.get("commit"): warnings.append("缺少 Git 提交信息；源码快照仍独立保留")
        if spec["seed"] is None: warnings.append("未声明随机种子")
        if not spec["units"]: warnings.append("未声明物理量单位")
        baseline = {"captured_at":now(),"project":str(self.root),"code":code,"inputs":inputs,"git":git,"environment":environment,"warnings":warnings}
        identifier, stamp = "e" + uuid.uuid4().hex, now()
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = prior(db)
            if old: return self.get(old, mode, sid)
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier,mode,sid,int(spec["shared"]),"planned",1,parent_id,encoded(spec),encoded(baseline),None,None,stamp,stamp))
            for path, blob in blobs.items():
                db.execute("INSERT INTO experiment_code VALUES(?,?,?,?)", (identifier,path,hashlib.sha256(blob).hexdigest(),blob))
            self._event(db, identifier, 1, "planned", {"spec":spec,"baseline":baseline,"parent_id":parent_id,"owner":{"mode":mode,"sid":sid}})
            if operation_key: db.execute("INSERT INTO experiment_creates VALUES(?,?,?)", (operation_key,identifier,fingerprint))
        return self.get(identifier, mode, sid)

    def events(self, identifier):
        with closing(self._conn()) as db:
            rows = db.execute("SELECT * FROM experiment_events WHERE id=? ORDER BY revision", (identifier,)).fetchall()
        return [{**{k:row[k] for k in row.keys() if k != "content_json"},"content":json.loads(row["content_json"])} for row in rows]

    def _integrity(self, record):
        events = self.events(record["id"])
        previous = ""
        for index, event in enumerate(events, 1):
            if (event["revision"] != index or event["previous_sha256"] != previous
                    or event["sha256"] != digest([record["id"],index,event["event"],event["content"],previous,event["created"]])):
                raise ValueError("实验事件记录校验失败")
            previous = event["sha256"]
        if not events or len(events) != record["revision"] or events[0]["content"]["baseline"] != record["baseline"] or events[0]["content"]["spec"] != record["spec"]:
            raise ValueError("实验基线或修订记录校验失败")
        if (events[0]["content"].get("owner") != {"mode":record["mode"],"sid":record["sid"]}
                or events[0]["content"].get("parent_id") != record["parent_id"] or record["shared"] != record["spec"]["shared"]
                or events[-1]["event"] != record["status"]
                or record["status"] == "running" and record["started"] != events[-1]["content"]
                or record["status"] in {"succeeded","failed","cancelled"} and record["outcome"] != events[-1]["content"]):
            raise ValueError("实验状态或登记结果校验失败")
        with closing(self._conn()) as db:
            blobs = db.execute("SELECT path,sha256,content FROM experiment_code WHERE id=?", (record["id"],)).fetchall()
        expected = {item["path"]:item["sha256"] for item in record["baseline"]["code"]}
        if len(blobs) != len(expected) or any(expected.get(row["path"]) != row["sha256"] or hashlib.sha256(row["content"]).hexdigest() != row["sha256"] for row in blobs):
            raise ValueError("冻结源码快照校验失败")
        return events, blobs

    def check(self, identifier, mode, sid, *, all_project=False):
        record = self.get(identifier, mode, sid, all_project=all_project)
        self._integrity(record)
        differences, errors = [], []
        for kind in ("code", "inputs"):
            for expected in record["baseline"][kind]:
                try:
                    actual, _ = self._files([expected["path"]], missing=True)
                    actual = actual[0]
                    if actual["sha256"] != expected["sha256"]:
                        differences.append({"kind":kind,"path":expected["path"],"expected":expected["sha256"],"actual":actual["sha256"],"missing":actual.get("missing", False)})
                except (OSError, ValueError) as exc:
                    errors.append({"kind":kind,"path":expected["path"],"error":type(exc).__name__})
        try:
            env = environment_snapshot()
            baseline = record["baseline"]["environment"]
            for key in ("python", "implementation", "platform", "machine", "package_sha256"):
                if env.get(key) != baseline.get(key): differences.append({"kind":"environment","field":key,"expected":baseline.get(key),"actual":env.get(key)})
        except Exception: errors.append({"kind":"environment","error":"capture_failed"})
        try:
            git = git_snapshot(self.root, record["spec"]["code_files"])
        except (OSError, ValueError):
            git = {"available":False}
            errors.append({"kind":"git","error":"capture_failed"})
        original = record["baseline"]["git"]
        if original.get("available"):
            for key in ("available", "commit", "selected_diff_sha256", "selected_status_sha256"):
                if git.get(key) != original.get(key): differences.append({"kind":"git","field":key,"expected":original.get(key),"actual":git.get(key)})
        return {"id":identifier,"revision":record["revision"],"checked_at":now(),"status":"check_incomplete" if errors else "conditions_changed" if differences else "conditions_match",
                "differences":differences,"errors":errors,"warnings":record["baseline"]["warnings"],
                "scientific_reproduction_verified":False,"command_executed":False,"environment_changed":False}

    def start(self, identifier, mode, sid, revision):
        record = self.get(identifier, mode, sid, write=True)
        check = self.check(identifier, mode, sid)
        if check["status"] != "conditions_match":
            raise ValueError("当前代码、输入或环境与基线不同，请查看复现检查并新建计划；不会覆盖原基线")
        started = {"recorded_at":now(),"reported_by":"user","execution_observed_by_workbench":False,"preflight":check}
        return self._transition(record, revision, {"planned"}, "running", "started_json", started)

    def finish(self, identifier, mode, sid, revision, value):
        record = self.get(identifier, mode, sid, write=True)
        if not isinstance(value, dict) or set(value) - {"status","exit_code","artifacts","metrics","notes"}:
            raise ValueError("结果登记字段不正确")
        status_value, exit_code = value.get("status"), value.get("exit_code")
        if not isinstance(status_value, str) or status_value not in {"succeeded","failed","cancelled"}:
            raise ValueError("结果状态须为 succeeded、failed 或 cancelled")
        if exit_code is not None and (type(exit_code) is not int or not -(2**31) <= exit_code <= 2**31 - 1):
            raise ValueError("退出码须为空或整数")
        if status_value == "succeeded" and exit_code not in {None,0}:
            raise ValueError("非零退出码不能登记为进程成功；科学判断请写到说明中")
        names = paths(value.get("artifacts", []), "结果文件")
        metrics = object_value(value.get("metrics", {}), "结果指标")
        notes = text(value.get("notes", ""), "结果说明", 6000)
        check_text(encoded({"metrics":metrics,"notes":notes}), self._secrets())
        artifacts, _ = self._files(names, missing=status_value != "succeeded")
        outcome = {"reported_status":status_value,"reported_exit_code":exit_code,"artifacts":artifacts,"metrics":metrics,"notes":notes,
                   "reported_by":"user","recorded_at":now(),"execution_observed_by_workbench":False,"scientific_validity_verified":False,
                   "postflight":self.check(identifier, mode, sid),"warnings":[] if artifacts else ["未登记结果文件，仅保留人工指标或说明"]}
        return self._transition(record, revision, {"running"}, status_value, "outcome_json", outcome)

    def _transition(self, record, revision, allowed, status_value, column, value):
        if type(revision) is not int or revision != record["revision"]:
            raise ExperimentChanged("实验记录已更新，请重新读取后再登记")
        if record["status"] not in allowed:
            raise ValueError("实验状态不允许此操作；已冻结或完成的记录不会被覆盖")
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(f"UPDATE experiments SET status=?,revision=revision+1,{column}=?,updated=? WHERE id=? AND revision=? AND status=?",
                                (status_value,encoded(value),now(),record["id"],revision,record["status"]))
            if result.rowcount != 1: raise ExperimentChanged("实验记录已在另一个窗口更新")
            self._event(db, record["id"], revision+1, status_value, value)
        return self.get(record["id"], record["mode"], record["sid"])

    def export(self, identifier, mode, sid, *, all_project=False):
        record = self.get(identifier, mode, sid, all_project=all_project)
        events, blobs = self._integrity(record)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", encoded({"schema":"research-experiment/v1","record":record}) + "\n")
            archive.writestr("events.json", encoded(events) + "\n")
            archive.writestr("python-packages.txt", "\n".join(f"{name}=={version}" for name,version in record["baseline"]["environment"]["packages"].items()) + "\n")
            archive.writestr("selected-code.patch", record["baseline"]["git"].get("selected_diff", ""))
            archive.writestr("README.txt", "这是复现资料包，不会执行命令。源码只包含显式选择的文件；输入/结果只含校验值，需自行提供原文件。\nPython 包版本不是包含系统依赖/轮子哈希的完整锁文件。记录的结果由用户报告，未验证科学正确性。分享前检查源码、路径与隐私信息。\n")
            for row in blobs:
                archive.writestr("code/" + safe_relative(row["path"]).as_posix(), row["content"])
        return buffer.getvalue()
