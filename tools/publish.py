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
from urllib.parse import quote, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ALIASES = {"grandpa": "爺爺", "grandma": "嬷嬷"}
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/pdf": ".pdf"}
MAX_PDF_BYTES = 50_000_000
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
    def check_sources(source_ids, patient_id):
        if not isinstance(source_ids, list) or len(source_ids) > 500:
            raise PublishError("Invalid source links.")
        checked = []
        for file_id in source_ids:
            _id(file_id)
            file = files.get(file_id)
            if not file or file.get("privacy_checked") is not True or file.get("patient_id") != patient_id:
                raise PublishError("A source is missing, belongs to another person, or needs privacy review.")
            linked.add(file_id)
            checked.append(file_id)
        return list(dict.fromkeys(checked))
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

        visit["source_file_ids"] = check_sources(item.get("source_file_ids", []), patient_id)
        visit["medicines"] = []
        for med in item.get("medicines", []):
            medicine = {"id": _id(med.get("id")), "status": med.get("status", "unknown")}
            if medicine["status"] not in {"unknown", "current", "stopped"}:
                raise PublishError("Invalid medication status.")
            for field in ("name", "strength", "dose", "frequency", "instructions"):
                medicine[field] = _text(med.get(field, ""))
            medicine["category"] = _text(med.get("category", ""), 80).strip()
            medicine["source_file_ids"] = check_sources(med.get("source_file_ids", []), patient_id)
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
    reports = []
    if not isinstance(raw.get("reports", []), list):
        raise PublishError("Invalid reports collection.")
    for item in raw.get("reports", []):
        if not isinstance(item, dict) or item.get("status") != "confirmed" or not item.get("reviewed_at") or item.get("privacy_reviewed") is not True:
            raise PublishError("Only confirmed and reviewed reports can be published.")
        patient_id = item.get("patient_id")
        if patient_id not in patient_ids:
            raise PublishError("Report belongs to an unknown patient alias.")
        report = {"id": _id(item.get("id")), "patient_id": patient_id, "date": _date(item.get("date")),
                  "status": "confirmed", "privacy_reviewed": True}
        for field in ("title", "facility", "doctor", "summary", "notes", "created_at", "updated_at", "reviewed_at"):
            report[field] = _text(item.get(field, ""))
        report["category"] = _text(item.get("category", ""), 80).strip()
        report["source_file_ids"] = check_sources(item.get("source_file_ids", []), patient_id)
        if not report["title"].strip() or not report["source_file_ids"] or item.get("review_notes"):
            raise PublishError("Confirmed reports require a title, reviewed sources and resolved review notes.")
        reports.append(report)
    if len({r["id"] for r in reports}) != len(reports):
        raise PublishError("Duplicate report ID.")
    attachments = []
    for file_id in sorted(linked):
        file = files[file_id]
        mime = file.get("mime")
        if mime not in MIME_EXT:
            raise PublishError("Only sanitized images and raster-only PDF reports may be shared.")
        digest = file.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PublishError("Invalid source checksum.")
        size = file.get("size")
        maximum = MAX_PDF_BYTES if mime == "application/pdf" else 25 * 1024 * 1024
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= maximum:
            raise PublishError("Invalid source size.")
        exported = {"id": file_id, "patient_id": file["patient_id"],
                            "archive_name": _text(file.get("archive_name", "相片"), 255),
                            "mime": mime, "size": size, "sha256": digest,
                            "privacy_checked": True, "status": "linked",
                            "object_path": f"{archive_id}/objects/{file_id}-{digest}{MIME_EXT[mime]}"}
        preview = file.get("thumbnail")
        if mime == "application/pdf" and preview is None:
            raise PublishError("Reviewed PDF reports require a safe first-page preview.")
        if preview is not None:
            if not isinstance(preview, dict) or preview.get("mime") != "image/jpeg":
                raise PublishError("Compressed previews must be metadata-free JPEG images.")
            preview_digest = preview.get("sha256", "")
            if not isinstance(preview_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", preview_digest):
                raise PublishError("Invalid compressed preview checksum.")
            preview_size = preview.get("size")
            if isinstance(preview_size, bool) or not isinstance(preview_size, int) or not 0 < preview_size <= 3 * 1024 * 1024:
                raise PublishError("Invalid compressed preview size.")
            dimensions = {field: preview.get(field) for field in ("width", "height")}
            if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 960 for value in dimensions.values()):
                raise PublishError("Compressed preview dimensions must be at most 960 pixels.")
            exported["thumbnail"] = {"mime": "image/jpeg", "size": preview_size, "sha256": preview_digest,
                                     **dimensions, "object_path": f"{archive_id}/objects/{file_id}-{preview_digest}.thumb.jpg"}
        attachments.append(exported)
    return {"schema_version": 1, "patients": patients, "visits": visits, "reports": reports, "attachments": attachments}


def content_fingerprint(snapshot):
    """Stable content digest; physical version/CAS paths do not change medical content."""
    value = json.loads(json.dumps(snapshot, ensure_ascii=False))
    value.setdefault("reports", [])
    for field in ("published_at", "content_fingerprint"):
        value.pop(field, None)
    for attachment in value.get("attachments", []):
        attachment.pop("object_path", None)
        if attachment.get("thumbnail"):
            attachment["thumbnail"].pop("object_path", None)
    for field in ("patients", "visits", "reports", "attachments"):
        value[field] = sorted(value.get(field, []), key=lambda item: item["id"])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


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
        self.opener = build_opener(_NoRedirect())

    def request(self, path, content, mime="application/json"):
        headers = {"apikey": self.service_key, "Content-Type": mime}
        if self.service_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.service_key}"
        request = Request(self.url + path, data=content, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.read()
        except HTTPError as error:
            # Never echo backend bodies: they may contain values or credentials.
            raise PublishError(f"Private upload failed (HTTP {error.code}); the prior published version remains active.") from None
        except URLError:
            raise PublishError("Cannot connect to the private data service; the prior published version remains active.") from None

    def read_json(self, path):
        headers = {"apikey": self.service_key}
        if self.service_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.service_key}"
        try:
            with self.opener.open(Request(self.url + path, headers=headers, method="GET"), timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError()
            return json.loads(raw)
        except (HTTPError, URLError, OSError, ValueError):
            raise PublishError("Cannot verify the currently published archive; nothing was published.") from None

    def verify_object(self, asset):
        """GET and hash real bytes. Hash-like names/size metadata alone are insufficient."""
        headers = {"apikey": self.service_key}
        if self.service_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.service_key}"
        path = "/storage/v1/object/authenticated/family-health/" + quote(asset["object_path"], safe="/")
        try:
            with self.opener.open(Request(self.url + path, headers=headers, method="GET"), timeout=60) as response:
                if response.headers.get("Content-Type", "").split(";")[0].strip().lower() != asset["mime"]:
                    raise PublishError("An existing immutable cloud file has unexpected content; publication stopped.")
                digest, size = hashlib.sha256(), 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > asset["size"]:
                        raise PublishError("An existing immutable cloud file failed integrity verification.")
                    digest.update(chunk)
                if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
                    raise PublishError("An existing immutable cloud file failed integrity verification.")
                return True
        except HTTPError as error:
            if error.code in (400, 404):
                # Supabase Storage returns either 400 or 404 for an absent key.
                # Distinguish a missing object from all other HTTP400 failures.
                if error.code == 404:
                    return False
                try:
                    body = json.loads(error.read(4096))
                    if body.get("statusCode") in (404, "404") or body.get("code") in ("NoSuchKey", "not_found"):
                        return False
                except (ValueError, OSError):
                    pass
            raise PublishError("Cannot verify private cloud files; publication stopped.") from None
        except (URLError, OSError):
            raise PublishError("Cannot verify private cloud files; publication stopped.") from None

    def current_archive(self, archive_id):
        rows = self.read_json("/rest/v1/family_archives?" + urlencode({"select": "id,current_version,published_at", "id": "eq." + archive_id, "limit": "2"}))
        if not isinstance(rows, list) or len(rows) > 1:
            raise PublishError("Invalid current archive response.")
        if not rows:
            return None
        if rows[0].get("id") != archive_id:
            raise PublishError("Archive identity mismatch.")
        return rows[0]

    def unchanged_current(self, snapshot, archive_id):
        archive = self.current_archive(archive_id)
        if not archive or not archive.get("current_version"):
            return None
        version = str(uuid.UUID(archive["current_version"]))
        rows = self.read_json("/rest/v1/family_snapshots?" + urlencode({"select": "archive_id,version_id,snapshot,published_at", "archive_id": "eq." + archive_id, "version_id": "eq." + version, "limit": "2"}))
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("archive_id") != archive_id or rows[0].get("version_id") != version:
            return None
        remote = rows[0].get("snapshot")
        try:
            normalized = prepare_snapshot(remote, archive_id, version)
            if content_fingerprint(snapshot) != content_fingerprint(remote) or content_fingerprint(normalized) != content_fingerprint(remote):
                return None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        expected = {}
        for file in remote["attachments"]:
            for asset_id, asset, suffix in [(file["id"], file, MIME_EXT[file["mime"]]),
                    *([(file["id"] + ":thumbnail", file["thumbnail"], ".thumb.jpg")] if file.get("thumbnail") else [])]:
                legacy = f"{archive_id}/{version}/{file['id']}{suffix}"
                cas = f"{archive_id}/objects/{file['id']}-{asset['sha256']}{suffix}"
                if asset.get("object_path") not in (legacy, cas):
                    return None
                expected[asset_id] = asset["object_path"]
        manifest = []
        for offset in range(0, 20001, 500):
            page = self.read_json("/rest/v1/family_media?" + urlencode({"select": "attachment_id,object_path", "archive_id": "eq." + archive_id, "version_id": "eq." + version, "order": "attachment_id", "limit": "500", "offset": str(offset)}))
            if not isinstance(page, list):
                return None
            manifest.extend(page)
            if len(page) < 500:
                break
        if len(manifest) != len(expected) or {m.get("attachment_id"): m.get("object_path") for m in manifest} != expected:
            return None
        for file in remote["attachments"]:
            for asset in [file, *([file["thumbnail"]] if file.get("thumbnail") else [])]:
                if not self.verify_object(asset):
                    return None
        current = self.current_archive(archive_id)
        if not current or current.get("current_version") != version:
            return None
        return {"version_id": version, "published_at": rows[0].get("published_at") or archive.get("published_at")}

    def publish(self, snapshot, file_bytes, archive_id, version_id):
        media = []
        uploaded, reused = 0, 0
        def put(asset, payload):
            nonlocal uploaded, reused
            if len(payload) != asset["size"] or hashlib.sha256(payload).hexdigest() != asset["sha256"]:
                raise PublishError("Local publication bytes failed integrity verification.")
            if self.verify_object(asset):
                reused += 1
                return
            path = "/storage/v1/object/family-health/" + quote(asset["object_path"], safe="/")
            try:
                self.request(path, payload, asset["mime"])
                uploaded += 1
            except PublishError:
                # A concurrent identical upload may have won. Only verified bytes
                # permit recovery; never overwrite or delete an existing object.
                if not self.verify_object(asset):
                    raise
                reused += 1
        for file in snapshot["attachments"]:
            content = file_bytes[file["id"]]
            put(file, content)
            media.append({"id": file["id"], "object_path": file["object_path"]})
            if file.get("thumbnail"):
                preview = file["thumbnail"]
                preview_id = file["id"] + ":thumbnail"
                put(preview, file_bytes[preview_id])
                media.append({"id": preview_id, "object_path": preview["object_path"]})
        payload = {"p_archive_id": archive_id, "p_version_id": str(version_id), "p_snapshot": snapshot,
                   "p_media": media}
        self.request("/rest/v1/rpc/publish_family_snapshot", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return {"uploaded_files": uploaded, "reused_files": reused}


def publish_archive(data_dir, dry_run=False):
    from healthlib.store import HealthStore
    data_dir = Path(data_dir).resolve()
    store = HealthStore(data_dir)
    try:
        config = load_config(data_dir)
        version_id = str(uuid.uuid4())
        raw = store.confirmed_snapshot()
        # Derivatives are produced only after the store's confirmation/privacy boundary.
        for source in raw["attachments"]:
            source["thumbnail"] = store.thumbnail_info(source["id"])
        snapshot = prepare_snapshot(raw, config["archive_id"], version_id)
        content = {}
        for file in snapshot["attachments"]:
            payload = store.attachment_path(file["id"]).read_bytes()
            if len(payload) != file["size"] or hashlib.sha256(payload).hexdigest() != file["sha256"]:
                raise PublishError("A source image changed during publication. Please retry after finishing edits.")
            content[file["id"]] = payload
            preview = file.get("thumbnail")
            if preview:
                payload = store.thumbnail_path(file["id"]).read_bytes()
                if len(payload) != preview["size"] or hashlib.sha256(payload).hexdigest() != preview["sha256"]:
                    raise PublishError("A compressed preview changed during publication. Please retry after finishing edits.")
                content[file["id"] + ":thumbnail"] = payload
        result = {"visits": len(snapshot["visits"]), "reports": len(snapshot["reports"]), "attachments": len(snapshot["attachments"]), "dry_run": dry_run}
        if dry_run:
            return result
        client = SupabasePublisher(config["url"], config["service_key"])
        unchanged = client.unchanged_current(snapshot, config["archive_id"])
        if unchanged:
            result.update(unchanged, skipped=True, uploaded_files=0, reused_files=0)
        else:
            usage = client.publish(snapshot, content, config["archive_id"], version_id) or {}
            result.update(usage, skipped=False, version_id=version_id, published_at=datetime.now(timezone.utc).isoformat())
        result["content_fingerprint"] = content_fingerprint(snapshot)
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
