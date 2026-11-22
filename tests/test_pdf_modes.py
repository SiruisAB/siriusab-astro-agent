"""PDF 三种传输模式验收测试。

背景：中转站/纯转发端点往往既不支持 `file` 内容块，也不支持 `input_file`。
实测（chat01.ai）结论：
    /files            → 404（无文件存储）
    input_file        → 400（三种写法全失败）
    code_interpreter  → 声明被接受但无 code_interpreter_call（容器不存在）
    input_image       → 200，能读出图中文字 ✅

所以 PDF 想"直接给模型看"，只能本地渲染成页图走视觉通道。本测试固化三种模式：

    PDF_MODE=text   提文本（默认，最省 token）
    PDF_MODE=file   原生 file 块（仅官方端点）
    PDF_MODE=image  渲染成 PNG 走 image_url（通用，能读扫描件/表格）

直接运行：.conda\\Scripts\\python.exe tests\\test_pdf_modes.py
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("API_KEY", "dummy")

import main  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai.chat_models import base as OAIB  # noqa: E402

HERE = Path(__file__).resolve().parent
PASS, FAIL = [], []


def check(cond, name, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


def make_pdf() -> Path | None:
    p = HERE / "_p_doc.pdf"
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(p))
        c.drawString(80, 750, "TOKEN: ABC789")
        c.drawString(80, 720, "energy 1e40 erg")
        c.showPage()                      # 换页，最后统一 save 一次
        c.drawString(80, 750, "PAGE 2 TOKEN: XYZ999")
        c.save()
    except ImportError:
        return None
    return p


def set_mode(mode):
    os.environ.pop("PDF_NATIVE", None)
    if mode is None:
        os.environ.pop("PDF_MODE", None)
    else:
        os.environ["PDF_MODE"] = mode


def test_mode_selection():
    print("\n[1] 模式解析优先级")
    set_mode(None)
    check(main._pdf_mode() == "text", "默认 text")
    set_mode("image")
    check(main._pdf_mode() == "image", "PDF_MODE=image")
    set_mode("file")
    check(main._pdf_mode() == "file", "PDF_MODE=file")
    set_mode("native")
    check(main._pdf_mode() == "file", "别名 native → file")
    set_mode(None)
    os.environ["PDF_NATIVE"] = "1"
    check(main._pdf_mode() == "file", "旧开关 PDF_NATIVE=1 → file")
    os.environ.pop("PDF_NATIVE")
    os.environ["PDF_MODE_GLM"] = "image"
    check(main._pdf_mode("glm") == "image", "按档案覆盖 PDF_MODE_GLM")
    check(main._pdf_mode("default") == "text", "其他档案不受影响")
    os.environ.pop("PDF_MODE_GLM")
    set_mode(None)


def test_text_mode():
    print("\n[2] text 模式：提取文本")
    pdf = make_pdf()
    if pdf is None:
        print("  (跳过：reportlab 未安装)")
        return
    set_mode("text")
    r = main._read_attachment(str(pdf))
    check(r["ok"] and r["kind"] == "text", "返回 text")
    check("ABC789" in r["text"], "正文可提取")
    body = main._compose_question("讲了什么", [str(pdf)])
    check(isinstance(body, str), "content 是字符串（无图片）")
    check("ABC789" in body, "文本进入提问")
    pdf.unlink()


def test_image_mode():
    print("\n[3] image 模式：渲染页图")
    pdf = make_pdf()
    if pdf is None:
        print("  (跳过：reportlab 未安装)")
        return
    set_mode("image")
    r = main._read_attachment(str(pdf))
    check(r["ok"] and r["kind"] == "pdfimages", "返回 pdfimages")
    check(len(r["images"]) == 2, f"两页都渲染（实际 {len(r['images'])}）")
    check(r["images"][0]["data_url"].startswith("data:image/png;base64,"), "data URL 合法")

    c = main._compose_question("这份 PDF 讲了什么", [str(pdf)])
    check(isinstance(c, list), "content 变成 block 列表")
    check(c[0]["type"] == "text", "首块是文本")
    check(sum(1 for b in c if b["type"] == "image_url") == 2, "两张页图")

    # 关键：实际发出的请求体必须是 image_url，不能是 file/input_file
    sent = OAIB._convert_message_to_dict(HumanMessage(content=c))
    kinds = [b.get("type") for b in sent["content"]]
    check("image_url" in kinds, "序列化后含 image_url")
    check("file" not in kinds, "不含 file 块（中转站不支持）")
    pdf.unlink()


def test_page_and_size_limits():
    print("\n[4] 页数上限与尺寸控制")
    pdf = make_pdf()
    if pdf is None:
        print("  (跳过)")
        return
    old = main._PDF_IMAGE_MAX_PAGES
    main._PDF_IMAGE_MAX_PAGES = 1
    set_mode("image")
    r = main._read_attachment(str(pdf))
    check(len(r["images"]) == 1, "超页数时被截到上限")
    check("渲染 1 页" in r["note"], "note 说明只渲染了部分", r["note"])
    main._PDF_IMAGE_MAX_PAGES = old
    pdf.unlink()


def test_missing_dependency_hint():
    print("\n[5] 缺 pypdfium2 时给出安装提示")
    pdf = make_pdf()
    if pdf is None:
        return
    set_mode("image")
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "pypdfium2":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    builtins.__import__ = fake_import
    try:
        r = main._read_attachment(str(pdf))
        check(not r["ok"] and "pypdfium2" in r["note"], "提示装 pypdfium2", r["note"][:60])
    finally:
        builtins.__import__ = real_import
    pdf.unlink()


if __name__ == "__main__":
    print("=" * 66)
    print("PDF 传输模式验收")
    print("=" * 66)
    test_mode_selection()
    test_text_mode()
    test_image_mode()
    test_page_and_size_limits()
    test_missing_dependency_hint()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
