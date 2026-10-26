"""Read remote feedback history into a local immutable cache. Never writes cloud data."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from healthlib.errors import ValidationError
from tools.publish import load_config
from tools.share import ShareError, SupabaseShareClient

PAGE_SIZE = 100  # Worst-case 500-character notes fit the client's 256 KiB limit.
MAX_PAGES = 20
MAX_SECONDS = 30
FIELDS = "id,patient_id,visit_id,medicine_id,rating,notes,revision,created_at,event_no"


def refresh_feedback(store, *, client=None, config=None):
    """Failures retain the exact old cursor/cache; bounded catch-up stays stale."""
    with store.feedback_sync_lock:
        scope = None
        try:
            settings = load_config(store.data_dir) if config is None else config
            url = settings.get("url", "").rstrip("/")
            if settings.get("archive_id", "family") != "family" or not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url):
                raise ValidationError("尚未設定服藥回饋同步來源")
            scope = hashlib.sha256((url + "/family").encode()).hexdigest()
            state = store.feedback_sync_state()
            if state["scope"] not in (None, scope):
                raise ValidationError("回饋快取屬於另一個雲端來源，未作合併")
            reader = client or SupabaseShareClient(url, settings.get("service_key", ""))
            cursor = state["cursor"]
            events = []
            complete = False
            deadline = time.monotonic() + MAX_SECONDS
            for _ in range(MAX_PAGES):
                query = urlencode({"archive_id": "eq.family", "event_no": "gt." + str(cursor),
                                   "select": FIELDS, "order": "event_no.asc", "limit": str(PAGE_SIZE)})
                rows = reader._request("/rest/v1/family_medicine_feedback?" + query)
                if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                    raise ValidationError("服藥回饋服務回傳格式錯誤")
                for row in rows:
                    if not isinstance(row, dict) or type(row.get("event_no")) is not int or not cursor < row["event_no"] < 2**63:
                        raise ValidationError("服藥回饋事件次序不正確")
                    cursor = row["event_no"]
                    events.append(row)
                if len(rows) < PAGE_SIZE:
                    complete = True
                    break
                if time.monotonic() >= deadline:
                    break
            # Validation, immutable conflict detection, append and cursor advance
            # form one transaction. A failed page never commits earlier pages.
            store.import_feedback(events, scope, cursor, complete=complete)
            result = store.feedback_data(scope=scope)
            result["synced_events"] = len(events)
            if not complete:
                result["message"] = "較早回饋正在分批同步，請稍後重新整理；備份會保留已收到的歷史。"
            return result
        except (ValidationError, ShareError, OSError, ValueError, TypeError, AttributeError):
            store.mark_feedback_stale()
            result = store.feedback_data(scope=scope)
            result.update(stale=True, synced_events=0, message="暫時未能完成回饋同步；已保留本機歷史，最後同步時間如下。")
            return result
