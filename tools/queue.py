#!/usr/bin/env python3
"""Print only the local pending image paths Codex needs to read."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from healthlib.store import HealthStore


def main():
    parser = argparse.ArgumentParser(description="掃描待整理照片並建立 Codex 清單")
    parser.add_argument("--data-dir", default=str(ROOT / "private"))
    parser.add_argument("--no-scan", action="store_true")
    args = parser.parse_args()
    store = HealthStore(args.data_dir)
    try:
        if not args.no_scan:
            scan = store.scan()
        else:
            scan = None
        print(json.dumps({**store.queue(), "scan": scan}, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
