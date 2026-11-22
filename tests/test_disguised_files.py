"""文件改名（扩展名与内容不符）容错测试。

场景：zip 被改名成 .txt 上传是很常见的操作（打包工具、邮件网关、上传白名单）。
按扩展名判断会读到乱码，必须看文件头魔法字节。

但要说清楚：**改名只影响"本地能否识别"**，不影响"模型能否解压"。
端点没有执行环境时，改名/换 MIME 都换不来解压能力。

覆盖：
  1. zip → .txt   仍能解包并注入内容
  2. pdf → .txt   仍能提取文本
  3. png → .txt   仍走视觉通道，MIME 正确
  4. 改名时给出说明（不静默）
  5. 普通文本文件不受影响（不能误判）
  6. 空文件 / 随机二进制不崩溃

直接运行：.conda\\Scripts\\python.exe tests\\test_disguised_files.py
"""

import base64
import io
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("API_KEY", "dummy")

import main  # noqa: E402

HERE = Path(__file__).resolve().parent
PASS, FAIL = [], []
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def check(cond, name, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


def make_zip_named(suffix: str) -> Path:
    p = HERE / f"_d_zip{suffix}"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("spec.csv", "secret_token,QWERTY123\n")
    return p


def make_pdf_named(suffix: str) -> Path | None:
    p = HERE / f"_d_doc{suffix}"
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(p))
        c.drawString(80, 750, "TOKEN: ABC789")
        c.save()
    except ImportError:
        return None
    return p


def make_png_named(suffix: str) -> Path:
    p = HERE / f"_d_img{suffix}"
    p.write_bytes(PNG)
    return p


def cleanup(*paths):
    for p in paths:
        if p and Path(p).exists():
            Path(p).unlink()


def test_zip_renamed():
    print("\n[1] zip 改名成 .txt 仍能解包")
    real = make_zip_named(".zip")
    fake = make_zip_named(".txt")
    r1 = main._read_attachment(str(real))
    r2 = main._read_attachment(str(fake))
    check(r2["ok"] and r2["kind"] == "archive", "改名后仍识别为 archive")
    check("QWERTY123" in str(r2["texts"]), "内容被正确注入")
    check([t["name"] for t in r1["texts"]] == [t["name"] for t in r2["texts"]],
          "与未改名版本结果一致")
    cleanup(real, fake)


def test_pdf_renamed():
    print("\n[2] pdf 改名成 .txt 仍能提取")
    fake = make_pdf_named(".txt")
    if fake is None:
        print("  (跳过：reportlab 未安装)")
        return
    r = main._read_attachment(str(fake))
    check(r["ok"] and r["kind"] == "text", "识别为 PDF 并提取文本")
    check("ABC789" in r.get("text", ""), "正文完整")
    check("PDF" in r["note"], "note 注明是 PDF")
    cleanup(fake)


def test_png_renamed():
    print("\n[3] png 改名成 .txt 仍走视觉通道")
    fake = make_png_named(".txt")
    r = main._read_attachment(str(fake))
    check(r["ok"] and r["kind"] == "image", "识别为 image")
    check(r["data_url"].startswith("data:image/png;base64,"), "MIME 按内容判定而非扩展名")
    cleanup(fake)


def test_note_discloses():
    print("\n[4] 改名时明确告知，不静默处理")
    fake = make_zip_named(".txt")
    r = main._read_attachment(str(fake))
    check("与内容不符" in r["note"], "note 说明扩展名不匹配", r["note"][:70])
    cleanup(fake)


def test_plain_text_not_misjudged():
    print("\n[5] 普通文本不被误判")
    p = HERE / "_d_plain.txt"
    p.write_text("# 正常笔记\nenergy 1e40 erg\n", encoding="utf-8")
    r = main._read_attachment(str(p))
    check(r["ok"] and r["kind"] == "text", "识别为 text")
    check("与内容不符" not in r["note"], "没有误报扩展名不符")
    check("1e40" in r["text"], "内容正确")
    cleanup(p)


def test_edge_cases():
    print("\n[6] 空文件 / 随机二进制不崩溃")
    empty = HERE / "_d_empty.txt"
    empty.write_bytes(b"")
    r = main._read_attachment(str(empty))
    check(r["ok"] and r["kind"] == "text", "空文件当文本处理")

    blob = HERE / "_d_blob.bin"
    blob.write_bytes(bytes(range(256)) * 4)
    r2 = main._read_attachment(str(blob))
    check(isinstance(r2, dict), "随机二进制不抛异常")
    check(r2["ok"], "作为文本容错读入（errors=replace）")
    cleanup(empty, blob)


if __name__ == "__main__":
    print("=" * 66)
    print("文件改名容错验收")
    print("=" * 66)
    test_zip_renamed()
    test_pdf_renamed()
    test_png_renamed()
    test_note_discloses()
    test_plain_text_not_misjudged()
    test_edge_cases()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
