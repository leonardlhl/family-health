"""Synthetic preview tests: no real medication photos or patient records."""
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

from healthlib.store import HealthStore, PrivacyReviewRequired, ValidationError
from server import LocalServer


def photo(size=(1800, 1200), color=None):
    image = Image.new("RGB", size, color) if color else Image.effect_noise(size, 70).convert("RGB")
    stream = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("patient_name", "SYNTHETIC DO NOT RETAIN")
    image.save(stream, format="PNG", pnginfo=metadata)
    return stream.getvalue()


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "test-output"
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.store = HealthStore(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reviewed_photo(self, size=(1800, 1200), color=None):
        attachment = self.store.upload("grandpa", "synthetic.png", photo(size, color))
        return self.store.review_attachment(attachment["id"], reviewed=True)

    def test_unreviewed_photo_cannot_generate_preview(self):
        attachment = self.store.upload("grandpa", "source.png", photo((200, 150)))
        with self.assertRaises(PrivacyReviewRequired):
            self.store.thumbnail_path(attachment["id"])
        with self.assertRaises(PrivacyReviewRequired):
            self.store.thumbnail_info(attachment["id"])
        self.assertEqual(list((Path(self.temp.name) / "previews").iterdir()), [])

    def test_preview_reduces_delivery_size_without_changing_original(self):
        attachment = self.reviewed_photo()
        original_path = self.store.attachment_path(attachment["id"])
        original_bytes = original_path.read_bytes()
        original_name = original_path.name
        preview = self.store.thumbnail_path(attachment["id"])
        preview_bytes = preview.read_bytes()
        info = self.store.thumbnail_info(attachment["id"])
        self.assertEqual(info["mime"], "image/jpeg")
        self.assertEqual((info["width"], info["height"]), (960, 640))
        self.assertLess(len(preview_bytes), len(original_bytes) // 2)
        self.assertEqual(info["sha256"], hashlib.sha256(preview_bytes).hexdigest())
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.store.attachment_path(attachment["id"]).name, original_name)
        self.assertEqual(self.store.attachment(attachment["id"])["sha256"], attachment["sha256"])
        with Image.open(io.BytesIO(preview_bytes)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.getexif())
            self.assertNotIn("patient_name", image.info)
            self.assertNotIn("comment", image.info)
        self.assertNotIn(b"SYNTHETIC DO NOT RETAIN", preview_bytes)

    def test_small_preview_is_not_upscaled_and_cache_is_reused(self):
        attachment = self.reviewed_photo((120, 80), "white")
        first = self.store.thumbnail_path(attachment["id"])
        stat = first.stat()
        second = self.store.thumbnail_path(attachment["id"])
        self.assertEqual(first, second)
        self.assertEqual(stat.st_mtime_ns, second.stat().st_mtime_ns)
        self.assertEqual(self.store.thumbnail_info(attachment["id"])["width"], 120)

    def test_sanitization_invalidates_preview_and_reflects_new_pixels(self):
        attachment = self.reviewed_photo((200, 160), "white")
        old_preview = self.store.thumbnail_path(attachment["id"])
        self.store.sanitize_attachment(attachment["id"], photo((200, 160), "black"), reviewed=True)
        self.assertFalse(old_preview.exists())
        new_preview = self.store.thumbnail_path(attachment["id"])
        self.assertNotEqual(old_preview, new_preview)
        with Image.open(new_preview) as image:
            self.assertEqual(image.getpixel((50, 50)), (0, 0, 0))
        self.assertEqual(len(list((Path(self.temp.name) / "previews").glob("*.jpg"))), 1)

    def test_checksum_failure_never_serves_stale_or_replaced_source(self):
        attachment = self.reviewed_photo((200, 160), "white")
        old_preview = self.store.thumbnail_path(attachment["id"])
        self.store.attachment_path(attachment["id"]).write_bytes(photo((200, 160), "black"))
        with self.assertRaises(ValidationError):
            self.store.thumbnail_path(attachment["id"])
        self.assertFalse(old_preview.exists())

    def test_corrupted_preview_is_regenerated_and_not_in_backup(self):
        attachment = self.reviewed_photo((200, 160), "white")
        preview = self.store.thumbnail_path(attachment["id"])
        preview.write_bytes(b"invalid jpeg")
        rebuilt = self.store.thumbnail_path(attachment["id"])
        with Image.open(rebuilt) as image:
            self.assertEqual(image.format, "JPEG")
        visit = self.store.upsert_visit({"patient_id": "grandpa", "date": "2026-10-25", "doctor": "測試醫生", "source_file_ids": [attachment["id"]]})
        self.store.confirm_visit(visit["id"], reviewed=True)
        backup = self.store.backup()
        with zipfile.ZipFile(backup["path"]) as bundle:
            self.assertFalse(any("previews" in name or name.endswith(".jpg") for name in bundle.namelist()))

    def test_http_preview_gate_type_and_cross_origin_protection(self):
        attachment = self.store.upload("grandpa", "synthetic.png", photo((200, 150)))
        server = LocalServer(("127.0.0.1", 0), self.store)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", f"/api/files/{attachment['id']}/thumbnail", headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.getheader("Content-Type"), response.getheader("Cache-Control"), response.read()
            connection.close()
            return result

        try:
            self.assertEqual(request()[0], 403)
            self.store.review_attachment(attachment["id"], reviewed=True)
            status, mime, cache, raw = request()
            self.assertEqual(status, 200)
            self.assertEqual(mime, "image/jpeg")
            self.assertEqual(cache, "no-store")
            self.assertEqual(request({"Origin": "https://untrusted.example"})[0], 403)
            self.assertEqual(request({"Host": "untrusted.example"})[0], 403)
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.size, (200, 150))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
