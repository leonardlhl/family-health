"""Immutable safe feedback values; clinical publication never includes this cache."""
from datetime import datetime, timezone
import re
import uuid

from .errors import ValidationError

RATINGS = {"effective", "ineffective", "undecided"}
ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
SCOPE = re.compile(r"^[a-f0-9]{64}$")


def timestamp(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise ValidationError("服藥回饋時間格式錯誤")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValidationError("服藥回饋時間格式錯誤") from None


def event(value):
    if not isinstance(value, dict):
        raise ValidationError("服藥回饋格式錯誤")
    try:
        item_id = str(uuid.UUID(value.get("id")))
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("服藥回饋編號格式錯誤") from None
    result = {"id": item_id}
    if value.get("patient_id") not in {"grandpa", "grandma"} or value.get("rating") not in RATINGS:
        raise ValidationError("服藥回饋人物或評價格式錯誤")
    for field in ("visit_id", "medicine_id"):
        if not isinstance(value.get(field), str) or not ID.fullmatch(value[field]):
            raise ValidationError("服藥回饋紀錄連結格式錯誤")
        result[field] = value[field]
    notes = value.get("notes", "")
    revision = value.get("revision")
    if not isinstance(notes, str) or len(notes) > 500 or type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise ValidationError("服藥回饋備註最多 500 字，版本須為正整數")
    result.update(patient_id=value["patient_id"], rating=value["rating"], notes=notes,
                  revision=revision, created_at=timestamp(value.get("created_at")))
    return result


def sync_state(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValidationError("服藥回饋同步資料格式錯誤")
    scope = value.get("scope")
    cursor = value.get("cursor", 0)
    if (scope is not None and (not isinstance(scope, str) or not SCOPE.fullmatch(scope))) or type(cursor) is not int or not 0 <= cursor < 2**63:
        raise ValidationError("服藥回饋同步來源格式錯誤")
    return {"scope": scope, "cursor": cursor, "last_sync": timestamp(value.get("last_sync"), optional=True), "stale": value.get("stale", True) is not False}


def revision_key(item):
    return item["patient_id"], item["visit_id"], item["medicine_id"], item["revision"]
