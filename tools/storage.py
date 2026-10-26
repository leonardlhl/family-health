"""Read-only local/cloud capacity totals. Never reads document contents or deletes files."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.publish import load_config
from tools.share import SupabaseShareClient, ShareError

COUNTERS = ("bucket_bytes", "bucket_files", "archive_bytes", "archive_files", "current_bytes", "current_files",
            "retained_versions", "unreferenced_bytes", "unreferenced_files", "unknown_size_files", "database_bytes")


def _tree_size(root, directory):
    total, count, partial = 0, 0, False
    pending = [directory]
    while pending:
        path = pending.pop()
        try:
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.resolve().is_relative_to(root):
                partial = True
                continue
            if not path.exists():
                continue
            if path.is_dir():
                pending.extend(path.iterdir())
            elif path.is_file():
                total += path.stat().st_size
                count += 1
        except OSError:
            partial = True
    return total, count, partial


def local_usage(data_dir):
    root = Path(data_dir).resolve()
    result = {"partial": False, "external_sources_included": False}
    for label, folders in (("source", ("inbox", "staging")), ("archive", ("archive",)),
                           ("backup", ("backups",)), ("preview", ("previews",))):
        measurements = [_tree_size(root, root / folder) for folder in folders]
        result[label + "_bytes"] = sum(value[0] for value in measurements)
        result[label + "_files"] = sum(value[1] for value in measurements)
        result["partial"] |= any(value[2] for value in measurements)
    database = [_tree_size(root, root / name) for name in ("archive.sqlite3", "archive.sqlite3-wal", "archive.sqlite3-shm")]
    result["database_bytes"] = sum(value[0] for value in database)
    result["partial"] |= any(value[2] for value in database)
    return result


def storage_status(data_dir, *, client=None, config=None):
    """GET /api/storage owner-only response. Unknown remote usage remains null."""
    cloud = {**dict.fromkeys(COUNTERS), "configured": False, "available": False, "measured_at": None,
             "quota_assumed": True, "file_quota_bytes": 1_000_000_000, "database_quota_bytes": 500_000_000,
             "warning_threshold": 0.8, "file_warning": None, "database_warning": None, "usage_incomplete": None}
    result = {"local": local_usage(data_dir), "cloud": cloud}
    try:
        settings = load_config(Path(data_dir)) if config is None else config
        if not settings.get("url") or not settings.get("service_key"):
            return result
        archive_id = settings.get("archive_id", "family")
        if archive_id != "family":
            raise ShareError("Storage counters are scoped to the family archive.")
        reader = client or SupabaseShareClient(settings["url"], settings["service_key"])
        cloud["configured"] = True
        measured = reader._request("/rest/v1/rpc/family_storage_usage", {"p_archive_id": archive_id})
        if not isinstance(measured, dict) or not isinstance(measured.get("measured_at"), str):
            raise ValueError()
        if any(isinstance(measured.get(key), bool) or not isinstance(measured.get(key), int)
               or measured[key] < 0 for key in COUNTERS):
            raise ValueError()
        cloud.update({key: measured[key] for key in COUNTERS})
        cloud.update(available=True, measured_at=measured["measured_at"],
                     usage_incomplete=measured["unknown_size_files"] > 0,
                     file_warning=measured["bucket_bytes"] >= cloud["file_quota_bytes"] * cloud["warning_threshold"],
                     database_warning=measured["database_bytes"] >= cloud["database_quota_bytes"] * cloud["warning_threshold"])
    except (ShareError, ValueError, TypeError, OSError, AttributeError):
        cloud["message"] = "Cloud usage could not be verified; no files were changed."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("HEALTH_DATA_DIR", str(ROOT / "private")))
    args = parser.parse_args()
    print(json.dumps(storage_status(args.data_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
