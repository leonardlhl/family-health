#!/usr/bin/env python3
"""Import Codex-transcribed records as drafts; never auto-confirm medication text."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from healthlib.store import HealthStore, ValidationError


def main():
    parser = argparse.ArgumentParser(description="匯入 Codex 整理的醫療紀錄草稿")
    parser.add_argument("file", help="JSON 檔案；單一紀錄、紀錄陣列或 {visits: [...]} ")
    parser.add_argument("--data-dir", default=str(ROOT / "private"))
    args = parser.parse_args()
    store = HealthStore(args.data_dir)
    try:
        value = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
        records = value.get("visits", [value]) if isinstance(value, dict) else value
        if not isinstance(records, list) or len(records) > 100:
            raise ValidationError("請提供不多於 100 筆求診紀錄")
        # Validate the whole batch first so an invalid final record cannot yield
        # a misleading partially imported batch.
        for record in records:
            store._validated_visit(record)
        imported = [store.upsert_visit(record) for record in records]
        print(json.dumps({"imported": len(imported), "ids": [r["id"] for r in imported], "status": "draft"}, ensure_ascii=False))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
