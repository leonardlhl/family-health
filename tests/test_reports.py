"""Synthetic reports only; never open a production PDF, inbox, or data directory."""
import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import pymupdf
from PIL import Image

from healthlib import pdf
from healthlib.store import HealthStore, PrivacyReviewRequired, ValidationError
from server import LocalServer


MARKER = "SYNTHETIC_PRIVATE_IDENTITY"


def sample_pdf(pages=2, rotate=True, encrypted=False):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=300, height=400)
            page.insert_text((25, 45), MARKER, fontsize=12)
            page.insert_text((25, 150), "DEMO ONLY - TEST REPORT", fontsize=12)
            page.draw_rect(pymupdf.Rect(100, 220, 200, 300), color=(0, 0, 1), fill=(0, 0, 1))
            if index == 0:
                page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(20, 20, 150, 50), "uri": "https://example.invalid/private"})
                page.add_text_annot((260, 300), MARKER)
                widget = pymupdf.Widget()
                widget.field_name = "identity"
                widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                widget.field_value = MARKER
                widget.rect = pymupdf.Rect(20, 330, 270, 350)
                page.add_widget(widget)
            if index == 1 and rotate:
                page.set_cropbox(pymupdf.Rect(10, 10, 290, 390))
                page.set_rotation(90)
        document.set_metadata({"title": MARKER, "author": MARKER, "subject": MARKER})
        document.embfile_add("synthetic.txt", MARKER.encode())
        if encrypted:
            return document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="test")
        return document.tobytes()


def sample_png(color="white"):
    out = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(out, "PNG")
    return out.getvalue()


def modify_backup(raw, callback):
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    snapshot = json.loads(files["archive.json"])
    callback(snapshot, files)
    files["archive.json"] = json.dumps(snapshot, ensure_ascii=False).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = {name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items() if name != "manifest.json"}
    files["manifest.json"] = json.dumps(manifest).encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return result.getvalue()


class ReportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="reports-", dir=root)
        self.store = HealthStore(Path(self.temp.name) / "private")
        self.raw_pdf = sample_pdf()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def upload_pdf(self, patient="grandpa"):
        return self.store.upload(patient, MARKER + ".pdf", self.raw_pdf, "report")

    def review_pdf(self, item):
        information = self.store.pdf_info(item["id"])
        return self.store.sanitize_pdf(item["id"], [{"page": 0, "boxes": [{"x": 0, "y": 0, "w": 1, "h": 0.25}]}], list(range(information["pages"])), True, information["sha256"])

    def report(self, item, **changes):
        value = {"patient_id": "grandpa", "date": "2026-10-26", "title": "測試血液報告", "category": "化驗", "facility": "測試中心", "doctor": "測試醫生", "summary": "照抄來源的測試結果", "source_file_ids": [item["id"]]}
        value.update(changes)
        return self.store.upsert_report(value)

    def confirmed(self):
        item = self.upload_pdf()
        self.review_pdf(item)
        report = self.report(item)
        return self.store.confirm_report(report["id"], True), item

    def assert_pixel_only(self, raw, count=2):
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            self.assertEqual(doc.page_count, count)
            self.assertEqual(doc.embfile_count(), 0)
            self.assertFalse(doc.get_xml_metadata())
            self.assertFalse(any(doc.metadata.get(field) for field in ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate")))
            for page in doc:
                self.assertEqual(page.get_text(), "")
                self.assertEqual(page.get_links(), [])
                self.assertEqual(list(page.annots() or []), [])
                self.assertEqual(list(page.widgets() or []), [])
                self.assertEqual(len(page.get_images()), 1)
                self.assertEqual(page.get_fonts(), [])

    def test_pdf_sanitize_is_mult_page_pixel_only_and_mask_is_baked(self):
        item = self.upload_pdf()
        raw_path = self.store.attachment_path(item["id"])
        information = self.store.pdf_info(item["id"])
        self.assertEqual(information["pages"], 2)
        self.assertEqual(information["page_sizes"], [{"width": 750, "height": 1000}, {"width": 950, "height": 700}])
        saved = self.review_pdf(item)
        self.assertTrue(saved["privacy_checked"])
        self.assertFalse(raw_path.exists())
        archived = self.store.attachment_path(item["id"]).read_bytes()
        self.assert_pixel_only(archived)
        rendered = Image.open(io.BytesIO(self.store.pdf_page(item["id"], 0)))
        self.assertTrue(all(channel < 5 for channel in rendered.getpixel((350, 100))))
        self.assertEqual(self.store.pdf_info(item["id"])["page_sizes"], information["page_sizes"])
        self.assertNotIn(MARKER, json.dumps(self.store.data()))

    def test_rotated_and_cropped_page_masks_use_same_display_coordinates(self):
        item = self.upload_pdf()
        info = self.store.pdf_info(item["id"])
        saved = self.store.sanitize_pdf(item["id"], [{"page": 1, "boxes": [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}]}], [0, 1], True, info["sha256"])
        page = Image.open(io.BytesIO(self.store.pdf_page(item["id"], 1)))
        self.assertEqual(page.size, (950, 700))
        self.assertTrue(all(channel < 5 for channel in page.getpixel((190, 140))))
        self.assertEqual(saved["pages"], 2)

    def test_all_page_review_stale_hash_and_coordinate_failures_leave_raw_untouched(self):
        item = self.upload_pdf()
        original = self.store.attachment_path(item["id"]).read_bytes()
        for reviewed in ([0], [0, 0], [0, 1, 2], [False, 1], "all"):
            with self.assertRaises(ValidationError):
                self.store.sanitize_pdf(item["id"], [], reviewed, True, item["sha256"])
        for checksum in (None, "0" * 64):
            with self.assertRaises(ValidationError):
                self.store.sanitize_pdf(item["id"], [], [0, 1], True, checksum)
        for box in ({"x": -0.1, "y": 0, "w": 0.2, "h": 0.2}, {"x": 0.9, "y": 0, "w": 0.2, "h": 0.2}, {"x": float("nan"), "y": 0, "w": 0.2, "h": 0.2}):
            with self.assertRaises(ValidationError):
                self.store.sanitize_pdf(item["id"], [{"page": 0, "boxes": [box]}], [0, 1], True, item["sha256"])
        for action in (lambda: self.store.review_attachment(item["id"], True), lambda: self.store.sanitize_attachment(item["id"], sample_png(), True)):
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(self.store.attachment_path(item["id"]).read_bytes(), original)
        self.assertFalse(self.store.attachment(item["id"])["privacy_checked"])

    def test_pdf_limits_password_and_malformed_input(self):
        self.assertEqual(pdf.MAX_PDF_BYTES, 50_000_000)
        for raw in (b"%PDF-1.7\nbroken", sample_pdf(encrypted=True), sample_pdf(81, rotate=False)):
            with self.assertRaises(ValidationError):
                self.store.upload("grandpa", "test.pdf", raw, "report")
        with patch.object(pdf, "MAX_RENDER_PIXELS", 1):
            with self.assertRaises(ValidationError):
                pdf.info(self.raw_pdf)
        with patch.object(pdf, "MAX_PDF_BYTES", len(self.raw_pdf) - 1):
            with self.assertRaises(ValidationError):
                pdf.info(self.raw_pdf)
        self.assertEqual(self.store.data()["attachments"], [])

    def test_report_confirmation_names_sources_and_keeps_deduplication(self):
        item = self.upload_pdf()
        draft = self.report(item)
        with self.assertRaises(ValidationError):
            self.store.confirm_report(draft["id"], True)
        self.review_pdf(item)
        raw = self.store.attachment_path(item["id"]).read_bytes()
        confirmed = self.store.confirm_report(draft["id"], True)
        path = self.store.attachment_path(item["id"])
        self.assertEqual(path.relative_to(self.store.data_dir).as_posix(), "archive/reports/grandpa/2026-10-26_測試血液報告/2026-10-26_爺爺_測試血液報告_01.pdf")
        self.assertEqual(path.read_bytes(), raw)
        self.assertTrue(self.store.upload("grandpa", "renamed.pdf", self.raw_pdf, "report")["duplicate"])
        family = self.store.family_data()
        self.assertEqual(family["reports"][0]["id"], confirmed["id"])
        self.assertEqual(family["reports"][0]["summary"], draft["summary"])
        self.assertNotIn("review_notes", family["reports"][0])
        self.assertNotIn("archive_name", family["attachments"][0])

    def test_report_cross_person_missing_title_review_notes_and_identity_are_rejected(self):
        grandma = self.upload_pdf("grandma")
        with self.assertRaises(ValidationError):
            self.report(grandma)
        item = self.upload_pdf()
        self.review_pdf(item)
        for change in ({"title": ""}, {"date": None}, {"review_notes": "待核對"}, {"source_file_ids": []}):
            report = self.report(item, **change)
            with self.assertRaises(ValidationError):
                self.store.confirm_report(report["id"], True)
        with self.assertRaises(ValidationError):
            self.report(item, patient_name=MARKER)
        self.assertEqual(self.store.family_data()["reports"], [])

    def test_shared_file_is_not_duplicated_or_renamed_by_another_record(self):
        report, item = self.confirmed()
        path = self.store.attachment_path(item["id"])
        visit = self.store.upsert_visit({"patient_id": "grandpa", "date": "2026-10-26", "doctor": "測試醫生", "source_file_ids": [item["id"]]})
        self.store.confirm_visit(visit["id"], True)
        self.assertEqual(self.store.attachment_path(item["id"]), path)
        self.assertEqual(len(self.store.confirmed_snapshot()["attachments"]), 1)
        self.store.sanitize_pdf(item["id"], [], [0, 1], True, self.store.pdf_info(item["id"])["sha256"])
        self.assertEqual(self.store.family_data()["reports"], [])
        self.assertEqual(self.store.family_data()["visits"], [])

    def test_report_preview_gate_and_cache_survive_nested_archive(self):
        item = self.upload_pdf()
        with self.assertRaises(PrivacyReviewRequired):
            self.store.thumbnail_path(item["id"])
        self.review_pdf(item)
        report = self.report(item)
        self.store.confirm_report(report["id"], True)
        preview = self.store.thumbnail_path(item["id"])
        info = self.store.thumbnail_info(item["id"])
        self.assertLessEqual(max(info["width"], info["height"]), 960)
        self.assertEqual(info["mime"], "image/jpeg")
        self.assertEqual(hashlib.sha256(preview.read_bytes()).hexdigest(), info["sha256"])
        self.assertEqual(self.store.thumbnail_path(item["id"]), preview)

    def test_report_backup_restore_preserves_pending_drafts_and_pdf_integrity(self):
        confirmed, item = self.confirmed()
        backup = self.store.backup()
        archive_bytes = Path(backup["path"]).read_bytes()
        draft = self.store.upsert_report({**confirmed, "notes": "NEW_PENDING_REPORT"})
        pending = self.store.upload("grandma", "pending.png", sample_png(), "report")
        pending_report = self.store.upsert_report({"patient_id": "grandma", "title": "新報告", "source_file_ids": [pending["id"]]})
        preview = self.store.restore_preview(archive_bytes)
        self.assertEqual(preview["reports"], 1)
        result = self.store.restore_apply(preview["token"])
        self.assertEqual(result["reports"], 1)
        self.assertEqual(result["preserved_drafts"], 2)
        data = self.store.data()
        self.assertEqual(len(data["reports"]), 3)
        retained = next(record for record in data["reports"] if record["notes"] == "NEW_PENDING_REPORT")
        self.assertNotEqual(retained["id"], confirmed["id"])
        self.assertEqual(next(record for record in data["reports"] if record["id"] == pending_report["id"])["source_file_ids"], [pending["id"]])
        self.assertEqual(len(self.store.family_data()["reports"]), 1)
        safe = self.store.attachment_path(item["id"]).read_bytes()
        self.assert_pixel_only(safe)
        restored = self.store.attachment(item["id"])
        self.assertEqual(restored["sha256"], hashlib.sha256(safe).hexdigest())
        self.assertEqual(restored["size"], len(safe))
        self.assertEqual(restored["pages"], 2)

    def test_restore_rejects_cross_person_and_pdf_manifest_page_mismatch(self):
        self.confirmed()
        backup = Path(self.store.backup()["path"]).read_bytes()
        mismatch = modify_backup(backup, lambda snapshot, files: snapshot["attachments"][0].update(pages=1))
        with self.assertRaises(ValidationError):
            self.store.restore_preview(mismatch)
        crossed = modify_backup(backup, lambda snapshot, files: snapshot["reports"][0].update(patient_id="grandma"))
        preview = self.store.restore_preview(crossed)
        before = self.store.data()
        with self.assertRaises(ValidationError):
            self.store.restore_apply(preview["token"])
        self.assertEqual(self.store.data(), before)

    def test_old_schema_without_reports_still_restores(self):
        backup = Path(self.store.backup()["path"]).read_bytes()
        legacy = modify_backup(backup, lambda snapshot, files: snapshot.pop("reports"))
        preview = self.store.restore_preview(legacy)
        self.assertEqual(preview["reports"], 0)
        self.assertTrue(self.store.restore_apply(preview["token"])["restored"])

    def test_checksum_valid_backup_cannot_reintroduce_hidden_pdf_objects(self):
        _, item = self.confirmed()
        backup = Path(self.store.backup()["path"]).read_bytes()

        def inject(snapshot, files):
            files["media/" + item["id"] + ".pdf"] = self.raw_pdf
            snapshot["attachments"][0].update(sha256=hashlib.sha256(self.raw_pdf).hexdigest(), size=len(self.raw_pdf))

        forged = modify_backup(backup, inject)
        preview = self.store.restore_preview(forged)
        self.store.restore_apply(preview["token"])
        self.assert_pixel_only(self.store.attachment_path(item["id"]).read_bytes())

    def test_recursive_report_source_groups_are_opaque_read_only_and_deduplicated(self):
        root = Path(self.temp.name) / "incoming"
        folder = root / "SYNTHETIC_PRIVATE_FOLDER" / "subfolder"
        folder.mkdir(parents=True)
        path = folder / "source.pdf"
        path.write_bytes(self.raw_pdf)
        (self.store.data_dir / "import-folders.json").write_text(json.dumps([{"patient_id": "grandpa", "path": str(root), "kind": "report"}]), encoding="utf-8")
        self.assertEqual(self.store.scan()["imported"], 1)
        item = self.store.data()["attachments"][0]
        self.assertEqual(item["kind"], "report")
        self.assertRegex(item["source_group"], r"^g_[a-f0-9]{24}$")
        self.assertNotIn("SYNTHETIC_PRIVATE_FOLDER", json.dumps(self.store.data()))
        path.rename(path.with_name("renamed.pdf"))
        self.assertEqual(self.store.scan()["duplicates"], 1)
        self.assertEqual(path.with_name("renamed.pdf").read_bytes(), self.raw_pdf)

    def test_title_paths_are_sanitized_and_collisions_never_overwrite(self):
        first = self.store.upload("grandpa", "a.png", sample_png(), "report")
        second = self.store.upload("grandpa", "b.png", sample_png("blue"), "report")
        for item in (first, second):
            self.store.review_attachment(item["id"], True)
            report = self.report(item, title="../test:report\\one")
            self.store.confirm_report(report["id"], True)
        paths = [self.store.attachment_path(item["id"]) for item in (first, second)]
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(path.is_relative_to(self.store.data_dir / "archive" / "reports" / "grandpa") for path in paths))
        self.assertNotEqual(paths[0].read_bytes(), paths[1].read_bytes())

    def test_draft_delete_preserves_source(self):
        item = self.upload_pdf()
        report = self.report(item)
        self.assertTrue(self.store.delete_report(report["id"])["sources_preserved"])
        self.assertTrue(self.store.attachment_path(item["id"]).exists())
        self.assertEqual(self.store.data()["reports"], [])


class ReportHTTPTests(unittest.TestCase):
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

    def request(self, method, path, value=None, csrf=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = self.server.csrf_token
        connection.request(method, path, json.dumps(value) if value is not None else None, headers)
        response = connection.getresponse()
        status, raw, mime = response.status, response.read(), response.getheader("Content-Type")
        connection.close()
        return status, json.loads(raw) if "json" in mime else raw

    def test_pdf_endpoints_and_confirmed_report_family_visibility(self):
        status, item = self.request("POST", "/api/upload", {"patient_id": "grandpa", "name": "test.pdf", "kind": "report", "content_base64": base64.b64encode(sample_pdf()).decode()})
        self.assertEqual(status, 200)
        path = "/api/files/" + item["id"]
        self.assertEqual(self.request("GET", path + "/thumbnail")[0], 403)
        _, info = self.request("GET", path + "/pdf-info")
        status, page = self.request("GET", path + "/page/0")
        self.assertEqual(status, 200)
        self.assertTrue(page.startswith(b"\x89PNG"))
        self.assertEqual(self.request("GET", path + "/page/999")[0], 400)
        payload = {"pages": [], "reviewed_pages": [0, 1], "privacy_checked": True, "source_sha256": info["sha256"]}
        self.assertEqual(self.request("POST", path + "/sanitize-pdf", payload, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", path + "/sanitize-pdf", payload)[0], 200)
        _, report = self.request("POST", "/api/reports", {"patient_id": "grandpa", "date": "2026-10-26", "title": "測試報告", "source_file_ids": [item["id"]]})
        self.assertEqual(self.request("GET", "/api/family-data")[1]["reports"], [])
        self.assertEqual(self.request("POST", "/api/reports/" + report["id"] + "/confirm", {"reviewed": True})[0], 200)
        self.assertEqual(self.request("GET", "/api/family-data")[1]["reports"][0]["id"], report["id"])
        self.assertEqual(self.request("GET", path + "/thumbnail")[0], 200)

    def test_storage_status_is_owner_only_and_does_not_expose_credentials(self):
        with patch("tools.storage.storage_status", return_value={"local": {"archive_bytes": 123}, "cloud": {"configured": False, "available": False}}) as measured:
            self.assertEqual(self.request("GET", "/api/storage", csrf=False)[0], 403)
            measured.assert_not_called()
            status, value = self.request("GET", "/api/storage")
            self.assertEqual(status, 200)
            self.assertEqual(value["local"]["archive_bytes"], 123)
            measured.assert_called_once_with(self.store.data_dir)


if __name__ == "__main__":
    unittest.main()
