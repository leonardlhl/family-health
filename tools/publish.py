"""Publish a confirmed, anonymized local snapshot. Credentials never enter static output."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ALIASES = {"grandpa": "爺爺", "grandma": "嬷嬷"}
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,96}$")


class PublishError(ValueError):
    pass


def _text(value, max_length=20000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise PublishError("Snapshot has invalid text fields.")
    return value


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise PublishError("Snapshot has an invalid identifier.")
    return value


def _date(value, optional=False):
    if not value and optional:
        return None
    if not isinstance(value, str):
        raise PublishError("Snapshot has an invalid date.")
    try:
        from datetime import date
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise PublishError("Snapshot has an invalid date.") from None
    return value


def prepare_snapshot(raw, archive_id, version_id):
    """A whitelist boundary: identity fields, raw OCR and local paths cannot pass through."""
    if raw.get("schema_version") != 1:
        raise PublishError("Unsupported archive version.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", archive_id):
        raise PublishError("Invalid archive ID.")
    uuid.UUID(str(version_id))
    patients = []
    for patient in raw.get("patients", []):
        patient_id = patient.get("id")
        if patient_id not in ALIASES:
            raise PublishError("Only grandpa and grandma aliases are allowed.")
        age = patient.get("age")
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 125):
            raise PublishError("Age must be an integer from 0 to 125.")
        sex = patient.get("sex", "unknown")
        if sex not in {"unknown", "male", "female"}:
            raise PublishError("Invalid sex value.")
        patients.append({"id": patient_id, "name": ALIASES[patient_id], "age": age, "sex": sex})
    if len({p["id"] for p in patients}) != len(patients):
        raise PublishError("Duplicate patient alias.")
    patient_ids = {p["id"] for p in patients}
    files = {a["id"]: a for a in raw.get("attachments", [])}
    if len(files) != len(raw.get("attachments", [])):
        raise PublishError("Duplicate attachment ID.")
    linked = set()
    visits = []
    for item in raw.get("visits", []):
        if item.get("status") != "confirmed" or not item.get("reviewed_at") or item.get("privacy_reviewed") is not True:
            raise PublishError("Only confirmed and reviewed records can be published.")
        patient_id = item.get("patient_id")
        if patient_id not in patient_ids:
            raise PublishError("Visit belongs to an unknown patient alias.")
        visit = {"id": _id(item.get("id")), "patient_id": patient_id, "date": _date(item.get("date")), "status": "confirmed", "privacy_reviewed": True}
        for field in ("doctor", "clinic", "reason", "diagnosis", "notes", "created_at", "updated_at", "reviewed_at"):
            visit[field] = _text(item.get(field, ""))
        if not visit["doctor"] and not visit["clinic"]:
            raise PublishError("Confirmed records need a doctor or clinic.")

        def check_sources(source_ids):
            if not isinstance(source_ids, list) or len(source_ids) > 500:
                raise PublishError("Invalid source links.")
            checked = []
            for file_id in source_ids:
                _id(file_id)
                file = files.get(file_id)
                if not file or file.get("privacy_checked") is not True or file.get("patient_id") != patient_id:
                    raise PublishError("A source image is missing, belongs to another person, or needs privacy review.")
                linked.add(file_id)
                checked.append(file_id)
            return list(dict.fromkeys(checked))

        visit["source_file_ids"] = check_sources(item.get("source_file_ids", []))
        visit["medicines"] = []
        for med in item.get("medicines", []):
            medicine = {"id": _id(med.get("id")), "status": med.get("status", "unknown")}
            if medicine["status"] not in {"unknown", "current", "stopped"}:
                raise PublishError("Invalid medication status.")
            for field in ("name", "strength", "dose", "frequency", "instructions"):
                medicine[field] = _text(med.get(field, ""))
            medicine["source_file_ids"] = check_sources(med.get("source_file_ids", []))
            visit["medicines"].append(medicine)
        appointment = item.get("appointment")
        visit["appointment"] = None
        if appointment:
            time = _text(appointment.get("time", ""), 5)
            if time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
                raise PublishError("Invalid appointment time.")
            status = appointment.get("status", "scheduled")
            if status not in {"scheduled", "completed", "cancelled"}:
                raise PublishError("Invalid appointment status.")
            visit["appointment"] = {"date": _date(appointment.get("date")), "time": time,
                                    "location": _text(appointment.get("location", "")),
                                    "status": status, "notes": _text(appointment.get("notes", ""))}
        visits.append(visit)
    if len({v["id"] for v in visits}) != len(visits):
        raise PublishError("Duplicate visit ID.")
    attachments = []
    for file_id in sorted(linked):
        file = files[file_id]
        mime = file.get("mime")
        if mime not in MIME_EXT:
            raise PublishError("Only sanitized PNG, JPEG and WebP images may be shared.")
        digest = file.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PublishError("Invalid source checksum.")
        size = file.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 25 * 1024 * 1024:
            raise PublishError("Invalid source size.")
        attachments.append({"id": file_id, "patient_id": file["patient_id"],
                            "archive_name": _text(file.get("archive_name", "相片"), 255),
                            "mime": mime, "size": size, "sha256": digest,
                            "privacy_checked": True, "status": "linked",
                            "object_path": f"{archive_id}/{version_id}/{file_id}{MIME_EXT[mime]}"})
    return {"schema_version": 1, "patients": patients, "visits": visits, "attachments": attachments}


def load_config(data_dir):
    config_path = data_dir / "cloud.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    if not isinstance(config, dict):
        raise PublishError("private/cloud.json must contain an object.")
    return {
        "url": os.environ.get("HEALTH_SUPABASE_URL", config.get("url", "")).rstrip("/"),
        "service_key": os.environ.get("HEALTH_SUPABASE_SERVICE_KEY", config.get("service_key", "")),
        "archive_id": os.environ.get("HEALTH_ARCHIVE_ID", config.get("archive_id", "family")),
    }


class SupabasePublisher:
    def __init__(self, url, service_key):
        if not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url):
            raise PublishError("Use the HTTPS project URL from Supabase settings.")
        if not service_key:
            raise PublishError("Set HEALTH_SUPABASE_SERVICE_KEY locally before publishing.")
        if not service_key.startswith("sb_secret_"):
            try:
                part = service_key.split(".")[1]
                role = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("role")
                if role != "service_role":
                    raise ValueError()
            except (ValueError, IndexError):
                raise PublishError("Publishing requires a server-only secret key or legacy service_role key.") from None
        self.url = url
        self.service_key = service_key

    def request(self, path, content, mime="application/json"):
        headers = {"apikey": self.service_key, "Content-Type": mime}
        if self.service_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.service_key}"
        request = Request(self.url + path, data=content, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as error:
            # Never echo backend bodies: they may contain values or credentials.
            raise PublishError(f"Private upload failed (HTTP {error.code}); the prior published version remains active.") from None
        except URLError:
            raise PublishError("Cannot connect to the private data service; the prior published version remains active.") from None

    def publish(self, snapshot, file_bytes, archive_id, version_id):
        for file in snapshot["attachments"]:
            content = file_bytes[file["id"]]
            path = "/storage/v1/object/family-health/" + quote(file["object_path"], safe="/")
            self.request(path, content, file["mime"])
        payload = {"p_archive_id": archive_id, "p_version_id": str(version_id), "p_snapshot": snapshot,
                   "p_media": [{"id": f["id"], "object_path": f["object_path"]} for f in snapshot["attachments"]]}
        self.request("/rest/v1/rpc/publish_family_snapshot", json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def publish_archive(data_dir, dry_run=False):
    from healthlib.store import HealthStore
    data_dir = Path(data_dir).resolve()
    store = HealthStore(data_dir)
    try:
        config = load_config(data_dir)
        version_id = str(uuid.uuid4())
        snapshot = prepare_snapshot(store.confirmed_snapshot(), config["archive_id"], version_id)
        content = {}
        for file in snapshot["attachments"]:
            payload = store.attachment_path(file["id"]).read_bytes()
            if len(payload) != file["size"] or hashlib.sha256(payload).hexdigest() != file["sha256"]:
                raise PublishError("A source image changed during publication. Please retry after finishing edits.")
            content[file["id"]] = payload
        result = {"visits": len(snapshot["visits"]), "attachments": len(snapshot["attachments"]), "dry_run": dry_run}
        if dry_run:
            return result
        SupabasePublisher(config["url"], config["service_key"]).publish(snapshot, content, config["archive_id"], version_id)
        result.update(version_id=version_id, published_at=datetime.now(timezone.utc).isoformat())
        temp = data_dir / "last-sync.tmp"
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temp.replace(data_dir / "last-sync.json")
        store.set_metadata("last_sync", result)
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("HEALTH_DATA_DIR", str(ROOT / "private")))
    parser.add_argument("--dry-run", action="store_true", help="Validate a publication without uploading anything")
    args = parser.parse_args()
    try:
        print(json.dumps(publish_archive(args.data_dir, args.dry_run), ensure_ascii=False))
    except (PublishError, OSError, ValueError) as error:
        print(f"Publish failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
