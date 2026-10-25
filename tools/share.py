"""Manage the owner's no-login family capability link.

Plain tokens exist only in private/family-share.json. Remote operations send
their SHA-256 digest, never the token. This module does not publish records.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAMILY_URL = "https://leonardlhl.github.io/family-health/"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
ARCHIVE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ShareError(ValueError):
    """Safe to show to the owner: messages never include secrets or response bodies."""


def _parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise ShareError("分享服務回傳格式不正確，請稍後重試。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise ShareError("分享服務回傳時間格式不正確，請稍後重試。") from None


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ShareError("本機分享設定格式錯誤；請保留檔案並檢查設定。") from None


def token_hash(token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ShareError("本機分享連結格式錯誤；請保留檔案並檢查設定。")
    try:
        decoded = base64.urlsafe_b64decode(token + "=")
    except ValueError:
        raise ShareError("本機分享連結格式錯誤；請保留檔案並檢查設定。") from None
    if len(decoded) != 32:
        raise ShareError("本機分享連結格式錯誤；請保留檔案並檢查設定。")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # A service credential must never follow a redirect to another host.
        return None


class SupabaseShareClient:
    def __init__(self, url, service_key, opener=None):
        if not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url):
            raise ShareError("請先設定私人資料服務的 HTTPS 網址。")
        if not isinstance(service_key, str) or not service_key:
            raise ShareError("請先在本機設定分享管理憑證。")
        if not re.fullmatch(r"sb_secret_[A-Za-z0-9_-]+", service_key):
            try:
                part = service_key.split(".")[1]
                payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                if payload.get("role") != "service_role":
                    raise ValueError()
            except (ValueError, IndexError, UnicodeDecodeError, AttributeError):
                raise ShareError("分享管理需要保存在本機的服務憑證。") from None
        self.url = url
        self.service_key = service_key
        self.opener = opener or build_opener(_NoRedirect())

    def _request(self, path, payload=None):
        headers = {"apikey": self.service_key, "Accept": "application/json", "Content-Type": "application/json"}
        if self.service_key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + self.service_key
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(self.url + path, data=data, headers=headers, method="POST" if payload is not None else "GET")
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ShareError("分享服務回應過大，未能確認結果。")
            return json.loads(raw)
        except HTTPError as error:
            # Never read/echo the response body: it could contain credentials.
            raise ShareError(f"分享服務未能完成要求（HTTP {error.code}）；請重新檢查狀態。") from None
        except (URLError, OSError, TimeoutError):
            raise ShareError("暫時無法連接分享服務；請稍後重新檢查狀態。") from None
        except (ValueError, UnicodeDecodeError):
            raise ShareError("分享服務回傳格式不正確，未能確認結果。") from None

    def get_link(self, archive_id, link_id):
        query = urlencode({"archive_id": "eq." + archive_id, "id": "eq." + link_id,
                           "select": "id,archive_id,token_hash,created_at,revoked_at,expires_at", "limit": "2"})
        rows = self._request("/rest/v1/family_share_links?" + query)
        if not isinstance(rows, list) or len(rows) > 1:
            raise ShareError("分享服務回傳格式不正確，未能確認結果。")
        return rows[0] if rows else None

    def rotate(self, archive_id, previous_id, candidate):
        return self._request("/rest/v1/rpc/rotate_family_share_link", {
            "p_archive_id": archive_id, "p_previous_id": previous_id,
            "p_id": candidate["id"], "p_token_hash": candidate["token_hash"],
        })

    def revoke(self, archive_id, current):
        return self._request("/rest/v1/rpc/revoke_family_share_link", {
            "p_archive_id": archive_id, "p_id": current["id"],
            "p_token_hash": current["token_hash"],
        })


class ShareManager:
    def __init__(self, data_dir, client=None, config=None):
        self.data_dir = Path(data_dir).resolve()
        self.state_path = self.data_dir / "family-share.json"
        self.lock_path = self.data_dir / "family-share.lock"
        self.client = client
        self.config_error = None
        if config is None:
            try:
                from tools.publish import load_config
                config = load_config(self.data_dir)
            except (ValueError, OSError, TypeError, AttributeError):
                config = {}
                self.config_error = "本機雲端設定無法讀取，請先檢查設定。"
        self.project_url = config.get("url", "")
        self.archive_id = config.get("archive_id", "family")
        if not isinstance(self.archive_id, str) or not ARCHIVE_PATTERN.fullmatch(self.archive_id):
            raise ShareError("分享檔案庫設定格式錯誤。")
        if not isinstance(self.project_url, str):
            raise ShareError("分享服務網址設定格式錯誤。")
        self.project_url = self.project_url.rstrip("/")
        if client is None and self.project_url and config.get("service_key"):
            try:
                self.client = SupabaseShareClient(self.project_url, config["service_key"])
            except ShareError as error:
                self.config_error = str(error)

    @contextmanager
    def _locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_symlink() or self.state_path.is_symlink():
            raise ShareError("分享設定不可使用外部連結。")
        # OS-owned locks are released even if a process terminates unexpectedly.
        stream = self.lock_path.open("a+b")
        acquired = False
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 5
            while not acquired:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ShareError("另一個分享操作正在進行，請稍後重試。") from None
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _empty_state(self):
        return {"version": 1, "archive_id": self.archive_id, "project_url": self.project_url, "current": None, "pending": None}

    def _link(self, value):
        if not isinstance(value, dict):
            raise ShareError("本機分享設定格式錯誤；請保留檔案並檢查設定。")
        result = {"id": _uuid(value.get("id")), "token": value.get("token")}
        result["token_hash"] = token_hash(result["token"])
        if not isinstance(value.get("token_hash"), str) or not secrets.compare_digest(result["token_hash"], value["token_hash"]):
            raise ShareError("本機分享連結檢查碼不符；請保留檔案並檢查設定。")
        for field in ("created_at", "revoked_at", "expires_at"):
            _parse_timestamp(value.get(field))
            result[field] = value.get(field)
        return result

    def _load(self):
        if not self.state_path.exists():
            return self._empty_state()
        try:
            if self.state_path.stat().st_size > 65536:
                raise ValueError()
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError()
            if raw.get("project_url") != self.project_url or raw.get("archive_id") != self.archive_id:
                raise ShareError("分享設定屬於另一個檔案庫或雲端專案；未作任何更改。")
            state = self._empty_state()
            if raw.get("current"):
                state["current"] = self._link(raw["current"])
            pending = raw.get("pending")
            if pending:
                if not isinstance(pending, dict) or pending.get("action") not in {"create", "rotate", "revoke"}:
                    raise ValueError()
                state["pending"] = {"action": pending["action"], "previous_id": _uuid(pending["previous_id"]) if pending.get("previous_id") else None}
                if pending["action"] == "revoke":
                    if not state["current"]:
                        raise ValueError()
                else:
                    state["pending"]["candidate"] = self._link(pending.get("candidate"))
                if state["pending"]["previous_id"] and (not state["current"] or state["pending"]["previous_id"] != state["current"]["id"]):
                    raise ValueError()
            return state
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, ShareError):
                raise
            raise ShareError("本機分享設定無法讀取；請保留檔案並檢查設定。") from None

    def _save(self, state):
        temporary = self.state_path.with_name("family-share." + secrets.token_hex(8) + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        except OSError:
            raise ShareError("本機分享設定未能儲存；請檢查磁碟並重新讀取分享狀態。") from None
        finally:
            temporary.unlink(missing_ok=True)

    def _remote(self, link):
        row = self.client.get_link(self.archive_id, link["id"])
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("id") != link["id"] or row.get("archive_id") != self.archive_id or not isinstance(row.get("token_hash"), str) or not secrets.compare_digest(row["token_hash"], link["token_hash"]):
            raise ShareError("分享服務紀錄不符，未作任何更改。")
        result = {"id": row["id"], "archive_id": row["archive_id"]}
        for field in ("created_at", "revoked_at", "expires_at"):
            _parse_timestamp(row.get(field))
            result[field] = row.get(field)
        if not result["created_at"]:
            raise ShareError("分享服務紀錄不完整，未能確認狀態。")
        return result

    @staticmethod
    def _active(row):
        return bool(row and row["revoked_at"] is None and (row["expires_at"] is None or _parse_timestamp(row["expires_at"]) > datetime.now(timezone.utc)))

    def _response(self, current, row, verified=True):
        result = {"configured": self.client is not None, "status": "not_created", "url": None,
                  "created_at": None, "revoked_at": None, "verified": verified}
        if current is None:
            return result
        result["created_at"] = row["created_at"] if row else current.get("created_at")
        if not row or row["revoked_at"]:
            result["status"] = "revoked"
            result["revoked_at"] = row["revoked_at"] if row else None
        elif not self._active(row):
            result["status"] = "expired"
        else:
            result["status"] = "active"
            result["url"] = FAMILY_URL + "#share=" + current["token"]
        return result

    def _update_current(self, state, link, row):
        current = dict(link)
        for field in ("created_at", "revoked_at", "expires_at"):
            current[field] = row.get(field) if row else current.get(field)
        state["current"] = current
        state["pending"] = None
        self._save(state)
        return current

    def _recover(self, state):
        """Reconcile an uncertain prior operation using exact remote row reads."""
        pending = state["pending"]
        if not pending:
            return state
        current = state["current"]
        if pending["action"] == "revoke":
            row = self._remote(current)
            if row is None or row["revoked_at"] is not None:
                self._update_current(state, current, row)
            return state
        candidate = pending["candidate"]
        row = self._remote(candidate)
        if row:
            if pending["previous_id"]:
                prior = self._remote(current)
                if prior and prior["revoked_at"] is None:
                    raise ShareError("分享切換尚未能核實；請稍後重新讀取狀態。")
            self._update_current(state, candidate, row)
        # A timed-out RPC might still commit after this read. Never discard its
        # candidate until the outcome is verified. An explicit retry reuses it.
        return state

    def _pending_response(self, state):
        current = state["current"]
        result = self._response(current, self._remote(current) if current else None)
        if state["pending"]:
            result["pending_action"] = state["pending"]["action"]
            result["message"] = "上次操作仍待核實；可重試同一操作以完成確認。"
            if current is None:
                result.update(status="unavailable", verified=False)
        return result

    def _finish_pending(self, state):
        pending = state["pending"]
        current = state["current"]
        try:
            if pending["action"] == "revoke":
                self.client.revoke(self.archive_id, {key: current[key] for key in ("id", "token_hash")})
            else:
                candidate = pending["candidate"]
                self.client.rotate(self.archive_id, pending["previous_id"], {key: candidate[key] for key in ("id", "token_hash")})
        except ShareError:
            pass  # A timeout may occur after commit; read back exact rows.
        state = self._recover(state)
        if state["pending"]:
            raise ShareError("操作尚未能確認；已保留本機連結，請重新檢查狀態或重試同一操作。")
        return self._pending_response(state)

    def status(self):
        if self.client is None:
            return {"configured": False, "status": "not_created", "url": None, "created_at": None, "revoked_at": None, "verified": False,
                    "message": self.config_error or "請先完成私人資料服務設定。"}
        try:
            with self._locked():
                state = self._recover(self._load())
                return self._pending_response(state)
        except ShareError as error:
            return {"configured": True, "status": "unavailable", "url": None, "created_at": None, "revoked_at": None, "verified": False, "message": str(error)}

    def _require_client(self):
        if self.client is None:
            raise ShareError(self.config_error or "請先完成私人資料服務設定。")

    def create(self, rotate=False):
        self._require_client()
        with self._locked():
            state = self._load()
            previous_pending = state["pending"]
            state = self._recover(state)
            if state["pending"]:
                if state["pending"]["action"] == "revoke":
                    raise ShareError("上次停用仍待核實；請先重試停用操作。")
                return self._finish_pending(state)
            if previous_pending and previous_pending["action"] in {"create", "rotate"}:
                return self._pending_response(state)
            current = state["current"]
            previous = self._remote(current) if current else None
            if not rotate and self._active(previous):
                return self._response(current, previous)
            if rotate and current is None:
                raise ShareError("請先建立家人連結，再使用更換連結。")
            candidate_token = secrets.token_urlsafe(32)
            candidate = {"id": str(uuid.uuid4()), "token": candidate_token, "token_hash": token_hash(candidate_token),
                         "created_at": None, "revoked_at": None, "expires_at": None}
            previous_id = current["id"] if previous and previous["revoked_at"] is None else None
            state["pending"] = {"action": "rotate" if rotate else "create", "previous_id": previous_id, "candidate": candidate}
            # Persist both old and new token before changing the remote pointer.
            self._save(state)
            return self._finish_pending(state)

    def rotate(self):
        return self.create(rotate=True)

    def revoke(self):
        self._require_client()
        with self._locked():
            state = self._load()
            previous_pending = state["pending"]
            state = self._recover(state)
            if state["pending"]:
                if state["pending"]["action"] != "revoke":
                    raise ShareError("上次建立或更換仍待核實；請先重試該操作，再停用連結。")
                return self._finish_pending(state)
            if previous_pending and previous_pending["action"] == "revoke":
                return self._pending_response(state)
            current = state["current"]
            if current is None:
                return self._response(None, None)
            previous = self._remote(current)
            if previous is None or previous["revoked_at"] is not None:
                return self._response(current, previous)
            state["pending"] = {"action": "revoke", "previous_id": current["id"]}
            self._save(state)
            return self._finish_pending(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage a private family link without publishing medical records.")
    parser.add_argument("action", choices=("status", "create", "rotate", "revoke"))
    parser.add_argument("--data-dir", default=os.environ.get("HEALTH_DATA_DIR", str(ROOT / "private")))
    parser.add_argument("--show-link", action="store_true", help="Explicitly print the active bearer URL for owner handoff")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        manager = ShareManager(args.data_dir)
        result = getattr(manager, args.action)()
        if not args.show_link:
            result.pop("url", None)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "unavailable" else 1
    except (ShareError, OSError):
        print("分享操作未完成。請在本機操作版重新檢查分享狀態及設定。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
