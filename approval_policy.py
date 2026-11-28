"""Conservative read-only command rules and file-change previews."""

import difflib
import hashlib
import re
import shlex
from pathlib import Path


def is_read_only_command(command, shell, root=None):
    if not isinstance(command, str) or not command.strip() or re.search(r"[;&|<>`$\r\n(){}@]", command):
        return False
    try:
        # PowerShell does not treat backslashes as escapes. POSIX parsing would
        # turn ..\..\private.txt into ....private.txt and hide path traversal.
        args = shlex.split(command, posix=shell != "powershell")
        if shell == "powershell":
            args = [arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
                    for arg in args]
    except ValueError:
        return False
    if not args or any(arg.startswith(("--pre", "--exec", "-Command", "-EncodedCommand")) for arg in args):
        return False
    name = args[0].lower()
    if root is not None:
        for value in args[1:]:
            if value.startswith("~") or any(character in value for character in "*?[]"):
                return False
            if value.startswith("-") or value.isdecimal():
                continue
            candidate = Path(value)
            if ":" in value and not candidate.is_absolute():
                return False
            candidate = (candidate if candidate.is_absolute() else Path(root) / candidate).resolve()
            if not candidate.is_relative_to(Path(root).resolve()):
                return False
    if shell == "powershell":
        no_args = {"get-location", "get-date", "whoami", "hostname"}
        readers = {"get-childitem", "get-item", "get-content", "test-path"}
        if name in no_args:
            return len(args) == 1
        allowed_flags = {"-literalpath", "-path", "-force", "-name", "-file", "-directory", "-recurse",
                         "-depth", "-raw", "-totalcount", "-tail", "-encoding"}
        return name in readers and all(not arg.startswith("-") or arg.lower() in allowed_flags for arg in args[1:])
    if name in {"pwd", "whoami", "hostname"}:
        return len(args) == 1
    flags = {
        "ls": {"-l", "-a", "-la", "-al", "-h", "-lh", "-lah", "-F", "--"},
        "cat": {"-n", "-b", "-s", "-A", "--"}, "head": {"-n", "-c", "--"},
        "tail": {"-n", "-c", "--"}, "wc": {"-l", "-w", "-m", "-c", "-L", "--"},
        "stat": {"-c", "--format", "--file-system", "-f", "--"},
        "file": {"-b", "--brief", "-i", "--mime", "--mime-type", "--"},
        "uname": {"-a", "-s", "-r", "-m", "-n", "-v", "-p", "-i", "-o"},
    }
    return name in flags and all(not arg.startswith("-") or arg in flags[name] for arg in args[1:])


def resolve_file(root, value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("请使用项目内的虚拟路径")
    path = (Path(root) / value.lstrip("/")).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("路径超出工作区")
    return path


def needs_file_review(root, name, args):
    try:
        path = resolve_file(root, args.get("file_path", ""))
        relative = path.relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return True
    # New analysis artifacts can be written directly; edits and overwrites are reviewed.
    return name != "write_file" or path.exists() or not relative.parts or relative.parts[0] != "results"


def file_preview(root, name, args):
    path = resolve_file(root, args.get("file_path", ""))
    old = ""
    if path.exists():
        if not path.is_file() or path.stat().st_size > 128 * 1024:
            return f"{name}: {args.get('file_path')}\n文件较大或不是普通文本文件，请核对拟议参数。"
        old = path.read_text(encoding="utf-8", errors="replace")
    if name == "write_file":
        new = args.get("content", "")
    else:
        target, replacement = args.get("old_string", ""), args.get("new_string", "")
        if not target or target not in old:
            return f"{name}: {args.get('file_path')}\n原字符串未找到，请核对修改参数。"
        new = old.replace(target, replacement, -1 if args.get("replace_all") else 1)
    diff = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                    fromfile=str(args.get("file_path")) + "（修改前）",
                                    tofile=str(args.get("file_path")) + "（修改后）"))
    return f"{name}: {args.get('file_path')}\n{diff[:12000]}" + ("\n…差异预览已截断" if len(diff) > 12000 else "")


def file_fingerprint(root, value):
    path = resolve_file(root, value)
    if not path.exists():
        return None
    if path.is_file() and path.stat().st_size <= 128 * 1024:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def interrupt_config(root):
    def shell_review(shell):
        return lambda request: not is_read_only_command(request.tool_call["args"].get("command"), shell, root)

    def file_rule(name):
        def description(tool_call, state, runtime):
            try:
                return file_preview(root, name, tool_call["args"])
            except (OSError, ValueError) as exc:
                return f"{name}: {tool_call['args']}\n无法预览：{exc}"
        return {"allowed_decisions": ["approve", "edit", "reject"],
                "when": lambda request: needs_file_review(root, name, request.tool_call["args"]),
                "description": description}

    return {"run_powershell": {"allowed_decisions": DEFAULT, "when": shell_review("powershell")},
            "run_bash": {"allowed_decisions": DEFAULT, "when": shell_review("bash")},
            "write_file": file_rule("write_file"), "edit_file": file_rule("edit_file")}


DEFAULT = ["approve", "edit", "reject"]
