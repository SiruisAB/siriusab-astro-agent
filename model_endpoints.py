"""Resolve common OpenAI-compatible API endpoints without network probing."""

from urllib.parse import urlsplit, urlunsplit


API_TYPES = {"chat_completions", "responses", "auto"}
API_PATHS = {"chat_completions": "/chat/completions", "responses": "/responses"}


def resolve_endpoint(base_url=None, api_type="chat_completions"):
    if not isinstance(api_type, str) or api_type not in API_TYPES:
        raise ValueError("接口类型请选择 Chat Completions、Responses 或自动识别")
    if base_url is not None and not isinstance(base_url, str):
        raise ValueError("模型 URL 格式不正确")
    raw = (base_url or "").strip()
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    detected = next((name for name, suffix in API_PATHS.items() if path.lower().endswith(suffix)), None)
    if detected:
        if api_type != "auto" and api_type != detected:
            raise ValueError("完整 URL 与所选接口类型不一致，请切换接口类型或使用基础 URL")
        path = path[:-len(API_PATHS[detected])]
    resolved = detected if api_type == "auto" and detected else ("chat_completions" if api_type == "auto" else api_type)
    normalized = urlunsplit(parsed._replace(path=path)) if raw else None
    # 在 path 上拼接接口后缀，而不是在整串 URL 末尾拼接：
    # 否则 http://h/v1?key=abc 会变成 http://h/v1?key=abc/chat/completions。
    if raw:
        endpoint = urlunsplit(parsed._replace(path=f"{path}{API_PATHS[resolved]}"))
    else:
        endpoint = "https://api.openai.com/v1" + API_PATHS[resolved]
    return {"api_type": resolved, "base_url": normalized, "endpoint_url": endpoint}


def model_identity(profile):
    endpoint = resolve_endpoint(profile.get("base_url"), profile.get("api_type", "chat_completions"))
    return {"model": profile.get("model"), "base_url": endpoint["base_url"], "api_type": endpoint["api_type"]}
