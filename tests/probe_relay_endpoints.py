"""探测中转站暴露了哪些 OpenAI 端点（最少请求数）。

目的：判断 chat01.ai 转发层是否带「文件存储」和「Responses/容器」能力。

用最省的方式探测：
  - GET  /models    → 基线：确认转发层通不通
  - POST /files     → 文件存储端点是否存在（上传 12 字节的 txt，几乎零成本）
  - GET  /files     → 文件列表端点
  - POST /responses → Responses API 是否存在（1 token 级请求）

判读规则（关键）：
  404 / 501           → 该端点不存在，转发层没代理
  401 / 403           → 端点存在（只是鉴权口径不同）
  400 / 422           → 端点存在（参数不对）
  405                 → 端点存在（方法不对）
  200                 → 端点存在且可用

用法：
    .conda\\Scripts\\python.exe tests\\probe_relay_endpoints.py
    .conda\\Scripts\\python.exe tests\\probe_relay_endpoints.py --no-responses   # 跳过会产生 token 的那一项
"""

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_openai_file_input import _load_key, _load_base_url  # noqa: E402


def normalize(base: str) -> str:
    """把误写成 .../chat/completions 的 base_url 还原成 .../v1。"""
    b = (base or "").rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if b.endswith(suffix):
            b = b[: -len(suffix)]
    return b


def verdict(code: int) -> str:
    if code == 200:
        return "✅ 存在且可用"
    if code in (401, 403):
        return "✅ 存在（鉴权口径不同）"
    if code in (400, 422):
        return "✅ 存在（参数不符）"
    if code == 405:
        return "✅ 存在（方法不允许）"
    if code in (404, 501):
        return "❌ 不存在"
    if code in (429,):
        return "⚠️ 存在但被限流"
    return f"❓ 未预期（{code}）"


def main():
    key = _load_key()
    base_raw = _load_base_url()
    if not key or not base_raw:
        sys.exit("缺 key 或 base_url，请检查 .env")
    base = normalize(base_raw)
    print(f"探测目标: {base}")
    print(f"key     : {key[:6]}…{key[-4:]}")
    print("=" * 66)

    h = {"Authorization": f"Bearer {key}"}
    timeout = httpx.Timeout(30.0)
    results = {}

    with httpx.Client(timeout=timeout, headers=h, follow_redirects=False) as c:
        # 1) 基线
        try:
            r = c.get(f"{base}/models")
            results["GET /models"] = (r.status_code, r.text[:160])
        except Exception as e:
            results["GET /models"] = (0, f"{type(e).__name__}: {e}")

        # 2) 文件存储端点（上传 12 字节）
        try:
            r = c.post(
                f"{base}/files",
                files={"file": ("probe.txt", b"hello probe\n", "text/plain")},
                data={"purpose": "assistants"},
            )
            body = r.text[:200]
            file_id = ""
            if r.status_code == 200:
                try:
                    file_id = r.json().get("id", "")
                except Exception:
                    pass
            results["POST /files"] = (r.status_code, body)
            results["_file_id"] = (r.status_code, file_id)
        except Exception as e:
            results["POST /files"] = (0, f"{type(e).__name__}: {e}")

        # 3) 文件列表
        try:
            r = c.get(f"{base}/files")
            results["GET /files"] = (r.status_code, r.text[:160])
        except Exception as e:
            results["GET /files"] = (0, f"{type(e).__name__}: {e}")

        # 4) Responses API（会产生少量 token，可跳过）
        if "--no-responses" not in sys.argv:
            try:
                r = c.post(
                    f"{base}/responses",
                    json={"model": os.environ.get("PROBE_MODEL", "gpt-4o-mini"),
                          "input": "hi"},
                )
                results["POST /responses"] = (r.status_code, r.text[:200])
            except Exception as e:
                results["POST /responses"] = (0, f"{type(e).__name__}: {e}")
        else:
            results["POST /responses"] = (-1, "已跳过")

    print()
    for name, (code, body) in results.items():
        if name.startswith("_"):
            continue
        if code == -1:
            print(f"{name:18} 跳过")
            continue
        if code == 0:
            print(f"{name:18} 连接失败: {body[:100]}")
            continue
        print(f"{name:18} [{code}] {verdict(code)}")
        if body:
            print(f"{'':20} {body[:150]}".replace("\n", " "))

    print()
    print("=" * 66)
    fid = results.get("_file_id", (0, ""))[1]
    files_ok = results.get("POST /files", (0, ""))[0] == 200
    resp_ok = results.get("POST /responses", (0, ""))[0] == 200

    if files_ok and fid:
        print(f"文件存储可用，file_id={fid} → 可尝试 code_interpreter 引用")
    elif files_ok:
        print("POST /files 返回 200 但没解析出 id，需人工看响应体")
    else:
        print("文件存储端点不可用 → 中转层只代理对话，没有文件层")
    print(f"Responses API: {'可用' if resp_ok else '不可用 → code_interpreter 无从谈起'}")
    print("=" * 66)


if __name__ == "__main__":
    main()
