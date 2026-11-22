"""附件（/file）能力验收测试。

覆盖上传文件给模型的三条通道与边界：
  1. 文本附件：原文完整注入提问
  2. 图片附件：转 base64 image_url 多模态 block
  3. PDF 文本模式：pypdf 提取
  4. PDF 原生模式：file 内容块（只有支持 file 块的端点能用）
  5. 关键区分：PDF 走 image_url 是非法请求，必须走 file 块
  6. 失败降级：文件不存在 / 超限 / 无 pypdf 都不崩
  7. 历史只留文件名，不重复存全文
  8. 端点拒绝 file 块时给出可操作提示

直接运行：.conda\\Scripts\\python.exe tests\\test_attachments.py
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


def make_files():
    t = HERE / "_a_note.md"
    t.write_text("# FRB 笔记\n- 源: FRB 20180916B\n- 能量: 1e40 erg\n", encoding="utf-8")
    png = HERE / "_a_pixel.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    pdf = HERE / "_a_doc.pdf"
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(pdf))
        c.drawString(80, 750, "FRB 20180916B energy 1e40 erg")
        c.drawString(80, 730, "Period 16.35 days")
        c.save()
    except ImportError:
        pdf = None
    return t, png, pdf


def cleanup(*paths):
    for p in paths:
        if p and Path(p).exists():
            Path(p).unlink()


def test_text():
    print("\n[1] 文本附件完整注入")
    t, _, _ = make_files()
    got = main._read_attachment(str(t))
    check(got["ok"] and got["kind"] == "text", "识别为文本")
    body = main._compose_question("讲了什么？", [str(t)])
    check(isinstance(body, str), "无图片时 content 是字符串")
    check("1e40 erg" in body, "原文完整进入提问")
    check("FRB 20180916B" in body, "关键编号未丢失")
    cleanup(t)


def test_image():
    print("\n[2] 图片转多模态 block")
    _, png, _ = make_files()
    got = main._read_attachment(str(png))
    check(got["ok"] and got["kind"] == "image", "识别为图片")
    check(got["data_url"].startswith("data:image/png;base64,"), "生成合法 data URL")
    c = main._compose_question("图里是什么？", [str(png)])
    check(isinstance(c, list) and c[0]["type"] == "text", "变成 block 列表，首块是文本")
    check(any(b["type"] == "image_url" for b in c), "含 image_url 块")
    cleanup(png)


def test_pdf_text_and_native():
    print("\n[3/4] PDF 两种模式")
    t, _, pdf = make_files()
    if pdf is None:
        print("  (跳过：reportlab 未安装，无法生成测试 PDF)")
        return

    # 文本模式（默认）
    os.environ.pop("PDF_NATIVE", None)
    got = main._read_attachment(str(pdf))
    check(got["ok"] and got["kind"] == "text", "默认走文本提取")
    check("1e40 erg" in got.get("text", ""), "提取到正文")
    body = main._compose_question("讲了什么？", [str(pdf)])
    check("===== 附件" in body, "以附件分隔块注入")

    # 原生模式
    os.environ["PDF_NATIVE"] = "1"
    got2 = main._read_attachment(str(pdf))
    check(got2["ok"] and got2["kind"] == "file", "PDF_NATIVE=1 时返回 file 块")
    blk = got2["block"]
    check(blk["mime_type"] == "application/pdf", "MIME 正确")
    check(blk["filename"] == pdf.name, "带文件名（OpenAI 要求）")
    c = main._compose_question("讲了什么？", [str(pdf)])
    check(any(b.get("type") == "file" for b in c), "content 含 file 块")

    # 关键：实际请求里 PDF 必须落在 file 字段，不能落在 image_url
    sent = OAIB._convert_message_to_dict(HumanMessage(content=c))
    blocks = sent["content"]
    file_blocks = [b for b in blocks if b.get("type") == "file"]
    check(len(file_blocks) == 1, "序列化后是 file 块")
    check(file_blocks[0]["file"]["file_data"].startswith("data:application/pdf;base64,"),
          "file_data 是 PDF 的 data URL")
    check(not any("application/pdf" in str(b.get("image_url", "")) for b in blocks),
          "PDF 没有被误塞进 image_url")

    os.environ.pop("PDF_NATIVE", None)
    cleanup(t, pdf)


def test_pdf_in_image_url_is_invalid():
    print("\n[5] 对照：PDF 塞进 image_url 是非法请求")
    pdf_b64 = base64.b64encode(b"%PDF-1.4\n").decode()
    m = HumanMessage(content=[{
        "type": "image_url",
        "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"},
    }])
    sent = OAIB._convert_message_to_dict(m)
    url = sent["content"][0]["image_url"]["url"]
    check("application/pdf" in url,
          "LangChain 会原样透传（不在本地拦截）",
          url[:60])
    check(url.startswith("data:application/pdf"),
          "所以只能在服务端被 400 —— 说明这条通道不能传 PDF")


def test_failures():
    print("\n[6] 失败降级")
    body = main._compose_question("q", ["no_such_file.txt"])
    check("文件不存在" in body, "缺失文件降级为提示")
    check("q" in body, "提问本体仍保留")

    pdf = HERE / "_a_big.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * (13 * 1024 * 1024))
    os.environ["PDF_NATIVE"] = "1"
    got = main._read_attachment(str(pdf))
    check(not got["ok"] and "过大" in got["note"], "超限 PDF 被拒绝")
    os.environ.pop("PDF_NATIVE", None)
    cleanup(pdf)


def test_history_light():
    print("\n[7] 历史只留文件名")
    note = main._history_note("这篇讲了什么？", ["a.pdf", "b.png"])
    check("a.pdf" in note and "b.png" in note, "记下文件名")
    check(len(note) < 200, "不含正文，长度可控", f"{len(note)} 字符")
    check(main._history_note("q", []) == "q", "无附件时原样返回")


def test_error_hint():
    print("\n[8] 端点拒绝 file 块时给可操作提示")
    exc = RuntimeError(
        "Error code: 400 - Invalid value: 'file'. "
        "Supported values are: 'text' and 'image_url'.")
    hinted = main.enrich_error(exc, ["x.pdf"])
    check("PDF_NATIVE" in hinted, "提示了怎么关掉原生模式")
    check("pypdf" in hinted, "提示了替代方案")
    check(main.enrich_error(ValueError("连接超时"), ["x.pdf"]) == "连接超时",
          "无关错误不被改写")


if __name__ == "__main__":
    print("=" * 66)
    print("附件上传能力验收")
    print("=" * 66)
    test_text()
    test_image()
    test_pdf_text_and_native()
    test_pdf_in_image_url_is_invalid()
    test_failures()
    test_history_light()
    test_error_hint()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
