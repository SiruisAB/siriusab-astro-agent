"""Local workbench configuration, with write-only API keys and atomic saves."""

import copy
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from model_endpoints import resolve_endpoint


ROOT = Path(__file__).resolve().parent
PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")

WEB_ADVISOR_URLS = {"chatgpt": "https://chatgpt.com/", "claude": "https://claude.ai/new",
                    "gemini": "https://gemini.google.com/app"}
DEFAULT_WEB_ADVISOR = {"enabled": False, "cdp_endpoint": "http://127.0.0.1:9222",
                       "dedicated_profile": False, "providers": ["chatgpt", "claude", "gemini"], "timeout": 180,
                       "browser_type": "dedicated", "proxy_mode": "system", "proxy_url": "",
                       "fingerprint_proxy_confirmed": False, "auto_close_minutes": 0}


def normalize_cdp_endpoint(value):
    if not isinstance(value, str) or len(value) > 200 or any(char.isspace() for char in value):
        raise ValueError("CDP 地址格式不正确")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("CDP 地址格式不正确") from None
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or not port or not 1 <= port <= 65535 or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("CDP 仅允许本机回环地址，如 http://127.0.0.1:9222；不能开放到局域网或 Tailscale")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def normalize_local_proxy(value):
    if not isinstance(value, str) or len(value) > 200 or any(char.isspace() for char in value):
        raise ValueError("本机代理地址格式不正确")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("本机代理地址格式不正确") from None
    if (parsed.scheme not in {"http", "https", "socks5"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or not port or not 1 <= port <= 65535 or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("代理只允许带端口的本机 HTTP/HTTPS/SOCKS5 地址，不能包含账号密码，如 http://127.0.0.1:7890")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"{parsed.scheme}://{host}:{port}"


def web_advisor_config(value=None):
    if value is None:
        return copy.deepcopy(DEFAULT_WEB_ADVISOR)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_WEB_ADVISOR):
        raise ValueError("网页顾问设置格式不正确")
    result = {**DEFAULT_WEB_ADVISOR, **value}
    if type(result["enabled"]) is not bool or type(result["dedicated_profile"]) is not bool:
        raise ValueError("网页顾问启用与专用浏览器确认必须为布尔值")
    if result["enabled"] and not result["dedicated_profile"]:
        raise ValueError("启用前请确认使用专用浏览器配置，并已手动登录所选网站")
    providers = result["providers"]
    if (not isinstance(providers, list) or len(providers) > len(WEB_ADVISOR_URLS)
            or any(not isinstance(provider, str) or provider not in WEB_ADVISOR_URLS for provider in providers)
            or len(set(providers)) != len(providers)):
        raise ValueError("网页顾问只支持 ChatGPT、Claude 和 Gemini")
    if result["enabled"] and not providers:
        raise ValueError("请至少选择一个网页顾问")
    if type(result["timeout"]) is not int or not 30 <= result["timeout"] <= 600:
        raise ValueError("网页顾问超时须为 30 到 600 秒")
    if type(result["auto_close_minutes"]) is not int or not 0 <= result["auto_close_minutes"] <= 240:
        raise ValueError("空闲自动关闭须为 0（关闭该项）到 240 分钟")
    if not isinstance(result["browser_type"], str) or result["browser_type"] not in {"dedicated", "fingerprint"}:
        raise ValueError("请选择普通专用浏览器或指纹浏览器（Chromium/CDP）")
    if not isinstance(result["proxy_mode"], str) or result["proxy_mode"] not in {"system", "direct", "custom"}:
        raise ValueError("代理模式只支持跟随系统、直连或指定本机代理")
    if not isinstance(result["proxy_url"], str):
        raise ValueError("本机代理地址必须为文本")
    if result["proxy_url"] or result["proxy_mode"] == "custom":
        result["proxy_url"] = normalize_local_proxy(result["proxy_url"])
    if type(result["fingerprint_proxy_confirmed"]) is not bool:
        raise ValueError("指纹浏览器代理确认必须为布尔值")
    if result["enabled"] and result["browser_type"] == "fingerprint" and not result["fingerprint_proxy_confirmed"]:
        raise ValueError("请先在指纹浏览器中应用所选代理方式、重新启动该配置，并明确确认；工作台不会自动修改它的代理")
    result["cdp_endpoint"] = normalize_cdp_endpoint(result["cdp_endpoint"])
    result["providers"] = list(providers)
    return result


class SettingsChanged(ValueError):
    pass


def _text(value, field, limit=256, *, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field}格式不正确")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{field}")
    return value


class WorkbenchSettings:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.path = self.root / ".workbench" / "settings.json"

    def load(self):
        if not self.path.exists():
            return {"version": 1, "revision": "initial", "port": 8765,
                    "models": {}, "disabled_profiles": [], "web_advisor": web_advisor_config()}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or type(data.get("port")) is not int or not 1 <= data["port"] <= 65535
                    or not isinstance(data.get("revision"), str)
                    or not isinstance(data.get("models"), dict)
                    or not isinstance(data.get("disabled_profiles"), list)
                    or any(not isinstance(name, str) or not isinstance(model, dict) for name, model in data["models"].items())
                    or any(not isinstance(name, str) for name in data["disabled_profiles"])):
                raise ValueError
            data["web_advisor"] = web_advisor_config(data.get("web_advisor"))
            return data
        except (ValueError, UnicodeError) as exc:
            raise ValueError("本地设置文件格式不正确，请检查 .workbench/settings.json") from exc

    def profiles(self, base, data=None):
        data = self.load() if data is None else data
        result = copy.deepcopy(base)
        for name in data["disabled_profiles"]:
            result.pop(name, None)
        for name, model in data["models"].items():
            result[name] = {**result.get(name, {}), **model}
        return result

    def public(self, base, data=None):
        data = self.load() if data is None else data
        profiles = self.profiles(base, data)
        return {"revision": data["revision"], "port": data["port"], "web_advisor": web_advisor_config(data.get("web_advisor")), "models": [
            {"id": name, "label": model.get("label") or name, "model": model.get("model", ""),
             "base_url": model.get("base_url") or "", "has_key": bool(model.get("api_key")) and not model.get("no_key", False),
             "no_key": model.get("no_key", False), "context_window": model.get("context_window"),
             "api_type": model.get("api_type", "chat_completions"),
             "endpoint_url": resolve_endpoint(model.get("base_url"), model.get("api_type", "chat_completions"))["endpoint_url"],
             "stream_usage": model.get("stream_usage"), "source": "settings" if name in data["models"] else "env"}
            for name, model in profiles.items()]}

    def prepare(self, payload, base):
        current = self.load()
        if not isinstance(payload, dict):
            raise ValueError("设置格式不正确")
        if payload.get("revision") != current["revision"]:
            raise SettingsChanged("设置已在其他窗口更新，请重新打开设置后再保存。")
        port = payload.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("端口必须是 1 到 65535 之间的整数")
        entries = payload.get("models")
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError("模型列表格式不正确，最多支持 50 个模型")
        previous = self.profiles(base, current)
        models = {}
        # Resolve the default profile first, so new models can inherit a Key
        # entered in the same save operation, rather than its previous value.
        entries = sorted(entries, key=lambda item: 0 if isinstance(item, dict) and str(item.get("id", "")).lower() == "default" else 1)
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("模型配置格式不正确")
            name = _text(entry.get("id"), "档案 ID", 48, required=True).lower()
            if not PROFILE_ID.fullmatch(name) or name in models:
                raise ValueError("档案 ID 只能使用字母、数字、短横线和下划线，且不能重复")
            model = _text(entry.get("model"), "模型 ID", 256, required=True)
            label = _text(entry.get("label", name), "显示名称", 100) or name
            url = _text(entry.get("base_url", ""), "模型 URL", 2048)
            if url:
                try:
                    parsed = urlsplit(url)
                    _ = parsed.port
                    valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                             and not any(char.isspace() for char in url))
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError("模型 URL 必须是有效的 http/https 地址，不能包含用户名、密码或查询参数")
            no_key = entry.get("no_key", False)
            old = previous.get(name, {})
            api_type = entry.get("api_type", old.get("api_type", "chat_completions"))
            resolve_endpoint(url, api_type)
            if type(no_key) is not bool:
                raise ValueError("无需 Key 选项格式不正确")
            key = _text(entry.get("api_key", ""), "API Key", 8192)
            if no_key:
                key = "not-needed"
            elif not key:
                fallback = models.get("default") or previous.get("default", {})
                key = (old.get("api_key") if not old.get("no_key") else None) or (
                    fallback.get("api_key") if name not in previous and not fallback.get("no_key") else None)
            if not key:
                raise ValueError(f"模型 {name} 需要 API Key；本地免鉴权服务请勾选“无需 Key”")
            window = entry.get("context_window")
            if window is not None and (type(window) is not int or not 1 <= window <= 9_999_999):
                raise ValueError("上下文窗口必须为空或 1 到 9999999 之间的整数")
            usage = entry.get("stream_usage")
            if usage is not None and type(usage) is not bool:
                raise ValueError("流式用量统计选项格式不正确")
            models[name] = {"label": label, "model": model, "base_url": url or None,
                            "api_key": key, "no_key": no_key, "context_window": window, "stream_usage": usage, "api_type": api_type}
        web_value = payload.get("web_advisor", current.get("web_advisor"))
        if isinstance(web_value, dict):
            # Older workbench clients must not reset browser/proxy fields they do not know.
            previous_web = current.get("web_advisor", {})
            incoming_web = web_value
            web_value = {**previous_web, **incoming_web}
            changed_browser = any(web_value.get(key) != previous_web.get(key)
                                  for key in ("browser_type", "cdp_endpoint", "proxy_mode", "proxy_url"))
            if changed_browser and "fingerprint_proxy_confirmed" not in incoming_web:
                web_value["fingerprint_proxy_confirmed"] = False
        return {"version": 1, "revision": uuid.uuid4().hex, "port": port, "models": models,
                "disabled_profiles": sorted(set(base) - set(models)),
                "web_advisor": web_advisor_config(web_value)}

    def save(self, data, expected_revision):
        if self.load()["revision"] != expected_revision:
            raise SettingsChanged("设置已更新，请重新打开设置后再保存。")
        self.path.parent.mkdir(exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
                json.dump(data, destination, ensure_ascii=False, indent=2)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def port(self):
        return self.load()["port"]
