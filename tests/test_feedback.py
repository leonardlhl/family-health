"""Feedback histories are synthetic; no real cloud credentials or records used."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile

from healthlib.store import HealthStore, ValidationError
from server import LocalServer
from tools import feedback
from tools.share import ShareError

CONFIG = {"url": "https://synthetic-feedback.supabase.co", "archive_id": "family", "service_key": "synthetic-not-used"}
SCOPE = hashlib.sha256((CONFIG["url"] + "/family").encode()).hexdigest()


class Remote:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.fail_after = None

    def _request(self, path):
        self.calls.append(path)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise ShareError("Synthetic unavailable")
        query = parse_qs(urlsplit(path).query)
        if query["archive_id"] != ["eq.family"]:
            raise AssertionError("Unscoped history request")
        cursor = int(query["event_no"][0].removeprefix("gt."))
        limit = int(query["limit"][0])
        return copy.deepcopy([row for row in self.rows if row["event_no"] > cursor][:limit])


def change_backup(raw, callback):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    snapshot = json.loads(files["archive.json"])
    callback(snapshot)
    files["archive.json"] = json.dumps(snapshot).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["archive.json"] = {"size": len(files["archive.json"]), "sha256": hashlib.sha256(files["archive.json"]).hexdigest()}
    files["manifest.json"] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="feedback-", dir=root)
        self.store = HealthStore(self.temp.name)
        visit = self.store.upsert_visit({"patient_id": "grandpa", "date": "2026-10-26", "doctor": "測試醫生", "medicines": [{"name": "TEST ONLY", "strength": "1 unit", "dose": "test", "frequency": "test", "instructions": "test"}]})
        self.visit = self.store.confirm_visit(visit["id"], True)
        self.med = self.visit["medicines"][0]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, revision=1, **changes):
        value = {"id": str(uuid.uuid4()), "patient_id": "grandpa", "visit_id": self.visit["id"], "medicine_id": self.med["id"], "rating": "effective", "notes": "合成測試觀察", "revision": revision, "created_at": f"2026-10-26T00:00:{revision % 60:02d}Z", "event_no": revision, "actor_scope": "DO_NOT_EXPORT_ACTOR", "request_id": str(uuid.uuid4())}
        value.update(changes)
        return value

    def sync(self, rows):
        return feedback.refresh_feedback(self.store, client=Remote(rows), config=CONFIG)

    def backup(self):
        return self.store.backup(refresh_feedback=False)

    def test_latest_revision_and_whitelisted_history_are_separate_from_clinical_snapshot(self):
        events = [self.event(1), self.event(2, rating="ineffective")]
        result = self.sync(events)
        self.assertFalse(result["stale"])
        self.assertEqual(result["items"][0]["rating"], "ineffective")
        self.assertEqual(result["items"][0]["revision"], 2)
        self.assertEqual(result["cached_events"], 2)
        self.assertNotIn("actor_scope", result["items"][0])
        self.assertNotIn("request_id", result["items"][0])
        self.assertNotIn("event_no", result["items"][0])
        self.assertNotIn("medicine_feedback", self.store.confirmed_snapshot())
        self.assertNotIn("medicine_feedback", self.store.family_data())
        with zipfile.ZipFile(self.backup()["path"]) as archive:
            snapshot = json.loads(archive.read("archive.json"))
        self.assertEqual(len(snapshot["medicine_feedback"]), 2)
        self.assertNotIn("DO_NOT_EXPORT_ACTOR", json.dumps(snapshot))
        self.assertEqual(snapshot["feedback_sync"]["cursor"], 2)

    def test_repeated_and_concurrent_sync_is_idempotent(self):
        remote = Remote([self.event()])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: feedback.refresh_feedback(self.store, client=remote, config=CONFIG), range(4)))
        self.assertTrue(all(not result["stale"] for result in results))
        self.assertEqual(self.store.feedback_data()["cached_events"], 1)
        self.assertEqual(self.store.feedback_sync_state()["cursor"], 1)
        self.assertEqual(len({item["id"] for item in self.store._rows("medicine_feedback")}), 1)

    def test_page_failure_discards_whole_batch_and_preserves_previous_cursor_and_time(self):
        self.sync([self.event(1)])
        before = self.store.feedback_sync_state()
        remote = Remote([self.event(2), self.event(3)])
        remote.fail_after = 1
        with patch.object(feedback, "PAGE_SIZE", 1):
            result = feedback.refresh_feedback(self.store, client=remote, config=CONFIG)
        self.assertTrue(result["stale"])
        self.assertEqual(result["cached_events"], 1)
        self.assertEqual(self.store.feedback_sync_state()["cursor"], before["cursor"])
        self.assertEqual(result["last_sync"], before["last_sync"])

    def test_bounded_prefix_stays_stale_and_next_sync_catches_up(self):
        remote = Remote([self.event(n) for n in range(1, 4)])
        with patch.object(feedback, "PAGE_SIZE", 1), patch.object(feedback, "MAX_PAGES", 2):
            result = feedback.refresh_feedback(self.store, client=remote, config=CONFIG)
        self.assertTrue(result["stale"])
        self.assertIsNone(result["last_sync"])
        self.assertEqual(result["cached_events"], 2)
        self.assertEqual(self.store.feedback_sync_state()["cursor"], 2)
        result = feedback.refresh_feedback(self.store, client=remote, config=CONFIG)
        self.assertFalse(result["stale"])
        self.assertEqual(result["items"][0]["revision"], 3)

    def test_malformed_or_immutable_conflicting_event_never_overwrites_history(self):
        original = self.event()
        self.sync([original])
        before = self.store._rows("medicine_feedback")
        conflicts = [{**original, "notes": "changed"}, self.event(1), self.event(2, rating="not-a-rating"), self.event(2, notes="a" * 501), {**self.event(2), "revision": True}, self.event(2, created_at="no timestamp")]
        for bad in conflicts:
            with self.assertRaises(ValidationError):
                self.store.import_feedback([bad], SCOPE, 2, complete=True)
            self.assertEqual(self.store._rows("medicine_feedback"), before)
            self.assertEqual(self.store.feedback_sync_state()["cursor"], 1)

    def test_orphan_and_wrong_patient_history_retained_but_not_displayed(self):
        self.sync([self.event(1, patient_id="grandma"), self.event(2, medicine_id="m_removed")])
        self.assertEqual(self.store.feedback_data()["items"], [])
        with zipfile.ZipFile(self.backup()["path"]) as archive:
            self.assertEqual(len(json.loads(archive.read("archive.json"))["medicine_feedback"]), 2)

    def test_changed_prescription_gets_new_id_but_category_and_status_do_not(self):
        self.sync([self.event()])
        edited = copy.deepcopy(self.visit)
        edited["medicines"][0].update(category="分組", status="stopped")
        edited = self.store.upsert_visit(edited)
        self.assertEqual(edited["medicines"][0]["id"], self.med["id"])
        self.store.confirm_visit(edited["id"], True)
        self.assertEqual(len(self.store.feedback_data()["items"]), 1)
        for field in ("name", "strength", "dose", "frequency", "instructions"):
            current = self.store._get("visits", self.visit["id"])
            prior_id = current["medicines"][0]["id"]
            current["medicines"][0][field] += " changed"
            changed = self.store.upsert_visit(current)
            self.assertNotEqual(changed["medicines"][0]["id"], prior_id)
            self.store.confirm_visit(changed["id"], True)
        self.assertEqual(self.store.feedback_data()["items"], [])
        self.assertEqual(self.store.feedback_data()["cached_events"], 1)

    def test_duplicate_medicine_identity_cannot_create_ambiguous_feedback_binding(self):
        repeated = copy.deepcopy(self.visit)
        repeated["medicines"].append(copy.deepcopy(repeated["medicines"][0]))
        with self.assertRaises(ValidationError):
            self.store.upsert_visit(repeated)
        self.assertEqual(self.store._get("visits", self.visit["id"]), self.visit)

    def test_backup_refresh_failure_includes_cached_history_and_truthful_sync_state(self):
        self.sync([self.event()])
        last_sync = self.store.feedback_sync_state()["last_sync"]
        def unavailable(store):
            store.mark_feedback_stale()
            return store.feedback_data()
        with patch("tools.feedback.refresh_feedback", side_effect=unavailable) as refresh:
            result = self.store.backup()
        refresh.assert_called_once_with(self.store)
        self.assertTrue(result["feedback_stale"])
        self.assertEqual(result["last_feedback_sync"], last_sync)
        self.assertEqual(result["feedback_events"], 1)
        with zipfile.ZipFile(result["path"]) as archive:
            snapshot = json.loads(archive.read("archive.json"))
        self.assertTrue(snapshot["feedback_sync"]["stale"])
        self.assertEqual(len(snapshot["medicine_feedback"]), 1)

    def test_restore_merges_history_preserves_newer_events_and_never_trusts_backup_cursor(self):
        self.sync([self.event(1)])
        raw = Path(self.backup()["path"]).read_bytes()
        self.sync([self.event(2)])
        altered = change_backup(raw, lambda snapshot: snapshot["feedback_sync"].update(cursor=999999))
        preview = self.store.restore_preview(altered)
        result = self.store.restore_apply(preview["token"])
        self.assertEqual(result["feedback_events"], 2)
        self.assertTrue(result["feedback_stale"])
        self.assertEqual(self.store.feedback_sync_state()["cursor"], 2)
        self.assertEqual(self.store.feedback_data()["items"][0]["revision"], 2)

    def test_restore_rejects_conflicting_event_without_changing_existing_cache(self):
        self.sync([self.event()])
        raw = Path(self.backup()["path"]).read_bytes()
        altered = change_backup(raw, lambda snapshot: snapshot["medicine_feedback"][0].update(notes="conflict"))
        before = self.store._rows("medicine_feedback")
        preview = self.store.restore_preview(altered)
        with self.assertRaises(ValidationError):
            self.store.restore_apply(preview["token"])
        self.assertEqual(self.store._rows("medicine_feedback"), before)

    def test_legacy_backup_does_not_erase_current_feedback(self):
        self.sync([self.event()])
        raw = Path(self.backup()["path"]).read_bytes()
        def legacy(snapshot):
            snapshot.pop("medicine_feedback")
            snapshot.pop("feedback_sync")
        preview = self.store.restore_preview(change_backup(raw, legacy))
        self.store.restore_apply(preview["token"])
        self.assertEqual(self.store.feedback_data()["cached_events"], 1)

    def test_other_cloud_scope_is_not_merged_or_displayed(self):
        self.sync([self.event()])
        remote = Remote([self.event(2)])
        result = feedback.refresh_feedback(self.store, client=remote, config={**CONFIG, "url": "https://other-family.supabase.co"})
        self.assertTrue(result["stale"])
        self.assertEqual(result["items"], [])
        self.assertEqual(remote.calls, [])
        self.assertEqual(self.store.feedback_data()["cached_events"], 1)

    def test_second_database_connection_cannot_regress_cursor_or_mutate_event(self):
        self.sync([self.event(1), self.event(2)])
        other = HealthStore(self.temp.name)
        try:
            with self.assertRaises(ValidationError):
                other.import_feedback([], SCOPE, 1, complete=True)
            self.assertEqual(self.store.feedback_sync_state()["cursor"], 2)
            other.mark_feedback_stale()
            self.assertEqual(self.store.feedback_sync_state()["cursor"], 2)
        finally:
            other.close()


class FeedbackHTTPTests(unittest.TestCase):
    def test_feedback_get_needs_csrf_and_has_no_local_write_endpoint(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            store = HealthStore(temporary)
            server = LocalServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, csrf):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Content-Type": "application/json"}
                if csrf:
                    headers["X-CSRF-Token"] = server.csrf_token
                connection.request(method, "/api/medicine-feedback", "{}" if method == "POST" else None, headers)
                response = connection.getresponse()
                status = response.status
                response.read()
                connection.close()
                return status
            try:
                with patch("tools.feedback.refresh_feedback", return_value={"items": [], "stale": True, "last_sync": None}) as refresh:
                    self.assertEqual(request("GET", False), 403)
                    refresh.assert_not_called()
                    self.assertEqual(request("GET", True), 200)
                    self.assertEqual(request("POST", True), 404)
            finally:
                server.shutdown()
                server.server_close()
                store.close()


if __name__ == "__main__":
    unittest.main()
