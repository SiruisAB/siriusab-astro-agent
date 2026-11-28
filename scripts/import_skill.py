"""Import an Agent Skill into this project, without enabling or executing it."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skill_store import MAX_ARCHIVE, SkillStore, import_official


def main():
    parser = argparse.ArgumentParser(description="导入项目技能；不会启用、执行脚本或安装依赖")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--official", metavar="NAME", help="从 anthropics/skills 导入指定技能")
    source.add_argument("--zip", type=Path, help="导入本地 ZIP")
    parser.add_argument("--name", help="多技能 ZIP 中要导入的技能名")
    args = parser.parse_args()
    store = SkillStore(ROOT)
    try:
        if args.official:
            print("Downloading official skill package; scripts will not execute...", flush=True)
            result = import_official(store, args.official)
        else:
            with args.zip.open("rb") as source:
                blob = source.read(MAX_ARCHIVE + 1)
            result = store.import_archive(blob, args.name)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"Import failed: {exc}\n")


if __name__ == "__main__":
    main()
