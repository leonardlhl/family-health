import base64
import copy
import importlib.util
import json
import os
import io
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load_module("health_publish", ROOT / "tools/publish.py")
builder = load_module("health_build", ROOT / "tools/build_site.py")
github = load_module("health_github", ROOT / "tools/github_publish.py")
VERSION = "b7970972-c839-491e-a680-819deea82a60"


def example():
    return {
        "schema_version": 1,
        "patients": [{"id": "grandpa", "name": "MUST NOT LEAK", "age": 82, "sex": "male", "dob": "PRIVATE"}],
        "visits": [{"id": "visit1", "patient_id": "grandpa", "date": "2026-10-25", "doctor": "Doctor Example",
                    "clinic": "", "diagnosis": "TEST ONLY", "status": "confirmed", "privacy_reviewed": True, "reviewed_at": "2026-10-25T00:00:00Z",
                    "source_file_ids": ["file1"], "medicines": [], "national_id": "PRIVATE"}],
        "attachments": [{"id": "file1", "patient_id": "grandpa", "privacy_checked": True, "mime": "image/png",
                         "sha256": hashlib.sha256(b"test").hexdigest(), "size": 4, "archive_name": "image.png", "original_name": "PRIVATE.png",
                         "ocr_text": "PATIENT NAME PRIVATE", "path": "C:/private/file.png"}],
    }


class PublicationTests(unittest.TestCase):
    def test_whitelist_excludes_identity_and_local_metadata(self):
        result = publish.prepare_snapshot(example(), "family", VERSION)
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("MUST NOT LEAK", encoded)
        self.assertNotIn("original_name", encoded)
        self.assertNotIn("ocr_text", encoded)
        self.assertNotIn("national_id", encoded)
        self.assertEqual(result["patients"][0], {"id": "grandpa", "name": "爺爺", "age": 82, "sex": "male"})
        self.assertEqual(result["visits"][0]["diagnosis"], "TEST ONLY")
        self.assertTrue(result["attachments"][0]["object_path"].startswith("family/objects/file1-"))

    def test_draft_or_unreviewed_visit_is_rejected(self):
        for change in ({"status": "draft"}, {"reviewed_at": None}, {"privacy_reviewed": False}):
            value = example(); value["visits"][0].update(change)
            with self.assertRaises(publish.PublishError):
                publish.prepare_snapshot(value, "family", VERSION)

    def test_private_source_cannot_publish_without_review(self):
        for change in ({"privacy_checked": False}, {"patient_id": "grandma"}, {"mime": "application/pdf"}):
            value = example(); value["attachments"][0].update(change)
            with self.assertRaises(publish.PublishError):
                publish.prepare_snapshot(value, "family", VERSION)

    def test_unlinked_images_are_not_published(self):
        value = example(); unused = copy.deepcopy(value["attachments"][0]); unused.update(id="unused", privacy_checked=False)
        value["attachments"].append(unused)
        result = publish.prepare_snapshot(value, "family", VERSION)
        self.assertEqual([a["id"] for a in result["attachments"]], ["file1"])

    def test_thumbnail_metadata_is_scoped_and_whitelisted(self):
        value = example()
        value["attachments"][0]["thumbnail"] = {"mime": "image/jpeg", "sha256": "b" * 64, "size": 100,
                                                  "width": 960, "height": 640, "path": "PRIVATE", "patient_name": "PRIVATE"}
        snapshot = publish.prepare_snapshot(value, "family", VERSION)
        preview = snapshot["attachments"][0]["thumbnail"]
        self.assertEqual(preview["object_path"], "family/objects/file1-" + "b" * 64 + ".thumb.jpg")
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        self.assertTrue(snapshot["attachments"][0]["object_path"].endswith(".png"))
        for change in ({"width": 961}, {"height": True}, {"mime": "image/png"}, {"sha256": "invalid"}):
            invalid = copy.deepcopy(value); invalid["attachments"][0]["thumbnail"].update(change)
            with self.assertRaises(publish.PublishError):
                publish.prepare_snapshot(invalid, "family", VERSION)

    def test_thumbnail_upload_is_in_same_atomic_publication(self):
        value = example()
        value["attachments"][0]["thumbnail"] = {"mime": "image/jpeg", "sha256": hashlib.sha256(b"preview").hexdigest(), "size": 7, "width": 960, "height": 640}
        snapshot = publish.prepare_snapshot(value, "family", VERSION)
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        calls = []
        client.verify_object = lambda asset: False
        client.request = lambda path, body, mime="application/json": calls.append((path, body, mime))
        client.publish(snapshot, {"file1": b"test", "file1:thumbnail": b"preview"}, "family", VERSION)
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0][0].endswith(".png"))
        self.assertTrue(calls[1][0].endswith(".thumb.jpg"))
        self.assertEqual(calls[1][2], "image/jpeg")
        payload = json.loads(calls[2][1])
        self.assertEqual(len(payload["p_media"]), 2)
        self.assertEqual(payload["p_media"][1]["id"], "file1:thumbnail")
        calls.clear()
        def fail_preview(path, body, mime="application/json"):
            calls.append((path, body, mime))
            if path.endswith(".thumb.jpg"): raise publish.PublishError("simulated preview upload failure")
        client.request = fail_preview
        with self.assertRaises(publish.PublishError):
            client.publish(snapshot, {"file1": b"test", "file1:thumbnail": b"preview"}, "family", VERSION)
        self.assertFalse(any("/rpc/" in call[0] for call in calls))

    def test_medication_sources_are_checked(self):
        value = example(); value["visits"][0]["medicines"] = [{"id": "med1", "source_file_ids": ["missing"]}]
        with self.assertRaises(publish.PublishError):
            publish.prepare_snapshot(value, "family", VERSION)

    def test_medicine_category_preserves_confirmed_label_and_legacy_default(self):
        value = example()
        value["visits"][0]["medicines"] = [
            {"id": "med1", "name": "測試藥名", "category": "  醫生確認分類  "},
            {"id": "med2", "name": "舊紀錄測試"},
        ]
        result = publish.prepare_snapshot(value, "family", VERSION)
        medicines = result["visits"][0]["medicines"]
        self.assertEqual(medicines[0]["category"], "醫生確認分類")
        self.assertEqual(medicines[1]["category"], "")
        value["visits"][0]["medicines"][0]["category"] = "類" * 81
        with self.assertRaises(publish.PublishError):
            publish.prepare_snapshot(value, "family", VERSION)

    def test_invalid_age_and_path_are_rejected(self):
        value = example(); value["patients"][0]["age"] = True
        with self.assertRaises(publish.PublishError):
            publish.prepare_snapshot(value, "family", VERSION)
        with self.assertRaises(publish.PublishError):
            publish.prepare_snapshot(example(), "../family", VERSION)

    def test_media_failure_never_switches_active_version(self):
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        client.verify_object = lambda asset: False
        calls = []
        def fail(path, *_args):
            calls.append(path)
            raise publish.PublishError("simulated upload failure")
        client.request = fail
        snapshot = publish.prepare_snapshot(example(), "family", VERSION)
        with self.assertRaises(publish.PublishError):
            client.publish(snapshot, {"file1": b"test"}, "family", VERSION)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("/storage/"))
        self.assertFalse(any("rpc" in p for p in calls))

    def test_pointer_commit_is_last_after_all_assets(self):
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        client.verify_object = lambda asset: False
        calls = []
        client.request = lambda path, *_args: calls.append(path)
        snapshot = publish.prepare_snapshot(example(), "family", VERSION)
        client.publish(snapshot, {"file1": b"test"}, "family", VERSION)
        self.assertEqual(calls[-1], "/rest/v1/rpc/publish_family_snapshot")

    def test_new_secret_key_is_not_used_as_a_bearer_jwt(self):
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        class StubResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b""
        captured = []
        def fake_open(request, timeout):
            captured.append(request)
            return StubResponse()
        with patch.object(client.opener, "open", fake_open):
            client.request("/rest/v1/rpc/publish_family_snapshot", b"{}")
        self.assertEqual(captured[0].get_header("Apikey"), "sb_secret_test_only")
        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_real_store_integration_and_last_sync_metadata(self):
        from healthlib.store import HealthStore
        from PIL import Image
        temp_root = ROOT / "test-output"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as folder:
            store = HealthStore(folder)
            pixels = io.BytesIO(); Image.new("RGB", (8, 8), "white").save(pixels, format="PNG")
            attachment = store.upload("grandpa", "identity-filename.png", pixels.getvalue())
            store.review_attachment(attachment["id"], reviewed=True)
            visit = store.upsert_visit({"patient_id": "grandpa", "date": "2026-10-25", "doctor": "測試醫生", "diagnosis": "測試內容",
                                        "medicines": [{"name": "測試文字", "status": "unknown"}], "source_file_ids": [attachment["id"]]})
            store.confirm_visit(visit["id"], reviewed=True)
            store.close()
            with patch.dict(os.environ, {"HEALTH_SUPABASE_URL": "https://example.supabase.co", "HEALTH_SUPABASE_SERVICE_KEY": "sb_secret_test_only", "HEALTH_ARCHIVE_ID": "family"}):
                check = publish.publish_archive(folder, dry_run=True)
                self.assertEqual(check, {"visits": 1, "reports": 0, "attachments": 1, "dry_run": True})
                captured = []
                with patch.object(publish.SupabasePublisher, "unchanged_current", return_value=None), patch.object(publish.SupabasePublisher, "publish", lambda self, snapshot, *_: captured.append(snapshot)):
                    result = publish.publish_archive(folder)
            self.assertEqual(len(captured), 1)
            self.assertNotIn("identity-filename", json.dumps(captured[0]))
            self.assertEqual(captured[0]["visits"][0]["diagnosis"], "測試內容")
            self.assertEqual(captured[0]["attachments"][0]["thumbnail"]["mime"], "image/jpeg")
            self.assertLessEqual(captured[0]["attachments"][0]["thumbnail"]["width"], 960)
            store = HealthStore(folder)
            try: self.assertEqual(store.metadata("last_sync")["version_id"], result["version_id"])
            finally: store.close()


class ReportsAndReuseTests(unittest.TestCase):
    def report_example(self):
        raw = example()
        raw["visits"] = []
        raw["reports"] = [{"id": "report1", "patient_id": "grandpa", "date": "2026-10-26", "title": "TEST REPORT",
                           "category": "  TEST CATEGORY ", "summary": "SOURCE TEXT", "status": "confirmed",
                           "privacy_reviewed": True, "reviewed_at": "2026-10-26T00:00:00Z", "source_file_ids": ["file1"],
                           "national_id": "PRIVATE", "original_name": "PRIVATE", "review_notes": ""}]
        raw["attachments"][0].update(mime="application/pdf", thumbnail={"mime": "image/jpeg", "sha256": "b" * 64,
            "size": 100, "width": 120, "height": 80})
        return raw

    def test_reports_pdf_allowlist_and_limits(self):
        raw = self.report_example()
        result = publish.prepare_snapshot(raw, "family", VERSION)
        self.assertEqual(result["reports"][0]["category"], "TEST CATEGORY")
        self.assertEqual(result["reports"][0]["summary"], "SOURCE TEXT")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("review_notes", result["reports"][0])
        self.assertTrue(result["attachments"][0]["object_path"].endswith(".pdf"))
        for change in ({"status": "draft"}, {"privacy_reviewed": False}, {"patient_id": "grandma"},
                       {"reviewed_at": None}, {"title": ""}, {"source_file_ids": []}, {"review_notes": "unresolved"}):
            invalid = copy.deepcopy(raw); invalid["reports"][0].update(change)
            with self.assertRaises(publish.PublishError): publish.prepare_snapshot(invalid, "family", VERSION)
        for change in ({"size": 50_000_001}, {"privacy_checked": False}, {"thumbnail": None}):
            invalid = copy.deepcopy(raw); invalid["attachments"][0].update(change)
            with self.assertRaises(publish.PublishError): publish.prepare_snapshot(invalid, "family", VERSION)
        raw["attachments"][0]["size"] = 50_000_000
        self.assertEqual(publish.prepare_snapshot(raw, "family", VERSION)["attachments"][0]["size"], 50_000_000)

    def test_cas_paths_survive_changed_versions_and_verified_assets_are_not_uploaded(self):
        one = publish.prepare_snapshot(example(), "family", VERSION)
        raw = example(); raw["visits"][0]["notes"] = "changed report text"
        two = publish.prepare_snapshot(raw, "family", "00000000-0000-4000-8000-000000000101")
        self.assertEqual(one["attachments"][0]["object_path"], two["attachments"][0]["object_path"])
        self.assertNotEqual(publish.content_fingerprint(one), publish.content_fingerprint(two))
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        client.verify_object = Mock(return_value=True); client.request = Mock()
        stats = client.publish(two, {"file1": b"test"}, "family", VERSION)
        self.assertEqual(stats, {"uploaded_files": 0, "reused_files": 1})
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], "/rest/v1/rpc/publish_family_snapshot")

    def test_upload_conflict_is_reused_only_after_byte_verification(self):
        snapshot = publish.prepare_snapshot(example(), "family", VERSION)
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        client.verify_object = Mock(side_effect=[False, True])
        client.request = Mock(side_effect=[publish.PublishError("synthetic concurrent upload"), b""])
        self.assertEqual(client.publish(snapshot, {"file1": b"test"}, "family", VERSION)["reused_files"], 1)
        self.assertEqual(client.request.call_count, 2)
        client.verify_object = Mock(side_effect=[False, False])
        client.request = Mock(side_effect=publish.PublishError("synthetic upload failure"))
        with self.assertRaises(publish.PublishError): client.publish(snapshot, {"file1": b"test"}, "family", VERSION)
        self.assertEqual(client.request.call_count, 1)

    def remote_client(self, snapshot):
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        current = {"id": "family", "current_version": VERSION, "published_at": "2026-10-26"}
        client.current_archive = Mock(return_value=current)
        manifest = [{"attachment_id": f["id"], "object_path": f["object_path"]} for f in snapshot["attachments"]]
        for file in snapshot["attachments"]:
            if file.get("thumbnail"):
                manifest.append({"attachment_id": file["id"] + ":thumbnail", "object_path": file["thumbnail"]["object_path"]})
        def read(path):
            if "family_snapshots?" in path:
                return [{"archive_id": "family", "version_id": VERSION, "snapshot": snapshot, "published_at": "2026-10-26"}]
            return manifest
        client.read_json = Mock(side_effect=read)
        client.verify_object = Mock(return_value=True)
        return client, manifest

    def test_unchanged_skip_requires_current_manifest_all_bytes_and_stable_pointer(self):
        snapshot = publish.prepare_snapshot(example(), "family", VERSION)
        client, manifest = self.remote_client(snapshot)
        self.assertEqual(client.unchanged_current(snapshot, "family")["version_id"], VERSION)
        self.assertEqual(client.verify_object.call_count, 1)
        client.current_archive = Mock(return_value=None)
        self.assertIsNone(client.unchanged_current(snapshot, "family"))
        client, manifest = self.remote_client(snapshot); manifest.append(manifest[0].copy())
        self.assertIsNone(client.unchanged_current(snapshot, "family")); client.verify_object.assert_not_called()
        client, _ = self.remote_client(snapshot); client.verify_object.return_value = False
        self.assertIsNone(client.unchanged_current(snapshot, "family"))
        client, _ = self.remote_client(snapshot)
        client.verify_object.side_effect = publish.PublishError("synthetic corrupt object")
        with self.assertRaises(publish.PublishError): client.unchanged_current(snapshot, "family")
        client, _ = self.remote_client(snapshot)
        client.current_archive.side_effect = [{"id": "family", "current_version": VERSION}, {"id": "family", "current_version": "00000000-0000-4000-8000-000000000101"}]
        self.assertIsNone(client.unchanged_current(snapshot, "family"))

    def test_legacy_paths_and_optional_reports_share_stable_fingerprint(self):
        snapshot = publish.prepare_snapshot(example(), "family", VERSION)
        old = copy.deepcopy(snapshot); old.pop("reports")
        old["attachments"][0]["object_path"] = f"family/{VERSION}/file1.png"
        self.assertEqual(publish.content_fingerprint(old), publish.content_fingerprint(snapshot))
        client, _ = self.remote_client(old)
        self.assertIsNotNone(client.unchanged_current(snapshot, "family"))
        changed = copy.deepcopy(snapshot); changed["reports"] = self.report_example()["reports"]
        self.assertNotEqual(publish.content_fingerprint(changed), publish.content_fingerprint(snapshot))

    def test_existing_object_verification_detects_same_size_corruption_and_bounds_read(self):
        client = publish.SupabasePublisher("https://example.supabase.co", "sb_secret_test_only")
        asset = publish.prepare_snapshot(example(), "family", VERSION)["attachments"][0]
        def response(payload):
            stream = io.BytesIO(payload); stream.headers = {"Content-Type": "image/png"}
            return stream
        with patch.object(client.opener, "open", side_effect=lambda *args, **kwargs: response(b"test")):
            self.assertTrue(client.verify_object(asset))
        for payload in (b"evil", b"oversized", b"sh"):
            with patch.object(client.opener, "open", side_effect=lambda *args, **kwargs: response(payload)):
                with self.assertRaises(publish.PublishError): client.verify_object(asset)


class BuildTests(unittest.TestCase):
    def test_rejects_service_role_and_secret_keys(self):
        payload = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        for key in ("sb_secret_not_public", "eyJheader." + payload + ".signature"):
            with self.assertRaises(ValueError):
                builder.validate_public_config("https://example.supabase.co", key, "family")

    def test_whitelist_cannot_include_runtime_data(self):
        temp_root = ROOT / "test-output"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as folder:
            root = Path(folder); (root / "web").mkdir(); (root / "private").mkdir()
            for name in builder.REQUIRED:
                (root / "web" / name).write_text("// empty interface fixture", encoding="utf-8")
            (root / "web" / "snapshot.json").write_text("PRIVATE", encoding="utf-8")
            (root / "private" / "cloud.json").write_text("SECRET", encoding="utf-8")
            with patch.dict(os.environ, {"HEALTH_SUPABASE_URL": "", "HEALTH_SUPABASE_ANON_KEY": ""}):
                result = builder.build(root)
            self.assertNotIn("snapshot.json", result["files"])
            self.assertNotIn("cloud.json", result["files"])
            contents = "".join(p.read_text(encoding="utf-8") for p in (root / "dist").iterdir())
            self.assertNotIn("PRIVATE", contents)
            self.assertNotIn("SECRET", contents)
            self.assertFalse(result["configured"])

    def test_deployment_requires_config(self):
        with self.assertRaises(ValueError):
            builder.validate_public_config("", "", "family", True)

    def test_initial_setup_viewer_allows_only_completely_empty_config(self):
        config = builder.validate_public_config("", "", "family", False)
        self.assertEqual(config["supabaseUrl"], "")
        for url, key in (("https://example.supabase.co", ""), ("", "sb_publishable_test")):
            with self.assertRaises(ValueError):
                builder.validate_public_config(url, key, "family", False)

    def test_members_are_select_only_at_database_boundary(self):
        sql = (ROOT / "supabase/001_family_archive.sql").read_text(encoding="utf-8").lower()
        self.assertIn("revoke all on public.family_archives", sql)
        self.assertIn("grant select on public.family_archives", sql)
        self.assertIn("from public, anon, authenticated", sql)
        self.assertNotIn("for insert to authenticated", sql)
        self.assertNotIn("for update to authenticated", sql)
        self.assertNotIn("for delete to authenticated", sql)
        self.assertIn("set public = false", sql)


class GitHubHelperTests(unittest.TestCase):
    def test_create_is_source_repo_metadata_only(self):
        calls = []
        class FakeClient:
            def verify_owner(self): return "leonardlhl"
            def call(self, method, path, data=None, allow_missing=False):
                calls.append((method, path, data))
                if method == "GET": return None
                return {"html_url": "https://github.com/leonardlhl/family-health", "private": False}
        result = github.perform(FakeClient(), "create")
        self.assertTrue(result["created"])
        self.assertEqual(calls[-1][1], "/user/repos")
        self.assertEqual(calls[-1][2]["name"], "family-health")
        self.assertFalse(calls[-1][2]["private"])
        self.assertNotIn("contents", str(calls))

    def test_existing_pages_source_is_preserved(self):
        class FakeClient:
            def verify_owner(self): return "leonardlhl"
            def call(self, method, path, data=None, allow_missing=False):
                self.assertion = method
                if method != "GET": raise AssertionError("Must not mutate existing source")
                return {"build_type": "legacy"} if path.endswith("/pages") else {"private": False}
        with self.assertRaises(github.GitHubError):
            github.perform(FakeClient(), "pages")

    def test_origin_and_redirect_restrictions(self):
        client = github.GitHub("do-not-print")
        for path in ("https://example.invalid", "//example.invalid", "/\\example.invalid"):
            with self.assertRaises(github.GitHubError):
                client.call("GET", path)
        self.assertIsNone(github.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
