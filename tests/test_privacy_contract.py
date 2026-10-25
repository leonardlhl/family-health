"""Independent privacy regressions using only temporary synthetic records."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image, PngImagePlugin

from healthlib.store import HealthStore, ValidationError


RAW_MARKER = "SYNTHETIC_PRIVATE_IMAGE_METADATA"
FILENAME_MARKER = "SYNTHETIC_PRIVATE_ORIGINAL_FILENAME"


def sample_png(color="red", metadata=None):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("Comment", metadata)
    Image.new("RGB", (12, 8), color).save(output, "PNG", pnginfo=info)
    return output.getvalue()


def rewrite_snapshot(backup_bytes, change):
    """Build a checksum-valid fixture; this does not use a production backup."""
    with zipfile.ZipFile(io.BytesIO(backup_bytes)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    snapshot = json.loads(files["archive.json"])
    change(snapshot)
    files["archive.json"] = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["archive.json"] = {
        "size": len(files["archive.json"]),
        "sha256": hashlib.sha256(files["archive.json"]).hexdigest(),
    }
    files["manifest.json"] = json.dumps(manifest).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name, raw in files.items():
            target.writestr(name, raw)
    return output.getvalue()


class PrivacyContractTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parents[1] / "test-output"
        self.test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="privacy-", dir=self.test_root)
        self.store = HealthStore(Path(self.temp.name) / "private")

    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(self.test_root.resolve()))
        self.temp.cleanup()

    def upload(self, patient="grandpa", color="red"):
        return self.store.upload(
            patient,
            FILENAME_MARKER + ".png",
            sample_png(color, RAW_MARKER),
        )

    def draft(self, attachment=None, **overrides):
        value = {
            "patient_id": "grandpa",
            "date": "2026-10-25",
            "doctor": "測試醫生",
            "clinic": "測試診所",
            "diagnosis": "來源所載測試診斷",
            "medicines": [],
            "source_file_ids": [attachment["id"]] if attachment else [],
        }
        value.update(overrides)
        return self.store.upsert_visit(value)

    def confirmed_record(self):
        attachment = self.upload()
        self.store.sanitize_attachment(attachment["id"], sample_png("white"), reviewed=True)
        visit = self.draft(attachment)
        visit = self.store.confirm_visit(visit["id"], reviewed=True)
        return visit, attachment

    def test_aliases_cannot_be_replaced_and_original_filename_is_not_saved(self):
        self.upload()
        patient = self.store.update_patient({
            "id": "grandpa", "name": "SYNTHETIC_PRIVATE_NAME", "age": 80,
            "sex": "male", "id_number": "SYNTHETIC_ID",
        })
        self.assertEqual(patient["name"], "爺爺")
        self.assertEqual(patient["age"], 80)
        data = self.store.data()
        self.assertEqual({p["name"] for p in data["patients"]}, {"爺爺", "嬷嬷"})
        serialized = json.dumps(data, ensure_ascii=False)
        for forbidden in (FILENAME_MARKER, "SYNTHETIC_PRIVATE_NAME", "SYNTHETIC_ID"):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(ValidationError):
            self.draft(patient_name="SYNTHETIC_PRIVATE_NAME")

    def test_unreviewed_photo_blocks_confirmation_and_is_excluded_from_backup(self):
        attachment = self.upload()
        visit = self.draft(attachment, notes="SYNTHETIC_DRAFT_ONLY_TEXT")
        inbox = self.store.data_dir / "inbox" / "grandpa" / "incoming.png"
        inbox.write_bytes(sample_png("green", "SYNTHETIC_INBOX_ONLY_METADATA"))
        with self.assertRaises(ValidationError):
            self.store.confirm_visit(visit["id"], reviewed=True)
        self.assertEqual(self.store.confirmed_snapshot()["visits"], [])
        backup = self.store.backup()
        self.assertEqual(backup["excluded_pending"], 1)
        self.assertEqual(backup["excluded_drafts"], 1)
        with zipfile.ZipFile(backup["path"]) as bundle:
            self.assertEqual(set(bundle.namelist()), {"manifest.json", "archive.json"})
            snapshot = json.loads(bundle.read("archive.json"))
            self.assertEqual(snapshot["visits"], [])
            self.assertEqual(snapshot["attachments"], [])
            for name in bundle.namelist():
                self.assertNotIn(RAW_MARKER.encode(), bundle.read(name))
                self.assertNotIn(b"SYNTHETIC_DRAFT_ONLY_TEXT", bundle.read(name))
                self.assertNotIn(b"SYNTHETIC_INBOX_ONLY_METADATA", bundle.read(name))

    def test_sanitized_archive_contains_only_new_pixels_without_source_metadata(self):
        attachment = self.upload()
        old_path = self.store.attachment_path(attachment["id"])
        self.assertIn(RAW_MARKER.encode(), old_path.read_bytes())
        result = self.store.sanitize_attachment(
            attachment["id"], sample_png("white", RAW_MARKER), reviewed=True
        )
        self.assertTrue(result["privacy_checked"])
        self.assertEqual(result["original_name"], "")
        self.assertEqual(result["ocr_text"], "")
        self.assertFalse(old_path.exists())
        saved_path = self.store.attachment_path(attachment["id"])
        self.assertNotIn(RAW_MARKER.encode(), saved_path.read_bytes())
        with Image.open(saved_path) as image:
            self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 255, 255))
            self.assertFalse(image.info)
        visit = self.draft(attachment)
        with self.assertRaises(ValidationError):
            self.store.confirm_visit(visit["id"], reviewed=False)
        self.store.confirm_visit(visit["id"], reviewed=True)
        snapshot = self.store.confirmed_snapshot()
        self.assertEqual(len(snapshot["visits"]), 1)
        backup = self.store.backup()
        with zipfile.ZipFile(backup["path"]) as bundle:
            media = bundle.read("media/" + attachment["id"] + ".png")
            self.assertEqual(media, self.store.attachment_path(attachment["id"]).read_bytes())
            self.assertNotIn(RAW_MARKER.encode(), media)

    def test_editing_confirmed_text_or_source_invalidates_publication(self):
        visit, attachment = self.confirmed_record()
        edited = self.store.upsert_visit({**visit, "diagnosis": "已修訂測試內容"})
        self.assertEqual(edited["status"], "draft")
        self.assertFalse(edited["privacy_reviewed"])
        self.assertIsNone(edited["reviewed_at"])
        self.assertEqual(self.store.confirmed_snapshot()["visits"], [])
        self.store.confirm_visit(visit["id"], reviewed=True)
        self.store.sanitize_attachment(attachment["id"], sample_png("blue"), reviewed=True)
        self.assertEqual(self.store.confirmed_snapshot()["visits"], [])
        current = next(v for v in self.store.data()["visits"] if v["id"] == visit["id"])
        self.assertEqual(current["status"], "draft")
        self.assertFalse(current["privacy_reviewed"])

    def test_medicine_sources_cannot_cross_patient_boundaries(self):
        grandma_photo = self.upload(patient="grandma")
        with self.assertRaises(ValidationError):
            self.draft(medicines=[{
                "name": "測試藥物", "source_file_ids": [grandma_photo["id"]],
            }])
        self.assertEqual(self.store.data()["visits"], [])

    def test_restore_preserves_new_pending_work_that_backups_intentionally_exclude(self):
        self.confirmed_record()
        backup = self.store.backup()
        pending = self.upload(color="green")
        draft = self.draft(pending, notes="NEW_PENDING_WORK")
        pending_path = self.store.attachment_path(pending["id"])
        preview = self.store.restore_preview(Path(backup["path"]).read_bytes())
        self.store.restore_apply(preview["token"])
        data = self.store.data()
        self.assertIn(draft["id"], {v["id"] for v in data["visits"]})
        self.assertIn(pending["id"], {a["id"] for a in data["attachments"]})
        preserved = next(v for v in data["visits"] if v["id"] == draft["id"])
        self.assertEqual(preserved["status"], "draft")
        self.assertEqual(preserved["notes"], "NEW_PENDING_WORK")
        self.assertTrue(pending_path.exists())

    def test_restore_does_not_import_extra_patient_identity_fields(self):
        self.confirmed_record()
        backup = self.store.backup()

        def add_disallowed_fields(snapshot):
            snapshot["attachments"][0]["patient_name"] = "SYNTHETIC_PRIVATE_NAME"
            snapshot["attachments"][0]["identity_number"] = "SYNTHETIC_PRIVATE_ID"

        forged = rewrite_snapshot(Path(backup["path"]).read_bytes(), add_disallowed_fields)
        try:
            preview = self.store.restore_preview(forged)
            self.store.restore_apply(preview["token"])
        except ValidationError:
            pass  # Rejecting an unknown attachment field is also a safe result.
        data = json.dumps(self.store.data(), ensure_ascii=False)
        self.assertNotIn("SYNTHETIC_PRIVATE_NAME", data)
        self.assertNotIn("SYNTHETIC_PRIVATE_ID", data)

    def test_restore_strips_metadata_even_from_checksum_valid_backup(self):
        _, attachment = self.confirmed_record()
        backup = self.store.backup()
        with zipfile.ZipFile(backup["path"]) as bundle:
            files = {name: bundle.read(name) for name in bundle.namelist()}
        media_name = "media/" + attachment["id"] + ".png"
        files[media_name] = sample_png("white", RAW_MARKER)
        snapshot = json.loads(files["archive.json"])
        snapshot["attachments"][0]["sha256"] = hashlib.sha256(files[media_name]).hexdigest()
        snapshot["attachments"][0]["size"] = len(files[media_name])
        files["archive.json"] = json.dumps(snapshot).encode()
        manifest = json.loads(files["manifest.json"])
        manifest["files"] = {
            name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in files.items() if name != "manifest.json"
        }
        files["manifest.json"] = json.dumps(manifest).encode()
        fixture = io.BytesIO()
        with zipfile.ZipFile(fixture, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, raw in files.items():
                bundle.writestr(name, raw)
        preview = self.store.restore_preview(fixture.getvalue())
        self.store.restore_apply(preview["token"])
        saved = self.store.attachment_path(attachment["id"]).read_bytes()
        self.assertNotIn(RAW_MARKER.encode(), saved)
        restored = next(a for a in self.store.data()["attachments"] if a["id"] == attachment["id"])
        self.assertEqual(restored["sha256"], hashlib.sha256(saved).hexdigest())
        self.assertEqual(restored["size"], len(saved))


if __name__ == "__main__":
    unittest.main()
