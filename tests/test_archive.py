"""压缩包上传能力验收测试。

zip/tar 无法直接喂给任何 LLM API，必须本地解包后按类型分发。这里固化：
  1. 文本文件逐个注入，二进制跳过
  2. 包内图片转多模态 block
  3. 噪声目录（.git/__pycache__ 等）跳过
  4. zip slip（../ 或绝对路径）拒绝
  5. zip bomb（声明解压量超上限）在解压前拦下
  6. 超大单文件截断
  7. 嵌套压缩包不递归（原因标注正确）
  8. 清单（manifest）让模型知道包里有什么
  9. tar / tar.gz 同样支持
 10. 内容预算：注入总量不超过上限

直接运行：.conda\\Scripts\\python.exe tests\\test_archive.py
"""

import base64
import io
import os
import sys
import tarfile
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


def build_mixed_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.md", "# 数据集\n- FRB 20180916B\n- 能量 1e40 erg\n")
        z.writestr("data/spec.csv", "name,value\nenergy,1e40\n")
        z.writestr("figs/plot.png", PNG)
        z.writestr("bin/model.pkl", b"\x00\x01\x02binary\x00data")
        z.writestr(".git/config", "[core]\n")
        z.writestr("__pycache__/x.pyc", b"\x00\x00")
        z.writestr("../evil.txt", "zip slip")
        z.writestr("nested.tar.gz", b"\x1f\x8b\x08\x00fake")
    return path


def cleanup(*paths):
    for p in paths:
        if p and Path(p).exists():
            Path(p).unlink()


def test_dispatch():
    print("\n[1-3] 解包分发：文本注入 / 图片转块 / 噪声跳过")
    z = build_mixed_zip(HERE / "_z_mixed.zip")
    r = main._read_attachment(str(z))
    check(r["ok"] and r["kind"] == "archive", "识别为 archive")
    names = [t["name"] for t in r["texts"]]
    check("readme.md" in names and "data/spec.csv" in names, "文本文件被注入", str(names))
    check(len(r["images"]) == 1, "包内图片转成多模态块")
    check(r["images"][0]["data_url"].startswith("data:image/png;base64,"), "图片 data URL 合法")
    check(all(n not in names for n in [".git/config", "__pycache__/x.pyc"]), "噪声目录被跳过")
    check("bin/model.pkl" not in names, "二进制文件被跳过")
    cleanup(z)


def test_zip_slip():
    print("\n[4] zip slip 拒绝")
    z = build_mixed_zip(HERE / "_z_slip.zip")
    r = main._read_attachment(str(z))
    names = [t["name"] for t in r["texts"]]
    check("../evil.txt" not in names, "相对越界路径未注入")
    check("路径越界" in r["manifest"], "清单里标注了越界原因")
    cleanup(z)


def test_zip_bomb():
    print("\n[5] zip bomb 解压前拦截")
    p = HERE / "_z_bomb.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\x00" * (300 * 1024 * 1024))
    size_on_disk = p.stat().st_size
    r = main._read_attachment(str(p))
    check(not r["ok"], "被拒绝")
    check("zip bomb" in r["note"], "明确指出疑似 zip bomb", r["note"][:60])
    check(size_on_disk < 1024 * 1024, f"压缩包本体很小({size_on_disk}B)却被拦下，说明是预检")
    cleanup(p)


def test_truncation_and_nested():
    print("\n[6-7] 超长文件截断 / 嵌套压缩包不递归")
    p = HERE / "_z_big.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("huge.txt", "A" * 50_000)
        z.writestr("inner.zip", b"PK\x03\x04fake")
    r = main._read_attachment(str(p))
    huge = [t for t in r["texts"] if t["name"] == "huge.txt"][0]
    check("已截断" in huge["text"], "超长文件被截断并注明")
    check(len(huge["text"]) <= main._ARCHIVE_MAX_FILE_CHARS + 40,
          f"截断到上限附近（{len(huge['text'])}）")
    check(all(t["name"] != "inner.zip" for t in r["texts"]), "嵌套压缩包未递归")
    check("嵌套压缩包" in r["manifest"], "清单标注嵌套原因")
    cleanup(p)


def test_manifest_and_tar():
    print("\n[8-9] 清单可读 / tar.gz 支持")
    z = build_mixed_zip(HERE / "_z_man.zip")
    r = main._read_attachment(str(z))
    check("压缩包 _z_man.zip" in r["manifest"], "清单含包名")
    check("✓" in r["manifest"] and "✗" in r["manifest"], "清单区分注入/跳过")
    check("1e40" in (r["texts"][0]["text"] + "".join(t["text"] for t in r["texts"])),
          "正文内容在（用于核对模型能否读到）")
    cleanup(z)

    tgz = HERE / "_t_notes.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        data = "# tar note\nenergy 1e40 erg\n".encode("utf-8")
        ti = tarfile.TarInfo("notes.md")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    r2 = main._read_attachment(str(tgz))
    check(r2["ok"] and r2["kind"] == "archive", "tar.gz 被识别")
    check("1e40" in r2["texts"][0]["text"], "tar 内文本被读取")
    cleanup(tgz)


def test_budget_and_compose():
    print("\n[10] 组装与预算")
    p = HERE / "_z_budget.zip"
    with zipfile.ZipFile(p, "w") as z:
        for i in range(30):
            z.writestr(f"f{i:02d}.txt", "X" * 10_000)
    r = main._read_attachment(str(p))
    check(len(r["texts"]) <= main._ARCHIVE_MAX_FILES, f"文件数受限于 {main._ARCHIVE_MAX_FILES}")

    c = main._compose_question("包里有什么？", [str(p)])
    text = c[0]["text"] if isinstance(c, list) else c
    check(len(text) <= main._ATTACH_TOTAL_CHARS + 3000,
          f"注入总量不超预算（{len(text)} 字符）")
    check("压缩包 _z_budget.zip" in text, "清单在组装结果里")
    check("包里有什么？" in text, "用户问题未被吞掉")
    cleanup(p)


if __name__ == "__main__":
    print("=" * 66)
    print("压缩包上传能力验收")
    print("=" * 66)
    test_dispatch()
    test_zip_slip()
    test_zip_bomb()
    test_truncation_and_nested()
    test_manifest_and_tar()
    test_budget_and_compose()
    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
