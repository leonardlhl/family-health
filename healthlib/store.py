"""SQLite archive and image lifecycle. No cloud credentials are stored here."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError
from .errors import ValidationError, PrivacyReviewRequired
from . import pdf

Image.MAX_IMAGE_PIXELS = 40_000_000
SCHEMA_VERSION = 1
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = pdf.MAX_PDF_BYTES
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_BACKUP_FILES = 5000
THUMBNAIL_EDGE = 960
THUMBNAIL_QUALITY = 86
THUMBNAIL_VERSION = "v1"
PATIENTS = {"grandpa": "爺爺", "grandma": "嬷嬷"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def text(value, limit=10000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError("文字欄位格式錯誤或內容過長")
    return value.strip()


def iso_date(value, optional=True):
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError("日期須使用 YYYY-MM-DD 格式")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("日期不存在，請核對原相") from exc
    return value


def identifier(value, prefix):
    if value is None or value == "":
        return prefix + uuid.uuid4().hex
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValidationError("紀錄編號格式錯誤")
    return value


def source_ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        raise ValidationError("相片來源清單格式錯誤")
    return list(dict.fromkeys(identifier(x, "a_") for x in value))


def safe_filename(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (name[:100] or "醫療紀錄").rstrip(" .")


def decode_base64(value, max_bytes=MAX_IMAGE_BYTES):
    if not isinstance(value, str) or len(value) > ((max_bytes + 2) // 3) * 4 + 8:
        raise ValidationError("檔案過大或格式錯誤")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError("檔案內容無法解碼") from exc
    if len(result) > max_bytes:
        raise ValidationError("檔案過大")
    return result


def inspect_image(raw, allow_pdf=True):
    if raw.startswith(b"%PDF-") and allow_pdf:
        pdf.info(raw)
        return "application/pdf", ".pdf"
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValidationError("相片須小於 25 MB")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValidationError("只支援 JPG、PNG、WebP 相片；PDF 可先放入待處理")
            if im.width * im.height > 40_000_000:
                raise ValidationError("相片像素過多，請先縮小")
            fmt = im.format
            im.verify()
        return {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}[fmt]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("無法讀取相片，請使用 JPG、PNG 或 WebP") from exc


def clean_image(raw):
    inspect_image(raw, allow_pdf=False)
    try:
        with Image.open(io.BytesIO(raw)) as source:
            oriented = ImageOps.exif_transpose(source)
            pixels = oriented.convert("RGB")
            # A new pixel-only image prevents source EXIF, comments and profiles leaking.
            clean = Image.new("RGB", pixels.size, "white")
            clean.paste(pixels)
            out = io.BytesIO()
            clean.save(out, format="PNG", optimize=True)
            result = out.getvalue()
            if len(result) > MAX_IMAGE_BYTES:
                raise ValidationError("清理後相片超過 25 MB，請先縮小")
            return result
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValidationError("相片無法清理，請重新匯入") from exc


class HealthStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.lock = threading.RLock()
        self.restore_tokens = {}
        for folder in ("inbox/grandpa", "inbox/grandma", "staging", "archive", "backups", "exports", "restore", "previews"):
            target = self.data_dir / folder
            target.mkdir(parents=True, exist_ok=True)
            if not target.resolve().is_relative_to(self.data_dir):
                raise ValidationError("資料夾不能連結到檔案庫以外的位置")
        self.db_path = self.data_dir / "archive.sqlite3"
        if self.db_path.is_symlink():
            raise ValidationError("資料庫不能使用外部連結")
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visits (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        for patient_id, name in PATIENTS.items():
            self.db.execute("INSERT OR IGNORE INTO patients VALUES (?,?)", (patient_id, json.dumps({"id": patient_id, "name": name, "age": None, "sex": "unknown"}, ensure_ascii=False)))
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def _rows(self, table):
        if table not in {"patients", "visits", "reports", "attachments"}:
            raise ValidationError("資料表不存在")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {table}")]

    def _get(self, table, item_id):
        if table not in {"patients", "visits", "reports", "attachments"}:
            raise ValidationError("資料表不存在")
        row = self.db.execute(f"SELECT payload FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise ValidationError("找不到紀錄")
        return json.loads(row[0])

    def _put(self, table, item):
        self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (item["id"], json.dumps(item, ensure_ascii=False)))

    def metadata(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_metadata(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()

    @staticmethod
    def _public_attachment(item):
        fields = {"id", "patient_id", "original_name", "archive_name", "sha256", "mime", "size", "status", "privacy_checked", "imported_at", "ocr_text", "privacy_reviewed_at", "kind", "source_group", "pages"}
        return {key: value for key, value in item.items() if key in fields}

    def data(self):
        with self.lock:
            visits = sorted(self._rows("visits"), key=lambda x: (x.get("date") or "", x["updated_at"]), reverse=True)
            reports = sorted(self._rows("reports"), key=lambda x: (x.get("date") or "", x["updated_at"]), reverse=True)
            return {"schema_version": SCHEMA_VERSION, "patients": self._rows("patients"), "visits": visits, "reports": reports, "attachments": [self._public_attachment(x) for x in self._rows("attachments")]}

    def update_patient(self, value):
        if not isinstance(value, dict) or value.get("id") not in PATIENTS:
            raise ValidationError("請選擇爺爺或嬷嬷")
        age = value.get("age")
        if age == "":
            age = None
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0 or age > 125):
            raise ValidationError("年齡須為 0 至 125 的整數，或留空")
        sex = value.get("sex", "unknown")
        if sex not in {"unknown", "male", "female"}:
            raise ValidationError("性別格式錯誤")
        patient = {"id": value["id"], "name": PATIENTS[value["id"]], "age": age, "sex": sex}
        with self.lock:
            self._put("patients", patient)
            self.db.commit()
        return patient

    def upload(self, patient_id, name, raw, kind="visit", source_group=None):
        if patient_id not in PATIENTS:
            raise ValidationError("請選擇爺爺或嬷嬷")
        if kind not in {"visit", "report"}:
            raise ValidationError("來源類別須為 visit 或 report")
        mime, ext = inspect_image(raw)
        checksum = digest(raw)
        with self.lock:
            for item in self._rows("attachments"):
                if item["patient_id"] == patient_id and checksum in {item.get("sha256"), item.get("source_sha256")}:
                    return {**self._public_attachment(item), "duplicate": True}
            item_id = "a_" + uuid.uuid4().hex
            relative = f"staging/{item_id}{ext}"
            target = self.data_dir / relative
            target.write_bytes(raw)
            item = {"id": item_id, "patient_id": patient_id, "original_name": "待處理檔案" + ext, "archive_name": "", "sha256": checksum, "source_sha256": checksum, "mime": mime, "size": len(raw), "status": "pending", "privacy_checked": False, "imported_at": now(), "ocr_text": "", "path": relative, "kind": kind}
            if source_group:
                item["source_group"] = identifier(source_group, "g_")
            if mime == "application/pdf":
                item["pages"] = pdf.info(raw)["pages"]
            self._put("attachments", item)
            self.db.commit()
            return self._public_attachment(item)

    def import_folders(self):
        """Validate explicitly configured read-only source folders.

        Configuration is local-only. Invalid or unavailable entries are skipped
        with visible warnings; they must never prevent the archive from opening.
        """
        settings_path = self.data_dir / "import-folders.json"
        result = {"folders": [], "warnings": []}
        if not settings_path.exists():
            return result
        try:
            if settings_path.is_symlink() or settings_path.stat().st_size > 65536:
                raise ValidationError("匯入資料夾設定不安全或檔案過大")
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, list) or len(settings) > 32:
                raise ValidationError("匯入資料夾設定須為清單，最多 32 個資料夾")
        except (OSError, ValueError) as exc:
            result["warnings"].append("無法讀取 import-folders.json，請檢查本機匯入設定")
            return result
        seen = {str((self.data_dir / "inbox" / pid).resolve()).casefold(): pid for pid in PATIENTS}
        for index, item in enumerate(settings, 1):
            try:
                if not isinstance(item, dict) or item.get("patient_id") not in PATIENTS:
                    raise ValidationError("人物須使用 grandpa 或 grandma")
                kind = item.get("kind", "visit")
                if kind not in {"visit", "report"}:
                    raise ValidationError("類別須使用 visit 或 report")
                raw_path = item.get("path")
                if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
                    raise ValidationError("資料夾路徑格式錯誤")
                folder = Path(raw_path)
                if not folder.is_absolute():
                    raise ValidationError("資料夾必須使用完整絕對路徑")
                if not folder.is_dir():
                    raise ValidationError("資料夾不存在或暫時無法讀取")
                for component in (folder, *folder.parents):
                    if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                        raise ValidationError("不能使用符號連結或連接點資料夾")
                folder = folder.resolve(strict=True)
                key = str(folder).casefold()
                if key in seen:
                    if seen[key] != item["patient_id"]:
                        raise ValidationError("同一資料夾只可屬於一位家人")
                    continue
                seen[key] = item["patient_id"]
                result["folders"].append({"patient_id": item["patient_id"], "path": str(folder), "kind": kind})
            except (OSError, ValidationError, RuntimeError) as exc:
                result["warnings"].append(f"第 {index} 個匯入資料夾：{exc}")
        return result

    def scan(self):
        result = {"imported": 0, "duplicates": 0, "skipped": 0, "errors": [], "input_policy": "待處理原檔不會備份或同步；私隱處理後可自行移除 inbox 原檔。"}
        configured = self.import_folders()
        result["warnings"] = configured["warnings"]
        folders = [{"patient_id": pid, "path": str(self.data_dir / "inbox" / pid)} for pid in PATIENTS] + configured["folders"]
        for entry in folders:
            patient_id, folder = entry["patient_id"], Path(entry["path"])
            try:
                children = sorted(folder.iterdir())
                if entry.get("kind") == "report":
                    # Report sources can be grouped in subfolders. Never follow
                    # symlinks/junctions or walk outside the configured root.
                    pending_dirs = [(folder, 0)]
                    children = []
                    visited = 0
                    while pending_dirs:
                        current, depth = pending_dirs.pop()
                        visited += 1
                        if visited > 1000:
                            raise ValidationError("報告資料夾超過 1,000 個子資料夾，請分批處理")
                        for path in sorted(current.iterdir()):
                            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                                result["skipped"] += 1
                                continue
                            if not path.resolve().is_relative_to(folder.resolve()):
                                raise ValidationError("報告來源不可超出指定資料夾")
                            if path.is_dir():
                                if depth >= 8:
                                    result["skipped"] += 1
                                else:
                                    pending_dirs.append((path, depth + 1))
                            else:
                                children.append(path)
                            if len(children) > 2000:
                                raise ValidationError("報告來源超過 2,000 個檔案，請分批處理")
            except (OSError, ValidationError):
                result["errors"].append({"patient_id": patient_id, "error": "匯入資料夾暫時無法讀取"})
                continue
            for path in children:
                if not path.is_file() or path.is_symlink() or getattr(path, "is_junction", lambda: False)() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
                    result["skipped"] += 1
                    continue
                try:
                    limit = MAX_FILE_BYTES if path.suffix.lower() == ".pdf" else MAX_IMAGE_BYTES
                    if path.stat().st_size > limit:
                        raise ValidationError("檔案超過相片 25 MB／PDF 50 MB 限制")
                    kind = entry.get("kind", "visit")
                    group = "g_" + digest(str(path.parent.resolve()).casefold().encode("utf-8"))[:24] if kind == "report" else None
                    item = self.upload(patient_id, path.name, path.read_bytes(), kind, group)
                    result["duplicates" if item.get("duplicate") else "imported"] += 1
                except (OSError, ValidationError) as exc:
                    result["skipped"] += 1
                    result["errors"].append({"patient_id": patient_id, "error": str(exc)})
        return result

    def attachment_path(self, item_id):
        with self.lock:
            item = self._get("attachments", item_id)
            path = (self.data_dir / item["path"]).resolve()
            if not path.is_relative_to(self.data_dir) or path.is_symlink() or not path.is_file():
                raise ValidationError("找不到原相檔案")
            return path

    def attachment(self, item_id):
        with self.lock:
            return self._public_attachment(self._get("attachments", item_id))

    def _invalidate_thumbnails(self, item_id, keep=None):
        # Only delete generated files belonging to this exact safe attachment ID.
        if not isinstance(item_id, str) or not SAFE_ID.fullmatch(item_id):
            raise ValidationError("相片編號格式錯誤")
        cache_dir = (self.data_dir / "previews").resolve()
        if not cache_dir.is_relative_to(self.data_dir):
            raise ValidationError("預覽資料夾不能連結到檔案庫以外的位置")
        for candidate in cache_dir.glob(item_id + "_*.jpg"):
            resolved = candidate.resolve()
            if candidate != keep and not candidate.is_symlink() and resolved.parent == cache_dir:
                candidate.unlink(missing_ok=True)

    def thumbnail_path(self, item_id):
        """Return a small, anonymous derivative; archive pixels stay unchanged.

        Source checksum is verified even for cache hits. A changed image must go
        through the privacy review lifecycle before a new derivative is served.
        """
        with self.lock:
            item = self._get("attachments", item_id)
            if item.get("privacy_checked") is not True:
                raise PrivacyReviewRequired("相片尚未完成私隱核對，暫不提供藥物預覽")
            source_path = self.attachment_path(item_id)
            archive_dir = (self.data_dir / "archive").resolve()
            if item.get("mime") not in {"image/png", "image/jpeg", "image/webp", "application/pdf"} or not source_path.is_relative_to(archive_dir):
                raise PrivacyReviewRequired("只可從已核對的歸檔相片建立預覽")
            source_bytes = source_path.read_bytes()
            checksum = digest(source_bytes)
            if checksum != item.get("sha256"):
                self._invalidate_thumbnails(item_id)
                raise ValidationError("歸檔相片檢查碼不符，請重新核對相片")
            cache_dir = self.data_dir / "previews"
            if cache_dir.is_symlink() or getattr(cache_dir, "is_junction", lambda: False)() or not cache_dir.resolve().is_relative_to(self.data_dir):
                raise ValidationError("預覽資料夾不能使用外部連結")
            target = cache_dir / f"{item_id}_{checksum}_{THUMBNAIL_VERSION}_{THUMBNAIL_EDGE}_q{THUMBNAIL_QUALITY}.jpg"
            if target.is_symlink() or target.resolve().parent != cache_dir.resolve():
                raise ValidationError("預覽檔案路徑不安全")
            if target.is_file():
                try:
                    with Image.open(target) as cached:
                        if cached.format != "JPEG" or max(cached.size) > THUMBNAIL_EDGE or cached.getexif() or cached.info.get("comment"):
                            raise ValueError("Invalid cached preview")
                        cached.verify()
                    return target
                except (OSError, ValueError, Image.DecompressionBombError):
                    target.unlink(missing_ok=True)
            try:
                if item["mime"] == "application/pdf":
                    source_bytes = pdf.page_png(source_bytes, 0)
                with Image.open(io.BytesIO(source_bytes)) as source:
                    oriented = ImageOps.exif_transpose(source)
                    pixels = oriented.convert("RGB")
                    pixels.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
                    # New image guarantees no EXIF, text comments or profiles.
                    clean = Image.new("RGB", pixels.size, "white")
                    clean.paste(pixels)
                    output = io.BytesIO()
                    clean.save(output, format="JPEG", quality=THUMBNAIL_QUALITY, subsampling=0, optimize=True, progressive=True)
                    encoded = output.getvalue()
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ValidationError("無法建立相片預覽，請檢查歸檔相片") from exc
            temporary = target.with_name(target.name + "." + secrets.token_hex(4) + ".tmp")
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            self._invalidate_thumbnails(item_id, keep=target)
            return target

    def thumbnail_info(self, item_id):
        with self.lock:
            path = self.thumbnail_path(item_id)
            raw = path.read_bytes()
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
            return {"mime": "image/jpeg", "sha256": digest(raw), "size": len(raw), "width": width, "height": height}

    def _save_sanitized(self, item_id, pixels, mime, extension, pages=None):
        item = self._get("attachments", item_id)
        old_path = self.attachment_path(item_id)
        archive_dir = self.data_dir / "archive"
        new_path = old_path if old_path.is_relative_to(archive_dir) and old_path.suffix == extension else archive_dir / (item_id + extension)
        temporary = new_path.with_name(new_path.name + "." + secrets.token_hex(4) + ".tmp")
        temporary.write_bytes(pixels)
        os.replace(temporary, new_path)
        item.update(path=new_path.relative_to(self.data_dir).as_posix(), mime=mime, size=len(pixels), sha256=digest(pixels), privacy_checked=True, original_name="", archive_name=new_path.name, ocr_text="", privacy_reviewed_at=now())
        if pages is not None:
            item["pages"] = pages
        self._put("attachments", item)
        for table in ("visits", "reports"):
            for record in self._rows(table):
                if item_id in self._all_source_ids(record) and record["status"] == "confirmed":
                    record.update(status="draft", privacy_reviewed=False, reviewed_at=None, updated_at=now())
                    self._put(table, record)
        self.db.commit()
        self._invalidate_thumbnails(item_id)
        if old_path != new_path and old_path.is_relative_to(self.data_dir):
            old_path.unlink(missing_ok=True)
        return self._public_attachment(item)

    def pdf_info(self, item_id):
        with self.lock:
            item = self._get("attachments", item_id)
            if item["mime"] != "application/pdf":
                raise ValidationError("這個來源不是 PDF")
            raw = self.attachment_path(item_id).read_bytes()
            if digest(raw) != item["sha256"]:
                raise ValidationError("PDF 檢查碼不符，請重新核對來源")
            return {**pdf.info(raw), "sha256": item["sha256"], "privacy_checked": item.get("privacy_checked") is True}

    def pdf_page(self, item_id, page_number):
        with self.lock:
            self.pdf_info(item_id)
            return pdf.page_png(self.attachment_path(item_id).read_bytes(), page_number)

    def sanitize_pdf(self, item_id, pages, reviewed_pages, reviewed=False, source_sha256=None):
        if reviewed is not True:
            raise ValidationError("請先確認每頁已遮蓋所有病人身份資料")
        with self.lock:
            information = self.pdf_info(item_id)
            if source_sha256 != information["sha256"]:
                raise ValidationError("PDF 已改變，請重新逐頁核對")
            pixels = pdf.sanitize(self.attachment_path(item_id).read_bytes(), pages, reviewed_pages)
            return self._save_sanitized(item_id, pixels, "application/pdf", ".pdf", information["pages"])

    def sanitize_attachment(self, item_id, raw, reviewed=False):
        if reviewed is not True:
            raise ValidationError("請先確認相片已遮蓋所有病人身份資料")
        with self.lock:
            item = self._get("attachments", item_id)
            if item["mime"] == "application/pdf":
                raise ValidationError("PDF 必須使用逐頁遮蓋及核對功能，不可用單張圖片取代")
            pixels = clean_image(raw)
            return self._save_sanitized(item_id, pixels, "image/png", ".png")

    def review_attachment(self, item_id, reviewed=False):
        with self.lock:
            item = self._get("attachments", item_id)
            if item["mime"] == "application/pdf":
                raise ValidationError("PDF 請使用逐頁遮蓋及核對功能")
            return self.sanitize_attachment(item_id, self.attachment_path(item_id).read_bytes(), reviewed)

    @staticmethod
    def _all_source_ids(visit):
        ids = list(visit.get("source_file_ids", []))
        for med in visit.get("medicines", []):
            ids.extend(med.get("source_file_ids", []))
        return list(dict.fromkeys(ids))

    def _validated_visit(self, value, existing=None):
        if not isinstance(value, dict):
            raise ValidationError("求診紀錄格式錯誤")
        patient_id = value.get("patient_id")
        if patient_id not in PATIENTS:
            raise ValidationError("請選擇爺爺或嬷嬷")
        forbidden = {"patient_name", "name", "dob", "birth_date", "id_number", "identity_number", "patient_address", "patient_phone", "patient_email"}
        if forbidden.intersection(value):
            raise ValidationError("請移除病人身份欄位，只保留爺爺／嬷嬷、年齡及性別")
        item = {"id": identifier(value.get("id"), "v_"), "patient_id": patient_id, "date": iso_date(value.get("date")), "status": "draft", "privacy_reviewed": False, "created_at": existing["created_at"] if existing else now(), "updated_at": now(), "reviewed_at": None}
        for field in ("doctor", "clinic", "reason", "diagnosis", "notes", "review_notes"):
            item[field] = text(value.get(field))
        item["source_file_ids"] = source_ids(value.get("source_file_ids"))
        medicines = value.get("medicines", [])
        if not isinstance(medicines, list) or len(medicines) > 100:
            raise ValidationError("藥物清單格式錯誤")
        item["medicines"] = []
        for med in medicines:
            if not isinstance(med, dict):
                raise ValidationError("藥物紀錄格式錯誤")
            medicine = {"id": identifier(med.get("id"), "m_")}
            for field in ("name", "strength", "dose", "frequency", "instructions"):
                medicine[field] = text(med.get(field))
            # A label supplied and reviewed by the operator, never an inferred
            # drug class or clinical recommendation.
            medicine["category"] = text(med.get("category"), 80)
            medicine["status"] = med.get("status", "unknown")
            if medicine["status"] not in {"unknown", "current", "stopped"}:
                raise ValidationError("服藥狀態格式錯誤")
            medicine["source_file_ids"] = source_ids(med.get("source_file_ids"))
            item["medicines"].append(medicine)
        appointment = value.get("appointment")
        if appointment is not None:
            if not isinstance(appointment, dict):
                raise ValidationError("覆診紀錄格式錯誤")
            appt = {"date": iso_date(appointment.get("date"), optional=False), "time": text(appointment.get("time"), 5), "location": text(appointment.get("location")), "notes": text(appointment.get("notes")), "status": appointment.get("status", "scheduled")}
            if appt["time"] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", appt["time"]):
                raise ValidationError("覆診時間須使用 HH:MM 格式")
            if appt["status"] not in {"scheduled", "completed", "cancelled"}:
                raise ValidationError("覆診狀態格式錯誤")
            item["appointment"] = appt
        else:
            item["appointment"] = None
        for file_id in self._all_source_ids(item):
            source = self._get("attachments", file_id)
            if source["patient_id"] != patient_id:
                raise ValidationError("相片屬於另一位家人，請核對人物")
        return item

    def upsert_visit(self, value):
        with self.lock:
            existing = None
            if isinstance(value, dict) and value.get("id"):
                row = self.db.execute("SELECT payload FROM visits WHERE id=?", (value["id"],)).fetchone()
                existing = json.loads(row[0]) if row else None
            item = self._validated_visit(value, existing)
            self._put("visits", item)
            for file_id in self._all_source_ids(item):
                attachment = self._get("attachments", file_id)
                attachment["status"] = "linked"
                self._put("attachments", attachment)
            self.db.commit()
            return item

    def _validated_report(self, value, existing=None):
        if not isinstance(value, dict) or value.get("patient_id") not in PATIENTS:
            raise ValidationError("請選擇爺爺或嬷嬷，並提供有效報告紀錄")
        allowed = {"id", "patient_id", "date", "title", "category", "facility", "doctor", "summary", "notes", "review_notes", "status", "privacy_reviewed", "source_file_ids", "created_at", "updated_at", "reviewed_at"}
        if set(value) - allowed:
            raise ValidationError("報告包含不支援的欄位；請勿保留病人身份資料")
        item = {"id": identifier(value.get("id"), "r_"), "patient_id": value["patient_id"], "date": iso_date(value.get("date")), "status": "draft", "privacy_reviewed": False, "created_at": existing["created_at"] if existing else now(), "updated_at": now(), "reviewed_at": None}
        for field in ("title", "category", "facility", "doctor", "summary", "notes", "review_notes"):
            item[field] = text(value.get(field), 120 if field == "title" else 80 if field == "category" else 10000)
        item["source_file_ids"] = source_ids(value.get("source_file_ids"))
        for file_id in item["source_file_ids"]:
            if self._get("attachments", file_id)["patient_id"] != item["patient_id"]:
                raise ValidationError("報告來源屬於另一位家人，請核對人物")
        return item

    def upsert_report(self, value):
        with self.lock:
            existing = None
            if isinstance(value, dict) and value.get("id"):
                row = self.db.execute("SELECT payload FROM reports WHERE id=?", (value["id"],)).fetchone()
                existing = json.loads(row[0]) if row else None
            item = self._validated_report(value, existing)
            self._put("reports", item)
            for file_id in item["source_file_ids"]:
                attachment = self._get("attachments", file_id)
                attachment["status"] = "linked"
                self._put("attachments", attachment)
            self.db.commit()
            return item

    def _assert_report_confirmable(self, item):
        if not item.get("date") or not item.get("title", "").strip() or not item.get("source_file_ids"):
            raise ValidationError("確認報告前請填寫日期、簡介標題，並連結至少一個來源檔案")
        if item.get("review_notes", "").strip():
            raise ValidationError("報告仍有待核對內容，請核對來源後清除待核對備註")
        for file_id in item["source_file_ids"]:
            source = self._get("attachments", file_id)
            if source["patient_id"] != item["patient_id"] or source.get("privacy_checked") is not True:
                raise ValidationError("請先遮蓋並確認所有報告來源沒有病人身份資料")

    def _archive_record_sources(self, item, table):
        """Move each managed source once; shared references keep one physical file."""
        report = table == "reports"
        label = item["title"] if report else item["doctor"] or item["clinic"]
        base = self.data_dir / "archive"
        folder = base / "reports" / item["patient_id"] / safe_filename(item["date"] + "_" + label[:60]) if report else base
        for component in (folder, *folder.parents):
            if component == self.data_dir:
                break
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise ValidationError("歸檔資料夾不能使用符號連結或連接點")
        if not folder.resolve().is_relative_to(base.resolve()):
            raise ValidationError("歸檔資料夾路徑不安全")
        folder.mkdir(parents=True, exist_ok=True)
        moved = []
        try:
            for index, file_id in enumerate(self._all_source_ids(item), 1):
                source = self._get("attachments", file_id)
                # A source linked by another confirmed record is already named.
                if source.get("archive_owner_id") and source["archive_owner_id"] != item["id"]:
                    continue
                old_path = self.attachment_path(file_id)
                extension = ".pdf" if source["mime"] == "application/pdf" else ".png"
                name = safe_filename(f"{item['date']}_{PATIENTS[item['patient_id']]}_{label[:60]}_{index:02d}") + extension
                new_path = folder / name
                if old_path != new_path and new_path.exists():
                    name = safe_filename(new_path.stem + "_" + file_id[-12:]) + extension
                    new_path = folder / name
                    if new_path.exists() and old_path != new_path:
                        raise ValidationError("歸檔名稱已被其他檔案使用，請檢查報告簡介")
                if old_path != new_path:
                    os.rename(old_path, new_path)
                    moved.append((old_path, new_path))
                source.update(path=new_path.relative_to(self.data_dir).as_posix(), archive_name=name, status="linked", archive_owner_id=item["id"])
                self._put("attachments", source)
            self._put(table, item)
            self.db.commit()
        except Exception:
            self.db.rollback()
            for old_path, new_path in reversed(moved):
                if new_path.exists() and not old_path.exists():
                    os.rename(new_path, old_path)
            raise

    def confirm_report(self, item_id, reviewed=False):
        if reviewed is not True:
            raise ValidationError("請確認報告資料準確，並已移除病人身份資料")
        with self.lock:
            item = self._get("reports", item_id)
            self._assert_report_confirmable(item)
            item.update(status="confirmed", privacy_reviewed=True, reviewed_at=now(), updated_at=now())
            self._archive_record_sources(item, "reports")
            return item

    def delete_report(self, item_id):
        with self.lock:
            item = self._get("reports", item_id)
            if item["status"] != "draft":
                raise ValidationError("只可移除草稿；已確認報告請先編輯並重新核對")
            self.db.execute("DELETE FROM reports WHERE id=?", (item_id,))
            self.db.commit()
            return {"deleted": True, "id": item_id, "sources_preserved": True}

    def _assert_confirmable(self, item):
        if not item.get("date") or not (item.get("doctor") or item.get("clinic")):
            raise ValidationError("確認前請填寫求診日期及醫生或診所")
        if item.get("review_notes", "").strip():
            raise ValidationError("仍有待核對內容，請核對原相並清除待核對備註")
        if any(not med.get("name") for med in item.get("medicines", [])):
            raise ValidationError("請核對所有藥名；未能讀取的藥物不可標為已確認")
        for file_id in self._all_source_ids(item):
            source = self._get("attachments", file_id)
            if source["patient_id"] != item["patient_id"] or not source.get("privacy_checked"):
                raise ValidationError("請先遮蓋並確認所有相關相片沒有病人身份資料")

    def confirm_visit(self, item_id, reviewed=False):
        if reviewed is not True:
            raise ValidationError("請確認資料準確，並已移除病人身份資料")
        with self.lock:
            item = self._get("visits", item_id)
            self._assert_confirmable(item)
            item.update(status="confirmed", privacy_reviewed=True, reviewed_at=now(), updated_at=now())
            self._archive_record_sources(item, "visits")
            return item

    def appointment_status(self, item_id, status):
        if status not in {"scheduled", "completed", "cancelled"}:
            raise ValidationError("覆診狀態格式錯誤")
        with self.lock:
            item = self._get("visits", item_id)
            if not item.get("appointment"):
                raise ValidationError("這次求診沒有覆診安排")
            item["appointment"]["status"] = status
            item["updated_at"] = now()
            self._put("visits", item)
            self.db.commit()
            return item

    def confirmed_snapshot(self):
        with self.lock:
            result = self.data()
            visits = [v for v in result["visits"] if v["status"] == "confirmed" and v.get("privacy_reviewed")]
            reports = [v for v in result["reports"] if v["status"] == "confirmed" and v.get("privacy_reviewed")]
            needed = set()
            for visit in visits:
                self._assert_confirmable(visit)
                needed.update(self._all_source_ids(visit))
            for report in reports:
                self._assert_report_confirmable(report)
                needed.update(report["source_file_ids"])
            result["visits"] = visits
            result["reports"] = reports
            result["attachments"] = [{**a, "original_name": "", "ocr_text": ""} for a in result["attachments"] if a["id"] in needed and a.get("privacy_checked")]
            return result

    def family_data(self):
        """Read-only local preview of exactly the reviewed family-facing data.

        No files are generated and no local paths, operator-only review notes,
        source filenames or OCR text cross this boundary.
        """
        with self.lock:
            snapshot = self.confirmed_snapshot()

            def selected(item, fields):
                return {field: item[field] for field in fields if field in item}

            patients = []
            for patient in snapshot["patients"]:
                if patient.get("id") in PATIENTS:
                    patients.append({"id": patient["id"], "name": PATIENTS[patient["id"]], "age": patient.get("age"), "sex": patient.get("sex", "unknown")})
            visits = []
            for item in snapshot["visits"]:
                visit = selected(item, ("id", "patient_id", "date", "doctor", "clinic", "reason", "diagnosis", "notes", "status", "privacy_reviewed", "created_at", "updated_at", "reviewed_at"))
                visit["source_file_ids"] = list(item.get("source_file_ids", []))
                visit["medicines"] = []
                for med in item.get("medicines", []):
                    medicine = selected(med, ("id", "name", "strength", "dose", "frequency", "instructions", "status"))
                    medicine["category"] = text(med.get("category"), 80)
                    medicine["source_file_ids"] = list(med.get("source_file_ids", []))
                    visit["medicines"].append(medicine)
                appointment = item.get("appointment")
                visit["appointment"] = selected(appointment, ("date", "time", "location", "status", "notes")) if appointment else None
                visits.append(visit)
            attachments = [selected(item, ("id", "patient_id", "mime", "size", "sha256", "status", "privacy_checked")) for item in snapshot["attachments"]]
            reports = [selected(item, ("id", "patient_id", "date", "title", "category", "facility", "doctor", "summary", "notes", "status", "privacy_reviewed", "source_file_ids", "created_at", "updated_at", "reviewed_at")) for item in snapshot["reports"]]
            return {"schema_version": SCHEMA_VERSION, "patients": patients, "visits": visits, "reports": reports, "attachments": attachments}

    def queue(self):
        with self.lock:
            data = self.data()
            items = []
            linked = {fid for visit in data["visits"] + data["reports"] for fid in self._all_source_ids(visit)}
            for item in data["attachments"]:
                if item["id"] not in linked:
                    items.append({"id": item["id"], "patient_id": item["patient_id"], "path": str(self.attachment_path(item["id"])), "mime": item["mime"], "privacy_checked": item["privacy_checked"], "kind": item.get("kind", "visit"), "source_group": item.get("source_group"), "pages": item.get("pages")})
            return {"schema_version": SCHEMA_VERSION, "pending": items, "instructions": "只轉錄可見資料；不要保留病人姓名、證件、生日、地址或聯絡資料。人物用 grandpa/grandma；可保留年齡性別。醫生及診斷照原文保留。模糊藥名、劑量、日期寫入 review_notes，勿猜測。透過 tools/import_record.py 匯入 draft，先遮蓋相片再由使用者確認。"}

    def _archive_bytes(self):
        snapshot = self.confirmed_snapshot()
        files = {}
        for item in snapshot["attachments"]:
            raw = self.attachment_path(item["id"]).read_bytes()
            if digest(raw) != item["sha256"]:
                raise ValidationError("相片檢查碼不符，請檢查本機檔案")
            extension = ".pdf" if item["mime"] == "application/pdf" else ".png"
            files["media/" + item["id"] + extension] = raw
            if len(files) >= MAX_BACKUP_FILES - 2 or sum(len(value) for value in files.values()) > MAX_BACKUP_BYTES:
                raise ValidationError("已超過單一備份 512 MB 或 4,998 張相片上限，請先分批歸檔")
        raw_snapshot = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        files["archive.json"] = raw_snapshot
        manifest = {"format": "family-health-backup", "version": 1, "created_at": now(), "files": {name: {"size": len(raw), "sha256": digest(raw)} for name, raw in files.items()}}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
            for name, raw in files.items():
                bundle.writestr(name, raw)
        return stream.getvalue(), manifest

    def backup(self):
        with self.lock:
            raw, manifest = self._archive_bytes()
            name = "安康簿_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3) + ".zip"
            path = self.data_dir / "backups" / name
            temp = path.with_suffix(".tmp")
            temp.write_bytes(raw)
            os.replace(temp, path)
            data = self.data()
            result = {"name": name, "path": str(path), "created_at": manifest["created_at"], "excluded_pending": sum(not a.get("privacy_checked") for a in data["attachments"]), "excluded_drafts": sum(v["status"] != "confirmed" for v in data["visits"] + data["reports"])}
            self.set_metadata("last_backup", result)
            return result

    def backup_path(self, name):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".zip"):
            raise ValidationError("備份檔案名稱錯誤")
        path = (self.data_dir / "backups" / name).resolve()
        if not path.is_relative_to((self.data_dir / "backups").resolve()) or not path.is_file():
            raise ValidationError("找不到備份")
        return path

    def _validate_backup(self, raw):
        if len(raw) > MAX_BACKUP_BYTES:
            raise ValidationError("備份超過 512 MB")
        try:
            bundle = zipfile.ZipFile(io.BytesIO(raw))
            infos = bundle.infolist()
            if len(infos) > MAX_BACKUP_FILES or len({i.filename for i in infos}) != len(infos):
                raise ValidationError("備份檔案數量過多或包含重複檔名")
            total = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                if info.is_dir() or path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValidationError("備份包含不安全的檔案路徑")
                total += info.file_size
                if total > MAX_BACKUP_BYTES or info.file_size > MAX_IMAGE_BYTES * 2:
                    raise ValidationError("備份解壓大小超過限制")
            manifest = json.loads(bundle.read("manifest.json"))
            if manifest.get("format") != "family-health-backup" or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
                raise ValidationError("不是支援的安康簿備份")
            if set(bundle.namelist()) != {"manifest.json", *manifest["files"]}:
                raise ValidationError("備份清單與檔案不符")
            files = {}
            for name, meta in manifest["files"].items():
                content = bundle.read(name)
                if len(content) != meta.get("size") or digest(content) != meta.get("sha256"):
                    raise ValidationError("備份檢查碼不符，檔案可能損壞")
                files[name] = content
            snapshot = json.loads(files["archive.json"])
            if snapshot.get("schema_version") != 1 or not all(isinstance(snapshot.get(k), list) for k in ("patients", "visits", "attachments")):
                raise ValidationError("備份資料格式錯誤")
            snapshot.setdefault("reports", [])
            if not isinstance(snapshot["reports"], list):
                raise ValidationError("備份報告格式錯誤")
            attachment_ids = set()
            expected_files = {"archive.json"}
            for item in snapshot["attachments"]:
                item_id = identifier(item.get("id"), "a_")
                if item_id in attachment_ids or not item.get("privacy_checked") or item.get("patient_id") not in PATIENTS:
                    raise ValidationError("備份包含未核對相片或重複紀錄")
                attachment_ids.add(item_id)
                extension = {"image/png": ".png", "application/pdf": ".pdf"}.get(item.get("mime"))
                if extension is None:
                    raise ValidationError("備份來源格式不支援")
                media_name = "media/" + item_id + extension
                expected_files.add(media_name)
                content = files.get(media_name)
                if content is None or digest(content) != item.get("sha256") or len(content) != item.get("size"):
                    raise ValidationError("備份相片不完整")
                mime, _ = inspect_image(content)
                if mime != item["mime"]:
                    raise ValidationError("備份相片格式錯誤")
                if mime == "application/pdf" and item.get("pages") != pdf.info(content)["pages"]:
                    raise ValidationError("備份 PDF 頁數與清單不符")
            needed = set()
            for table in ("visits", "reports"):
                record_ids = set()
                for record in snapshot[table]:
                    record_id = record.get("id")
                    if not isinstance(record_id, str) or not SAFE_ID.fullmatch(record_id) or record_id in record_ids:
                        raise ValidationError("備份紀錄編號錯誤或重複")
                    record_ids.add(record_id)
                    needed.update(self._all_source_ids(record))
            if needed != attachment_ids:
                raise ValidationError("備份來源與紀錄連結不符")
            if set(files) != expected_files:
                raise ValidationError("備份包含未識別資料")
            return snapshot, manifest, files
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError, TypeError, AttributeError) as exc:
            raise ValidationError("備份無法讀取或已損壞") from exc

    def restore_preview(self, raw):
        snapshot, manifest, files = self._validate_backup(raw)
        token = secrets.token_urlsafe(24)
        path = self.data_dir / "restore" / (token + ".zip")
        path.write_bytes(raw)
        with self.lock:
            for old_token, old in list(self.restore_tokens.items()):
                if old["expires"] < time.time():
                    Path(old["path"]).unlink(missing_ok=True)
                    self.restore_tokens.pop(old_token)
            self.restore_tokens[token] = {"path": path, "expires": time.time() + 600, "sha256": digest(raw)}
        return {"token": token, "visits": len(snapshot["visits"]), "reports": len(snapshot["reports"]), "attachments": len(snapshot["attachments"]), "created_at": manifest["created_at"]}

    def restore_apply(self, token):
        with self.lock:
            cached = self.restore_tokens.pop(token, None)
            if not cached or cached["expires"] < time.time():
                raise ValidationError("還原預覽已過期，請重新選擇備份")
            raw = cached["path"].read_bytes()
            if digest(raw) != cached["sha256"]:
                raise ValidationError("備份在預覽後有變動，請重新選擇")
            snapshot, manifest, files = self._validate_backup(raw)
            # Validate in an isolated database before touching the live archive.
            check_dir = self.data_dir / "restore" / ("check_" + secrets.token_hex(8))
            test_store = HealthStore(check_dir)
            try:
                for patient in snapshot["patients"]:
                    test_store.update_patient(patient)
                for attachment in snapshot["attachments"]:
                    is_pdf = attachment["mime"] == "application/pdf"
                    extension = ".pdf" if is_pdf else ".png"
                    clean = {"id": attachment["id"], "patient_id": attachment["patient_id"], "path": "archive/" + attachment["id"] + extension, "original_name": "", "ocr_text": "", "archive_name": attachment["id"] + extension, "mime": attachment["mime"], "status": "linked", "privacy_checked": True, "privacy_reviewed_at": now(), "imported_at": text(attachment.get("imported_at"), 80) or now(), "kind": "report" if attachment.get("kind") == "report" else "visit"}
                    # A backup may be valid yet produced by another program.
                    # Keep pixel content, but never restore source metadata.
                    incoming_bytes = files["media/" + attachment["id"] + extension]
                    pixels = pdf.clean_restored(incoming_bytes) if is_pdf else clean_image(incoming_bytes)
                    if is_pdf:
                        clean["pages"] = pdf.info(pixels)["pages"]
                    clean.update(sha256=digest(pixels), size=len(pixels))
                    (check_dir / clean["path"]).write_bytes(pixels)
                    test_store._put("attachments", clean)
                test_store.db.commit()
                visit_ids = set()
                for visit in snapshot["visits"]:
                    if visit.get("status") != "confirmed" or visit.get("privacy_reviewed") is not True or visit.get("id") in visit_ids:
                        raise ValidationError("備份包含未確認或重複求診紀錄")
                    visit_ids.add(visit.get("id"))
                    validated = test_store.upsert_visit(visit)
                    test_store.confirm_visit(validated["id"], reviewed=True)
                report_ids = set()
                for report in snapshot["reports"]:
                    if report.get("status") != "confirmed" or report.get("privacy_reviewed") is not True or report.get("id") in report_ids:
                        raise ValidationError("備份包含未確認或重複報告")
                    report_ids.add(report.get("id"))
                    validated = test_store.upsert_report(report)
                    test_store.confirm_report(validated["id"], reviewed=True)
                ready = test_store.confirmed_snapshot()
                pre_restore = self.backup()
                # All imported objects validated. Copy files to collision-free names first.
                replacements = []
                for attachment in ready["attachments"]:
                    generated_path = test_store.attachment_path(attachment["id"]).relative_to(test_store.data_dir)
                    destination = self.data_dir / generated_path
                    if destination.exists():
                        destination = destination.with_name(destination.stem + "_restored_" + secrets.token_hex(4) + destination.suffix)
                    if not destination.resolve().is_relative_to(self.data_dir / "archive"):
                        raise ValidationError("還原歸檔路徑不安全")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(test_store.attachment_path(attachment["id"]), destination)
                    internal = test_store._get("attachments", attachment["id"])
                    replacements.append({**attachment, "path": destination.relative_to(self.data_dir).as_posix(), "archive_name": destination.name, "archive_owner_id": internal.get("archive_owner_id")})
                # Preserve local drafts and unreviewed images, even when restoring
                # an earlier version whose IDs overlap with a current draft.
                current = self.data()
                drafts = [v for v in current["visits"] if v["status"] != "confirmed"]
                draft_reports = [r for r in current["reports"] if r["status"] != "confirmed"]
                draft_sources = {fid for v in drafts + draft_reports for fid in self._all_source_ids(v)}
                confirmed_sources = {fid for v in current["visits"] + current["reports"] if v["status"] == "confirmed" for fid in self._all_source_ids(v)}
                incoming = {a["id"]: a for a in replacements}
                kept_attachments = []
                remap = {}
                for attachment in self._rows("attachments"):
                    old_id = attachment["id"]
                    if old_id not in draft_sources and old_id in confirmed_sources:
                        continue
                    if old_id in incoming:
                        if incoming[old_id]["sha256"] == attachment["sha256"] and incoming[old_id]["patient_id"] == attachment["patient_id"]:
                            continue
                        attachment["id"] = "a_local_" + uuid.uuid4().hex
                        remap[old_id] = attachment["id"]
                    kept_attachments.append(attachment)
                incoming_visit_ids = {v["id"] for v in ready["visits"]}
                for visit in drafts:
                    if visit["id"] in incoming_visit_ids:
                        visit["id"] = "v_local_" + uuid.uuid4().hex
                    visit["source_file_ids"] = [remap.get(fid, fid) for fid in visit["source_file_ids"]]
                    for med in visit["medicines"]:
                        med["source_file_ids"] = [remap.get(fid, fid) for fid in med["source_file_ids"]]
                incoming_report_ids = {r["id"] for r in ready["reports"]}
                for report in draft_reports:
                    if report["id"] in incoming_report_ids:
                        report["id"] = "r_local_" + uuid.uuid4().hex
                    report["source_file_ids"] = [remap.get(fid, fid) for fid in report["source_file_ids"]]
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    for table in ("visits", "reports", "attachments", "patients"):
                        self.db.execute(f"DELETE FROM {table}")
                    for patient in ready["patients"]:
                        self._put("patients", patient)
                    for attachment in replacements + kept_attachments:
                        self._put("attachments", attachment)
                    for visit in ready["visits"] + drafts:
                        self._put("visits", visit)
                    for report in ready["reports"] + draft_reports:
                        self._put("reports", report)
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                self.set_metadata("last_sync", None)
                cached["path"].unlink(missing_ok=True)
                return {"restored": True, "visits": len(ready["visits"]), "reports": len(ready["reports"]), "attachments": len(ready["attachments"]), "preserved_drafts": len(drafts) + len(draft_reports), "preserved_pending": len(kept_attachments), "pre_restore_backup": pre_restore["name"]}
            finally:
                test_store.close()
                if check_dir.is_relative_to(self.data_dir / "restore"):
                    shutil.rmtree(check_dir)

    def export_snapshot(self):
        with self.lock:
            snapshot = self.confirmed_snapshot()
            snapshot["exported_at"] = now()
            target = self.data_dir / "exports" / "confirmed-records.json"
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, target)
            return {"path": str(target), "visits": len(snapshot["visits"]), "reports": len(snapshot["reports"]), "attachments": len(snapshot["attachments"])}
