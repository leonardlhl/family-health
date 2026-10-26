import json
from pathlib import Path
import tempfile
import unittest
from tools.storage import storage_status, COUNTERS
from tools.share import ShareError
TEST_ROOT = Path(__file__).resolve().parents[1] / "test-output"
TEST_ROOT.mkdir(exist_ok=True)


class StorageTests(unittest.TestCase):
    def test_managed_local_totals_do_not_read_external_sources_or_credentials(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            for name, size in (("inbox/grandpa/input.png", 3), ("staging/pending.pdf", 5),
                               ("archive/reviewed.pdf", 7), ("previews/page.jpg", 11),
                               ("backups/archive.zip", 13), ("archive.sqlite3", 17)):
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b"x" * size)
            (root / "import-folders.json").write_text('{"folders":["DO_NOT_SCAN"]}')
            (root / "family-share.json").write_text('SECRET_NOT_RETURNED')
            result = storage_status(root, config={})
            self.assertEqual(result["local"]["source_bytes"], 8)
            self.assertEqual(result["local"]["source_files"], 2)
            self.assertEqual(result["local"]["archive_bytes"], 7)
            self.assertEqual(result["local"]["preview_bytes"], 11)
            self.assertEqual(result["local"]["backup_bytes"], 13)
            self.assertEqual(result["local"]["database_bytes"], 17)
            self.assertFalse(result["local"]["external_sources_included"])
            self.assertIsNone(result["cloud"]["bucket_bytes"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_cloud_counts_all_history_and_warns_at_assumed_free_threshold(self):
        measured = {key: 0 for key in COUNTERS}
        measured.update(bucket_bytes=800_000_000, archive_bytes=700_000_000, current_bytes=200,
                        bucket_files=20, archive_files=18, current_files=2, retained_versions=9,
                        unknown_size_files=1, database_bytes=400_000_000, measured_at="2026-10-26T00:00:00Z")
        calls = []
        class Client:
            def _request(self, path, body): calls.append((path, body)); return measured
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as root:
            result = storage_status(root, client=Client(), config={"url": "https://example.supabase.co", "service_key": "synthetic"})
        self.assertEqual(calls, [("/rest/v1/rpc/family_storage_usage", {"p_archive_id": "family"})])
        cloud = result["cloud"]
        self.assertTrue(cloud["available"]); self.assertTrue(cloud["file_warning"]); self.assertTrue(cloud["database_warning"])
        self.assertTrue(cloud["quota_assumed"]); self.assertTrue(cloud["usage_incomplete"])
        self.assertEqual(cloud["retained_versions"], 9)

    def test_unavailable_usage_is_unknown_not_zero(self):
        class Client:
            def _request(self, *args): raise ShareError("synthetic unavailable")
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as root:
            result = storage_status(root, client=Client(), config={"url": "https://example.supabase.co", "service_key": "SECRET"})
        self.assertTrue(result["cloud"]["configured"])
        self.assertFalse(result["cloud"]["available"])
        self.assertTrue(all(result["cloud"][key] is None for key in COUNTERS))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_sql_usage_rpc_is_service_only_and_has_no_delete(self):
        sql = (Path(__file__).resolve().parents[1] / "supabase/004_reports_storage.sql").read_text(encoding="utf-8")
        self.assertIn("revoke all on function public.family_storage_usage(text) from public, anon, authenticated", sql)
        self.assertIn("grant execute on function public.family_storage_usage(text) to service_role", sql)
        self.assertNotIn("delete from", sql.lower())
        self.assertIn("drop constraint if exists family_media_object_path_key", sql)
        self.assertIn("public = false", sql)


if __name__ == "__main__": unittest.main()
