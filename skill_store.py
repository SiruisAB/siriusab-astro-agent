"""Project-scoped Agent Skills, reviewed package hashes and durable load snapshots.

SKILL.md is data, not executable code. Nothing here runs package scripts or
installs dependencies. Import, activation and resource reads never follow links.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath


NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_PACKAGE = 16 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_SKILL = 64 * 1024
MAX_FILES = 512
OFFICIAL = "https://github.com/anthropics/skills"


class SkillChanged(ValueError):
    pass


@contextmanager
def _import_stage(parent, root):
    # Windows mkdir(0o700), used by TemporaryDirectory, installs an owner-only
    # DACL which survives rename. A sandbox user's import then becomes unreadable
    # to the workbench user. Inherit project ACLs on Windows; stay private on POSIX.
    stage = no_links(parent / ("skill-import-" + uuid.uuid4().hex), root)
    stage.mkdir(mode=0o755 if os.name == "nt" else 0o700)
    try:
        yield stage
    finally:
        no_links(stage, root)
        shutil.rmtree(stage)


def valid_name(name):
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
        raise ValueError("技能名称须为小写字母、数字和单个短横线，最多 64 字符")
    return name


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("技能资源须使用包内相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("技能路径超出包目录")
    for part in path.parts:
        stem = part.split(".")[0].casefold()
        if (any(c in part for c in '<>:"|?*') or part.endswith((".", " ")) or part.startswith(".")
                or part.casefold().startswith(("id_rsa", "id_ed25519")) or PurePosixPath(part).suffix.casefold() in {".key", ".pem", ".p12", ".pfx", ".kdbx"}
                or stem in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}):
            raise ValueError("技能包包含隐藏配置、疑似凭证、设备或非法文件名")
    return path


def no_links(path, boundary):
    path, boundary = Path(path), Path(boundary)
    if not path.is_relative_to(boundary):
        raise ValueError("技能路径超出项目")
    # One non-following stat per ancestor also detects dangling links/junctions.
    # Do not cache: activation and resource reads must validate the current tree.
    candidate = boundary
    for part in (None, *path.relative_to(boundary).parts):
        if part is not None:
            candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("技能目录和资源不得使用符号链接或目录联接")
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("技能路径超出项目")
    return path


def parse_skill(blob, directory):
    if len(blob) > MAX_SKILL:
        raise ValueError("SKILL.md 超过 64 KiB，请把较长说明拆到 references/；不会截断技能说明")
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("SKILL.md 必须使用 UTF-8 编码") from None
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match or len(match[1]) > 16 * 1024:
        raise ValueError("SKILL.md 缺少有效 YAML frontmatter")
    import yaml
    class Loader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("技能元数据键必须是唯一字符串")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        tokens = list(yaml.scan(match[1]))
        if len(tokens) > 512 or any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in tokens):
            raise ValueError("技能 YAML 不支持锚点、别名或过于复杂的元数据")
        header = yaml.load(match[1], Loader=Loader)
    except (yaml.YAMLError, RecursionError):
        raise ValueError("技能 YAML 元数据格式不正确") from None
    if not isinstance(header, dict):
        raise ValueError("技能元数据必须是映射")
    actual_name = valid_name(header.get("name"))
    if directory is not None and actual_name != directory:
        raise ValueError("技能 name 必须与父目录名称一致")
    directory = actual_name
    for field, limit, required in (("description", 1024, True), ("license", 4096, False),
                                   ("compatibility", 500, False), ("allowed-tools", 2048, False)):
        value = header.get(field)
        if value is None and not required:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
            raise ValueError(f"技能 {field} 格式不正确")
    metadata = header.get("metadata", {})
    if not isinstance(metadata, dict) or len(metadata) > 50 or any(not isinstance(v, str) for v in metadata.values()):
        raise ValueError("技能 metadata 必须是字符串键值映射")
    return {"name": directory, "description": header["description"], "license": header.get("license", "未声明"),
            "compatibility": header.get("compatibility", ""), "allowed_tools": header.get("allowed-tools", ""),
            "metadata": metadata, "text": text}


def package_snapshot(folder, boundary):
    no_links(folder, boundary)
    files = []
    for current, directories, filenames in os.walk(folder, followlinks=False):
        for name in directories:
            no_links(Path(current) / name, boundary)
        for name in filenames:
            path = no_links(Path(current) / name, boundary)
            relative = path.relative_to(folder).as_posix()
            safe_relative(relative)
            if not path.is_file():
                raise ValueError("技能包包含非普通文件")
            files.append((relative, path))
            if len(files) > MAX_FILES:
                raise ValueError("技能包文件数超过 512")
    digest, manifest, skill, total = hashlib.sha256(), {}, None, 0
    for relative, path in sorted(files):
        if path.stat().st_size > (MAX_SKILL if relative == "SKILL.md" else MAX_FILE):
            raise ValueError("技能文件过大")
        with path.open("rb") as source:
            blob = source.read(MAX_FILE + 1)
        total += len(blob)
        if len(blob) > MAX_FILE or total > MAX_PACKAGE:
            raise ValueError("技能包超过 16 MiB 安全上限")
        digest.update(relative.encode("utf-8") + b"\0" + len(blob).to_bytes(8, "big") + blob)
        manifest[relative] = {"checksum": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
        if relative == "SKILL.md":
            skill = blob
    if skill is None:
        raise ValueError("技能目录缺少 SKILL.md")
    return {**parse_skill(skill, folder.name), "checksum": digest.hexdigest(), "files": manifest,
            "project_path": f"skills/{folder.name}", "bytes": total}


class SkillStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / "skills"
        self.path = self.root / ".workbench" / "skills.db"

    def _conn(self):
        no_links(self.path.parent, self.root).mkdir(exist_ok=True)
        no_links(self.path, self.root)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, revision TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO settings VALUES (1, 'initial')")
        db.execute("CREATE TABLE IF NOT EXISTS packages (name TEXT PRIMARY KEY, approved TEXT, source TEXT NOT NULL DEFAULT '{}')")
        db.execute("CREATE TABLE IF NOT EXISTS loads (scope TEXT, name TEXT, snapshot TEXT NOT NULL, PRIMARY KEY(scope,name))")
        db.commit()
        return db

    def _state(self):
        with closing(self._conn()) as db:
            revision = db.execute("SELECT revision FROM settings WHERE id=1").fetchone()[0]
            packages = {name: {"approved": approved, "source": json.loads(source)} for name, approved, source in db.execute("SELECT name,approved,source FROM packages")}
        return revision, packages

    def get(self, name, *, enabled=False):
        valid_name(name)
        no_links(self.directory, self.root)
        folder = self.directory / name
        if not folder.is_dir():
            raise ValueError("技能不存在，请先导入或复制到项目 skills/ 目录")
        snapshot = package_snapshot(folder, self.root)
        _, state = self._state()
        settings = state.get(name, {})
        snapshot["enabled"] = settings.get("approved") == snapshot["checksum"]
        snapshot["source"] = settings.get("source", {})
        if enabled and not snapshot["enabled"]:
            raise SkillChanged("技能未启用或包内容已变更，请在技能管理中审阅并重新启用")
        return snapshot

    def catalog(self):
        revision, settings = self._state()
        no_links(self.directory, self.root)
        result, warnings = [], []
        if self.directory.is_dir():
            for folder in sorted(self.directory.iterdir()):
                try:
                    if not folder.is_dir():
                        continue
                    valid_name(folder.name)
                    if len(result) >= 200:
                        raise ValueError("最多展示 200 个项目技能")
                    snapshot = package_snapshot(folder, self.root)
                    entry = {key: value for key, value in snapshot.items() if key not in {"text", "files"}}
                    entry["resources"] = len(snapshot["files"]) - 1
                    old = settings.get(folder.name, {})
                    entry["enabled"] = old.get("approved") == snapshot["checksum"]
                    entry["changed"] = bool(old.get("approved")) and not entry["enabled"]
                    entry["source"] = old.get("source", {})
                    result.append(entry)
                except (OSError, ValueError) as exc:
                    warnings.append(f"{folder.name}: {exc}")
        return {"revision": revision, "directory": str(self.directory), "skills": result, "warnings": warnings[:30]}

    def set_enabled(self, name, enabled, checksum, revision):
        if type(enabled) is not bool:
            raise ValueError("启用状态必须为布尔值")
        snapshot = self.get(name)
        if snapshot["checksum"] != checksum:
            raise SkillChanged("技能已变更，请重新查看完整说明后启用")
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT revision FROM settings WHERE id=1").fetchone()[0] != revision:
                raise SkillChanged("技能设置已在其他窗口更新，请刷新")
            db.execute("INSERT INTO packages(name,approved) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET approved=excluded.approved",
                       (name, checksum if enabled else None))
            db.execute("UPDATE settings SET revision=? WHERE id=1", (uuid.uuid4().hex,))
        return self.catalog()

    def activate(self, name, scope, checksum=None):
        valid_name(name)
        with closing(self._conn()) as db:
            row = db.execute("SELECT snapshot FROM loads WHERE scope=? AND name=?", (scope, name)).fetchone()
        if row:
            snapshot = json.loads(row[0])
            if checksum and checksum != snapshot["checksum"]:
                raise SkillChanged("本轮已经加载另一技能版本，不能在恢复时替换")
            return snapshot
        snapshot = self.get(name, enabled=True)
        if checksum and checksum != snapshot["checksum"]:
            raise SkillChanged("所选技能已变更，请重新审阅并选择")
        with closing(self._conn()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM loads WHERE scope=?", (scope,)).fetchone()[0] >= 6:
                raise ValueError("本轮最多加载 6 个技能")
            db.execute("INSERT OR IGNORE INTO loads VALUES (?,?,?)", (scope, name, json.dumps(snapshot, ensure_ascii=False)))
            saved = db.execute("SELECT snapshot FROM loads WHERE scope=? AND name=?", (scope, name)).fetchone()[0]
        return json.loads(saved)

    def resource(self, name, path, scope, offset=0, limit=16000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32000:
            raise ValueError("资源 offset/limit 格式不正确")
        with closing(self._conn()) as db:
            row = db.execute("SELECT snapshot FROM loads WHERE scope=? AND name=?", (scope, name)).fetchone()
        if not row:
            raise ValueError("请先调用 load_skill 完整读取当前技能说明")
        snapshot = json.loads(row[0])
        current = self.get(name, enabled=True)
        if current["checksum"] != snapshot["checksum"]:
            raise SkillChanged("技能资源已变更，不能替换本轮的已加载版本")
        relative = safe_relative(path).as_posix()
        if relative not in snapshot["files"]:
            raise ValueError("资源不属于当前技能包")
        resource = no_links(self.directory / name / relative, self.root)
        with resource.open("rb") as source:
            blob = source.read(MAX_FILE + 1)
        if hashlib.sha256(blob).hexdigest() != snapshot["files"][relative]["checksum"]:
            raise SkillChanged("技能资源读取期间已变更")
        try:
            text = blob.decode("utf-8-sig")
            if "\0" in text:
                raise UnicodeError
        except UnicodeError:
            return {"path": f"skills/{name}/{relative}", "binary": True, "bytes": len(blob), "text": None,
                    "notice": "二进制资源不注入文本；可通过工作区预览，或在任务/代码模式经审批的脚本中使用"}
        return {"path": f"skills/{name}/{relative}", "checksum": snapshot["files"][relative]["checksum"],
                "text": text[offset:offset+limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(text) else None, "total_chars": len(text)}

    def import_archive(self, blob, name=None, source=None):
        if not isinstance(blob, bytes) or len(blob) > MAX_ARCHIVE:
            raise ValueError("技能 ZIP 超过 32 MiB")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            if len(infos) > 10000:
                raise ValueError("ZIP 条目过多")
            candidates = [info for info in infos if PurePosixPath(info.filename).name == "SKILL.md"]
            if name is None:
                if len(candidates) != 1:
                    raise ValueError("ZIP 须包含一个技能；多技能仓库请先选择要导入的技能名称")
                name = PurePosixPath(candidates[0].filename).parent.name
                if not name:
                    with archive.open(candidates[0]) as item:
                        name = parse_skill(item.read(MAX_SKILL + 1), None)["name"]
            valid_name(name)
            matches = [info for info in candidates if PurePosixPath(info.filename).parent.name == name or
                       (len(candidates) == 1 and PurePosixPath(info.filename).parent.name == "")]
            if len(matches) != 1:
                raise ValueError("ZIP 中未找到唯一匹配的 SKILL.md")
            prefix = str(PurePosixPath(matches[0].filename).parent)
            if prefix == ".":
                prefix = ""
            # Validate the selected prefix too, not just paths after stripping it.
            if prefix:
                safe_relative(prefix)
            selected, seen, size = [], set(), 0
            for info in infos:
                if prefix and not info.filename.startswith(prefix + "/"):
                    continue
                relative = info.filename[len(prefix)+1:] if prefix else info.filename
                if not relative:
                    continue
                relative = safe_relative(relative).as_posix()
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("ZIP 技能不允许链接或特殊文件")
                if info.is_dir():
                    continue
                if relative.casefold() in seen or info.file_size > MAX_FILE:
                    raise ValueError("ZIP 含重复文件名或过大的文件")
                seen.add(relative.casefold())
                selected.append((info, relative))
                size += info.file_size
                if size > MAX_PACKAGE or len(selected) > MAX_FILES:
                    raise ValueError("技能包超过文件数或解压大小限制")
            # Retain repository-level license/notice files when not already bundled.
            top = PurePosixPath(prefix).parts[0] if prefix else ""
            for info in infos:
                path = PurePosixPath(info.filename)
                if (not info.is_dir() and path.parent.as_posix() == top and
                        path.name in {"LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt"} and path.name.casefold() not in seen):
                    if stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_FILE:
                        raise ValueError("许可证文件包含链接或过大内容")
                    selected.append((info, path.name))
                    seen.add(path.name.casefold())
                    size += info.file_size
            if size > MAX_PACKAGE or len(selected) > MAX_FILES:
                raise ValueError("技能包超过大小限制")
            no_links(self.directory, self.root).mkdir(exist_ok=True)
            destination = self.directory / name
            if destination.exists() or destination.is_symlink():
                raise ValueError("同名技能已存在，不会覆盖已有文件；请先手动备份或选择其他技能")
            no_links(self.path.parent, self.root).mkdir(exist_ok=True)
            with _import_stage(self.path.parent, self.root) as stage:
                folder = Path(stage) / name
                folder.mkdir()
                for info, relative in selected:
                    with archive.open(info) as item:
                        content = item.read(MAX_FILE + 1)
                    if len(content) != info.file_size or len(content) > MAX_FILE:
                        raise ValueError("ZIP 解压内容大小不一致")
                    target = folder / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                snapshot = package_snapshot(folder, self.root)
                with closing(self._conn()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    if destination.exists() or destination.is_symlink():
                        raise SkillChanged("导入过程中已出现同名技能，不会覆盖")
                    folder.rename(destination)
                    origin = {**(source or {"type": "zip"}), "archive_sha256": hashlib.sha256(blob).hexdigest()}
                    db.execute("INSERT INTO packages(name,source) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET source=excluded.source,approved=NULL",
                               (name, json.dumps(origin, ensure_ascii=False)))
                    db.execute("UPDATE settings SET revision=? WHERE id=1", (uuid.uuid4().hex,))
        return {"name": name, "checksum": snapshot["checksum"], "enabled": False}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("技能下载不接受重定向，请使用固定官方仓库来源")


def fetch_official(url, limit):
    # Callers only supply the two fixed, unauthenticated HTTPS URLs below.
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "astro-agent-skills/1", "Accept": "application/octet-stream"})
    begun, parts, total = time.monotonic(), [], 0
    with opener.open(request, timeout=15) as response:
        while True:
            if time.monotonic() - begun > 30:
                raise ValueError("官方技能下载超过 30 秒，请稍后重试或上传 ZIP")
            part = response.read(64 * 1024)
            if not part:
                break
            total += len(part)
            if total > limit:
                raise ValueError("官方技能下载超过大小上限")
            parts.append(part)
    return b"".join(parts)


def official_catalog():
    blob = fetch_official("https://raw.githubusercontent.com/anthropics/skills/main/.claude-plugin/marketplace.json", 256 * 1024)
    data = json.loads(blob)
    names = set()
    for plugin in data.get("plugins", []):
        for path in plugin.get("skills", []):
            if isinstance(path, str) and path.startswith("./skills/"):
                name = path[len("./skills/"):]
                valid_name(name)
                names.add(name)
    return {"repository": OFFICIAL, "skills": sorted(names)}


def import_official(store, name):
    valid_name(name)
    no_links(store.directory, store.root)
    destination = store.directory / name
    if destination.exists() or destination.is_symlink():
        raise ValueError("同名技能已存在，不会下载或覆盖；请先手动备份")
    try:
        archive = fetch_official("https://codeload.github.com/anthropics/skills/zip/refs/heads/main", MAX_ARCHIVE)
    except (OSError, ValueError):
        return _import_official_sparse(store, name)
    return store.import_archive(archive, name, {"type": "anthropics", "repository": OFFICIAL, "ref": "main"})


def _import_official_sparse(store, name):
    """Small-package fallback: fixed public repo, no hooks, filters or submodules.

    Isolated Git configuration prevents executing user-configured checkout
    hooks/smudge filters. Only tracked skill files and license notices are packed.
    """
    no_links(store.path.parent, store.root).mkdir(exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    with tempfile.TemporaryDirectory(prefix="skill-source-", dir=store.path.parent) as stage:
        checkout = Path(stage) / "repository"
        def git(args):
            try:
                result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", *args],
                                        env=environment, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                raise ValueError("官方 Git 下载超时，请稍后重试或上传 ZIP") from None
            if result.returncode:
                raise ValueError("官方 Git 技能下载失败，请稍后重试或上传 ZIP：" + result.stderr[-1000:])
            return result.stdout.strip()
        git(["clone", "--depth=1", "--branch=main", "--filter=blob:none", "--sparse", "--", OFFICIAL + ".git", str(checkout)])
        git(["-C", str(checkout), "sparse-checkout", "set", "skills/" + name])
        commit = git(["-C", str(checkout), "rev-parse", "HEAD"])
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("无法核验官方技能 Git 提交")
        folder = checkout / "skills" / name
        tracked = git(["-C", str(checkout), "ls-files", "--stage", "--", "skills/" + name])
        seen = set()
        for line in tracked.splitlines():
            mode, _, _, path = line.split(maxsplit=3)
            if mode in {"120000", "160000"} or path.casefold() in seen:
                raise ValueError("官方技能包含链接、子模块或大小写冲突，当前导入器拒绝复制")
            seen.add(path.casefold())
        package_snapshot(folder, store.root)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, "repo/skills/" + name + "/" + path.relative_to(folder).as_posix())
            for filename in ("LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt"):
                path = checkout / filename
                no_links(path, store.root)
                if path.is_file() and path.stat().st_size <= MAX_FILE:
                    archive.write(path, "repo/" + filename)
        return store.import_archive(stream.getvalue(), name, {"type":"anthropics", "repository":OFFICIAL,
            "ref":"main", "commit":commit, "transport":"git-sparse"})
