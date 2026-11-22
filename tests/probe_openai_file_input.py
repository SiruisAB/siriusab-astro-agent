"""OpenAI 文件输入边界探针 —— 用真实 key 一次性测清 zip 能不能传、怎么传。

争论「能不能直接传 zip」没有意义，这里把四种通道各打一次，看服务端真实返回。

用法（推荐：写进项目 .env，不必粘贴到对话里）：
    # 在 astro-agent/.env 里加一行
    OPENAI_API_KEY=sk-...

    .conda\\Scripts\\python.exe tests\\probe_openai_file_input.py

也可用真实环境变量覆盖。可选：
    PROBE_MODEL=gpt-5        # 默认 gpt-4o-mini
    PROBE_BASE_URL=...       # 用中转站/兼容端点时填

key 只用于本次调用，永不打印。

判读：
    [200] = 服务端接受
    [400] = 该通道不接受这种文件（正是要确认的边界）
    [404/403] = key 或权限问题，与文件格式无关

注意：本脚本会真实消耗 token 并上传测试文件（约 1KB）。
"""

import base64
import io
import os
import sys
import zipfile
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺 openai SDK：.conda\\Scripts\\python.exe -m pip install openai")


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.md", "# 探针数据\n- energy 1e40 erg\n- period 16.35 d\n")
        z.writestr("spec.csv", "name,value\nenergy,1e40\n")
    return buf.getvalue()


def show(tag, fn):
    try:
        r = fn()
        print(f"  [{tag}] OK")
        return r
    except Exception as e:
        msg = str(e).replace("\n", " ")[:220]
        print(f"  [{tag}] {type(e).__name__}: {msg}")
        return None


def _load_key() -> str | None:
    """优先读真实环境变量，其次读项目 .env —— 避免 key 出现在对话或命令行历史里。"""
    # 优先级：专用变量 → 项目主档案 API_KEY（本项目的约定）
    for var in ("OPENAI_API_KEY", "PROBE_API_KEY"):
        k = os.environ.get(var)
        if k:
            return k.strip()
    for name in (".env", ".env.example"):
        path = Path(__file__).resolve().parent.parent / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() not in ("OPENAI_API_KEY", "PROBE_API_KEY", "API_KEY"):
                continue
            v = val.strip().strip('"').strip("'")
            if v and "你的" not in v and v.lower() not in ("sk-xxx", "sk-x"):
                return v
    return None


def _load_base_url() -> str | None:
    """项目 .env 里的 BASE_URL（未注释时）或 PROBE_BASE_URL。"""
    for var in ("PROBE_BASE_URL", "BASE_URL"):
        v = os.environ.get(var)
        if v:
            return v.strip()
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("BASE_URL=") :
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def single_request(client, model: str, zbytes: bytes) -> None:
    """只发一次请求：Responses API + code_interpreter，zip 用 base64 内联。

    这是对「能不能把 zip 交给模型自己解」最直接的单次检验：
    成功 → 平台接受 zip 并在容器里解开（回答里会出现 1e40）
    失败 → 就该通道不接受 zip，返回体里的错误会说明原因
    """
    b64 = base64.b64encode(zbytes).decode()
    print("[单次请求] Responses API + code_interpreter，zip 内联")
    try:
        r = client.responses.create(
            model=model,
            tools=[{"type": "code_interpreter",
                    "container": {"type": "auto"}}],
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "filename": "probe.zip",
                     "file_data": f"data:application/zip;base64,{b64}"},
                    {"type": "input_text",
                     "text": "解压这个 zip，读出 spec.csv 里 energy 的值，只回答那个数值。"},
                ],
            }],
        )
        txt = (getattr(r, "output_text", "") or "").strip()
        print(f"  [200] 模型回答: {txt[:200]}")
        print(f"  读到 1e40: {'是 → 容器确实解开了 zip' if '1e40' in txt else '否 → 没读到预期内容'}")
    except Exception as e:
        print(f"  [{type(e).__name__}] {str(e).replace(chr(10),' ')[:300]}")


def main():
    key = _load_key()
    if not key:
        sys.exit(
            "未找到 openai key。请在项目 .env 里加一行：\n"
            "  OPENAI_API_KEY=sk-...\n"
            "（或设置同名环境变量）"
        )
    base = _load_base_url()
    model = os.environ.get("PROBE_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=key, base_url=base)
    zbytes = make_zip()
    print(f"模型: {model} | base_url: {base or 'OpenAI 官方'} | key: {key[:6]}…{key[-4:]}")
    print(f"测试 zip: {len(zbytes)} 字节\n")

    if "--single" in sys.argv:
        single_request(client, model, zbytes)
        return

    # ---------- 1. Files API 上传 zip ----------
    print("[1] Files API 上传 zip（purpose=assistants）")
    up = show("upload", lambda: client.files.create(
        file=("probe.zip", zbytes), purpose="assistants"))
    file_id = getattr(up, "id", None)
    if file_id:
        print(f"      file_id = {file_id}")

    # ---------- 2. Chat Completions 的 file 块（预期失败）----------
    print("\n[2] Chat Completions 内联 zip 作为 file 块")
    show("chat.file", lambda: client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "file", "file": {
                "file_data": "data:application/zip;base64,"
                             + base64.b64encode(zbytes).decode(),
                "filename": "probe.zip"}},
            {"type": "text", "text": "列出这个压缩包里的文件"},
        ]}],
    ))

    # ---------- 3. Responses API input_file（base64 内联）----------
    print("\n[3] Responses API input_file（base64 内联 zip）")
    show("resp.inline", lambda: client.responses.create(
        model=model,
        input=[{"role": "user", "content": [
            {"type": "input_file",
             "filename": "probe.zip",
             "file_data": "data:application/zip;base64,"
                          + base64.b64encode(zbytes).decode()},
            {"type": "input_text", "text": "列出这个压缩包里的文件"},
        ]}],
    ))

    # ---------- 4. Responses API + code_interpreter 容器 ----------
    print("\n[4] Responses API + code_interpreter（容器挂载 zip）")
    if not file_id:
        print("  跳过：上一步没拿到 file_id")
    else:
        r4 = show("resp.ci", lambda: client.responses.create(
            model=model,
            tools=[{"type": "code_interpreter",
                    "container": {"type": "auto", "file_ids": [file_id]}}],
            input="解开这个 zip，列出里面的文件，并读出 spec.csv 的 energy 值",
        ))
        if r4 is not None:
            txt = getattr(r4, "output_text", "") or ""
            print(f"     模型回答: {txt[:300]}")
            hit = "1e40" in txt
            print(f"     是否读到 energy=1e40: {'是 ← 说明容器真的解开了' if hit else '否'}")

    print("\n" + "=" * 60)
    print("判读：若 [1]+[4] 成功而 [2] 失败 → zip 只能走『上传+容器』，")
    print("      不能作为 chat 消息内容块直接内联。")
    print("=" * 60)


if __name__ == "__main__":
    main()
