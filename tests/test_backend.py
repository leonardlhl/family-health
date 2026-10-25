import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from PIL import Image, PngImagePlugin

from healthlib.store import HealthStore, ValidationError
from server import LocalServer


def picture(color="white", metadata=False):
    stream = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("patient_name", "DO NOT RETAIN THIS NAME")
    Image.new("RGB", (40, 30), color).save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = HealthStore(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def visit(self, attachment=None, **kwargs):
        value = {"patient_id": "grandpa", "date": "2026-10-25", "doctor": "陳醫生", "clinic": "門診", "diagnosis": "醫生原文", "medicines": [{"name": "藥袋文字", "status": "unknown"}], "source_file_ids": [attachment["id"]] if attachment else []}
        value.update(kwargs)
        return self.store.upsert_visit(value)

    def reviewed(self):
        attachment = self.store.upload("grandpa", "private-name.png", picture(metadata=True))
        self.store.review_attachment(attachment["id"], reviewed=True)
        visit = self.visit(attachment)
        return self.store.confirm_visit(visit["id"], reviewed=True), attachment

    def test_patient_identity_fields_not_persisted(self):
        patient = self.store.update_patient({"id": "grandma", "name": "Real Person", "age": 81, "sex": "female", "dob": "1945-10-25"})
        self.assertEqual(patient, {"id": "grandma", "name": "嬷嬷", "age": 81, "sex": "female"})
        for bad in (True, -1, 126, "81"):
            with self.assertRaises(ValidationError):
                self.store.update_patient({"id": "grandma", "age": bad})
        with self.assertRaises(ValidationError):
            self.visit(patient_name="Private Name")

    def test_duplicate_scan_keeps_same_attachment_after_redaction(self):
        raw = picture(metadata=True)
        path = Path(self.temp.name) / "inbox/grandpa/source.png"
        path.write_bytes(raw)
        self.assertEqual(self.store.scan()["imported"], 1)
        item = self.store.data()["attachments"][0]
        self.store.review_attachment(item["id"], reviewed=True)
        self.assertEqual(self.store.scan()["duplicates"], 1)
        self.assertEqual(len(self.store.data()["attachments"]), 1)
        self.assertTrue(path.exists(), "Never delete the user's inbox original silently")

    def test_external_import_folders_are_top_level_read_only_and_deduplicated(self):
        sources = Path(self.temp.name) / "external"
        sources.mkdir()
        (sources / "nested").mkdir()
        original = sources / "source.png"
        raw = picture(metadata=True)
        original.write_bytes(raw)
        (sources / "nested" / "must-not-scan.png").write_bytes(picture("blue"))
        config = [{"patient_id": "grandpa", "path": str(sources)}, {"patient_id": "grandpa", "path": str(sources)}]
        (Path(self.temp.name) / "import-folders.json").write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(len(self.store.import_folders()["folders"]), 1)
        first = self.store.scan()
        self.assertEqual(first["imported"], 1)
        self.assertEqual(self.store.scan()["duplicates"], 1)
        self.assertEqual(original.read_bytes(), raw)
        attachment = self.store.data()["attachments"][0]
        self.store.review_attachment(attachment["id"], reviewed=True)
        self.assertEqual(self.store.scan()["duplicates"], 1)
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(len(self.store.data()["attachments"]), 1)

    def test_invalid_import_folder_config_does_not_break_internal_inbox(self):
        settings = [{"patient_id": "stranger", "path": self.temp.name}, {"patient_id": "grandpa", "path": "relative/path"}, {"patient_id": "grandma", "path": str(Path(self.temp.name) / "missing")}]
        (Path(self.temp.name) / "import-folders.json").write_text(json.dumps(settings), encoding="utf-8")
        (Path(self.temp.name) / "inbox/grandpa/source.png").write_bytes(picture())
        result = self.store.scan()
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(result["warnings"]), 3)
        (Path(self.temp.name) / "import-folders.json").write_text("not-json", encoding="utf-8")
        self.assertEqual(len(self.store.import_folders()["warnings"]), 1)

    def test_pending_source_and_uncertainty_block_confirmation(self):
        item = self.store.upload("grandpa", "source.png", picture())
        visit = self.visit(item)
        with self.assertRaises(ValidationError):
            self.store.confirm_visit(visit["id"], reviewed=True)
        self.store.review_attachment(item["id"], reviewed=True)
        visit["review_notes"] = "藥名不清楚"
        visit = self.store.upsert_visit(visit)
        with self.assertRaises(ValidationError):
            self.store.confirm_visit(visit["id"], reviewed=True)
        visit["review_notes"] = ""
        visit = self.store.upsert_visit(visit)
        with self.assertRaises(ValidationError):
            self.store.confirm_visit(visit["id"], reviewed=False)
        confirmed = self.store.confirm_visit(visit["id"], reviewed=True)
        self.assertEqual(confirmed["medicines"][0]["status"], "unknown")
        self.assertEqual(confirmed["diagnosis"], "醫生原文")

    def test_invalid_dates_and_cross_person_sources(self):
        with self.assertRaises(ValidationError):
            self.visit(date="2026-02-30")
        item = self.store.upload("grandma", "source.png", picture())
        with self.assertRaises(ValidationError):
            self.visit(item)
        with self.assertRaises(ValidationError):
            self.visit(appointment={"date": "2026-11-01", "time": "25:30"})

    def test_sanitize_discards_original_pixels_metadata_and_filename(self):
        original = self.store.upload("grandpa", "patient_name.png", picture(metadata=True))
        old_path = self.store.attachment_path(original["id"])
        changed = self.store.sanitize_attachment(original["id"], picture("black"), reviewed=True)
        self.assertFalse(old_path.exists())
        self.assertEqual(changed["original_name"], "")
        self.assertEqual(changed["ocr_text"], "")
        with Image.open(self.store.attachment_path(original["id"])) as image:
            self.assertEqual(image.info, {})
            self.assertEqual(image.getpixel((1, 1)), (0, 0, 0))

    def test_edit_invalidates_confirmed_visit_and_source_revision(self):
        visit, attachment = self.reviewed()
        self.assertEqual(len(self.store.confirmed_snapshot()["visits"]), 1)
        visit["diagnosis"] = "更正"
        draft = self.store.upsert_visit(visit)
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["privacy_reviewed"])
        self.assertEqual(self.store.confirmed_snapshot()["visits"], [])
        self.store.confirm_visit(draft["id"], reviewed=True)
        self.store.sanitize_attachment(attachment["id"], picture("black"), reviewed=True)
        self.assertEqual(self.store.confirmed_snapshot()["visits"], [])

    def test_backup_has_only_confirmed_anonymized_records(self):
        visit, attachment = self.reviewed()
        pending = self.store.upload("grandma", "secret.png", picture("blue"))
        self.visit(date="2026-10-24", doctor="待核對")
        result = self.store.backup()
        self.assertEqual(result["excluded_pending"], 1)
        self.assertEqual(result["excluded_drafts"], 1)
        with zipfile.ZipFile(result["path"]) as bundle:
            data = json.loads(bundle.read("archive.json"))
            self.assertEqual(len(data["visits"]), 1)
            self.assertEqual(len(data["attachments"]), 1)
            self.assertNotIn(pending["id"], bundle.read("archive.json").decode())
            self.assertNotIn(b"DO NOT RETAIN", b"".join(bundle.read(n) for n in bundle.namelist()))
            self.assertTrue(all(not n.startswith(("inbox", "staging")) for n in bundle.namelist()))

    def test_restore_roundtrip_preserves_pending_local_work(self):
        visit, attachment = self.reviewed()
        backup = self.store.backup()
        visit["diagnosis"] = "本機新草稿"
        draft = self.store.upsert_visit(visit)
        pending = self.store.upload("grandma", "pending.png", picture("blue"))
        preview = self.store.restore_preview(Path(backup["path"]).read_bytes())
        restored = self.store.restore_apply(preview["token"])
        self.assertEqual(restored["preserved_drafts"], 1)
        data = self.store.data()
        self.assertEqual(len(data["visits"]), 2)
        self.assertTrue(any(v["diagnosis"] == "本機新草稿" for v in data["visits"]))
        self.assertTrue(self.store.attachment_path(pending["id"]).exists())
        self.assertEqual(len(self.store.confirmed_snapshot()["visits"]), 1)
        with self.assertRaises(ValidationError):
            self.store.restore_apply(preview["token"])

    def test_restore_rejects_traversal_duplicate_and_tampering(self):
        self.reviewed()
        original = Path(self.store.backup()["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            files = {n: source.read(n) for n in source.namelist()}
        for mutate in (lambda d: d.update({"../leak.png": picture()}), lambda d: d.update({"archive.json": b"{}"})):
            changed = dict(files)
            mutate(changed)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as output:
                for name, content in changed.items():
                    output.writestr(name, content)
            with self.assertRaises(ValidationError):
                self.store.restore_preview(stream.getvalue())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = HealthStore(self.temp.name)
        self.server = LocalServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        actual = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, json.dumps(body) if body is not None else None, actual)
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        return status, json.loads(data)

    def test_guard_blocks_foreign_origins_hosts_and_missing_csrf(self):
        self.assertEqual(self.request("GET", "/api/data", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/data", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/scan", {})[0], 403)
        status, bootstrap = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["app"], "family-health")
        status, result = self.request("POST", "/api/scan", {}, {"X-CSRF-Token": bootstrap["csrf_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"], 0)

    def test_bootstrap_never_exposes_cloud_service_key(self):
        (Path(self.temp.name) / "cloud.json").write_text(json.dumps({"url": "https://example-test.supabase.co", "anon_key": "sb_publishable_example_only", "service_key": "DO_NOT_DISCLOSE_THIS_SECRET", "archive_id": "family"}), encoding="utf-8")
        status, bootstrap = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(bootstrap["cloud"]["configured"])
        self.assertNotIn("DO_NOT_DISCLOSE", json.dumps(bootstrap))
        (Path(self.temp.name) / "cloud.json").write_text(json.dumps({"url": "https://example-test.supabase.co", "anon_key": "sb_secret_do_not_expose"}), encoding="utf-8")
        _, bootstrap = self.request("GET", "/api/bootstrap")
        self.assertFalse(bootstrap["cloud"]["configured"])
        self.assertNotIn("sb_secret", json.dumps(bootstrap))

    def test_paths_cannot_read_private_database(self):
        for path in ("/../private/archive.sqlite3", "/%2e%2e/private/archive.sqlite3", "/api/backups/../archive.sqlite3"):
            self.assertIn(self.request("GET", path)[0], {400, 404})


if __name__ == "__main__":
    unittest.main()
