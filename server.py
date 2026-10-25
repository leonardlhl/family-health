#!/usr/bin/env python3
"""Loopback-only owner app. The static web folder can also be hosted for readers."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from healthlib.store import HealthStore, ValidationError, PrivacyReviewRequired, decode_base64, MAX_BACKUP_BYTES

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
VERSION = "1.0.0"
APP_ID = "family-health"


def public_cloud_config(store):
    """Read only explicitly public settings; never return a service credential."""
    result = {"configured": False, "url": "", "anon_key": "", "archive_id": "family"}
    try:
        config_path = store.data_dir / "cloud.json"
        settings = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        if not isinstance(settings, dict):
            return result
        from tools.build_site import validate_public_config
        public = validate_public_config(os.environ.get("HEALTH_SUPABASE_URL", settings.get("url", "")).rstrip("/"), os.environ.get("HEALTH_SUPABASE_ANON_KEY", settings.get("anon_key", "")), os.environ.get("HEALTH_ARCHIVE_ID", settings.get("archive_id", "family")), require_cloud=True)
        return {"configured": True, "url": public["supabaseUrl"], "anon_key": public["supabaseAnonKey"], "archive_id": public["archiveId"]}
    except (ValueError, TypeError, AttributeError, OSError, ImportError):
        return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "FamilyHealth/1.0"

    def log_message(self, fmt, *args):
        # Request URLs can contain medical record identifiers. No access log.
        pass

    @property
    def store(self):
        return self.server.store

    def valid_host(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        return self.headers.get("Host", "").lower() in allowed

    def guard(self, mutate=False):
        if not self.valid_host():
            self.json_response({"error": "只允許本機存取"}, 403)
            return False
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if origin and origin not in allowed:
            self.json_response({"error": "拒絕其他網站的要求"}, 403)
            return False
        if mutate and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf_token):
            self.json_response({"error": "頁面已過期，請重新整理後再試"}, 403)
            return False
        return True

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def json_response(self, result, status=200):
        raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def file_response(self, path, mime=None, download=False):
        raw = path.read_bytes()
        self.send_response(200)
        self.common_headers()
        self.send_header("Content-Type", mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        if download:
            self.send_header("Content-Disposition", "attachment; filename=family-health-backup.zip")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValidationError("請使用 JSON 傳送資料")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("要求格式錯誤") from exc
        limit = (MAX_BACKUP_BYTES * 4 // 3 + 1000) if self.path.startswith("/api/restore/preview") else 36 * 1024 * 1024
        if size <= 0 or size > limit:
            raise ValidationError("要求內容過大或為空")
        try:
            body = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("資料格式錯誤") from exc
        if not isinstance(body, dict):
            raise ValidationError("資料格式錯誤")
        return body

    def do_GET(self):
        if not self.guard():
            return
        route = unquote(urlsplit(self.path).path)
        try:
            if route == "/api/bootstrap":
                imports = self.store.import_folders()
                return self.json_response({"app": APP_ID, "mode": "local", "csrf_token": self.server.csrf_token, "version": VERSION, "paths": {"inbox": str(self.store.data_dir / "inbox"), "archive": str(self.store.data_dir / "archive"), "backups": str(self.store.data_dir / "backups"), "import_folders": imports["folders"]}, "import_folder_warnings": imports["warnings"], "last_backup": self.store.metadata("last_backup"), "last_sync": self.store.metadata("last_sync"), "cloud": public_cloud_config(self.store)})
            if route == "/api/data":
                return self.json_response(self.store.data())
            if route == "/api/family-data":
                return self.json_response(self.store.family_data())
            if route == "/api/queue":
                return self.json_response(self.store.queue())
            if route.startswith("/api/files/") and route.endswith("/thumbnail"):
                file_id = route.removeprefix("/api/files/").removesuffix("/thumbnail")
                return self.file_response(self.store.thumbnail_path(file_id), "image/jpeg")
            if route.startswith("/api/files/"):
                file_id = route.removeprefix("/api/files/")
                item = self.store.attachment(file_id)
                return self.file_response(self.store.attachment_path(file_id), item["mime"])
            if route.startswith("/api/backups/"):
                return self.file_response(self.store.backup_path(route.removeprefix("/api/backups/")), "application/zip", download=True)
            if route.startswith("/api/"):
                return self.json_response({"error": "找不到功能"}, 404)
            path = (WEB / (route.lstrip("/") or "index.html")).resolve()
            if not path.is_relative_to(WEB.resolve()) or not path.is_file() or path.is_symlink():
                return self.json_response({"error": "找不到頁面"}, 404)
            return self.file_response(path)
        except PrivacyReviewRequired as exc:
            self.json_response({"error": str(exc)}, 403)
        except ValidationError as exc:
            self.json_response({"error": str(exc)}, 400)
        except OSError:
            self.json_response({"error": "檔案暫時無法讀取，請稍後再試"}, 500)

    def do_POST(self):
        if not self.guard(mutate=True):
            return
        route = unquote(urlsplit(self.path).path)
        try:
            value = self.read_json()
            if route == "/api/scan":
                result = self.store.scan()
            elif route == "/api/upload":
                result = self.store.upload(value.get("patient_id"), value.get("name", ""), decode_base64(value.get("content_base64")))
            elif route == "/api/patients":
                result = self.store.update_patient(value)
            elif route == "/api/visits":
                result = self.store.upsert_visit(value)
            elif route.startswith("/api/visits/") and route.endswith("/confirm"):
                result = self.store.confirm_visit(route.split("/")[3], value.get("reviewed"))
            elif route.startswith("/api/appointments/"):
                result = self.store.appointment_status(route.removeprefix("/api/appointments/"), value.get("status"))
            elif route.startswith("/api/files/") and route.endswith("/sanitize"):
                result = self.store.sanitize_attachment(route.split("/")[3], decode_base64(value.get("content_base64")), value.get("privacy_checked"))
            elif route.startswith("/api/files/") and route.endswith("/privacy"):
                result = self.store.review_attachment(route.split("/")[3], value.get("reviewed"))
            elif route == "/api/backup":
                result = self.store.backup()
            elif route == "/api/restore/preview":
                result = self.store.restore_preview(decode_base64(value.get("content_base64"), MAX_BACKUP_BYTES))
            elif route == "/api/restore/apply":
                result = self.store.restore_apply(value.get("token"))
            elif route == "/api/export":
                result = self.store.export_snapshot()
            else:
                return self.json_response({"error": "找不到功能"}, 404)
            self.json_response(result)
        except ValidationError as exc:
            self.json_response({"error": str(exc)}, 400)
        except (OSError, sqlite3.Error):
            self.json_response({"error": "資料未能儲存，請檢查磁碟空間或重新啟動程式"}, 500)

    def do_OPTIONS(self):
        self.json_response({"error": "不允許跨網站存取"}, 403)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="安康簿本機管理")
    parser.add_argument("--data-dir", default=str(ROOT / "private"))
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    store = HealthStore(args.data_dir)
    server = LocalServer(("127.0.0.1", args.port), store)
    print(f"安康簿: http://127.0.0.1:{server.server_port}", flush=True)
    print("只允許本機管理；家人網站須另外設定登入及同步。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
