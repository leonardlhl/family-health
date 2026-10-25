import base64
import copy
import importlib.util
import json
import os
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
                         "sha256": "a" * 64, "size": 12, "archive_name": "image.png", "original_name": "PRIVATE.png",
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
        self.assertTrue(result["attachments"][0]["object_path"].startswith("family/" + VERSION + "/"))

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

    def test_medication_sources_are_checked(self):
        value = example(); value["visits"][0]["medicines"] = [{"id": "med1", "source_file_ids": ["missing"]}]
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
        with patch.object(publish, "urlopen", fake_open):
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
                self.assertEqual(check, {"visits": 1, "attachments": 1, "dry_run": True})
                captured = []
                with patch.object(publish.SupabasePublisher, "publish", lambda self, snapshot, *_: captured.append(snapshot)):
                    result = publish.publish_archive(folder)
            self.assertEqual(len(captured), 1)
            self.assertNotIn("identity-filename", json.dumps(captured[0]))
            self.assertEqual(captured[0]["visits"][0]["diagnosis"], "測試內容")
            store = HealthStore(folder)
            try: self.assertEqual(store.metadata("last_sync")["version_id"], result["version_id"])
            finally: store.close()


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
