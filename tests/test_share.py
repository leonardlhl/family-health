"""Share management tests use fake service state and temporary synthetic archives."""
import copy
from contextlib import redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

from healthlib.store import HealthStore
from server import LocalServer
from tools import share


CONFIG = {"url": "https://synthetic-test.supabase.co", "archive_id": "family"}
NOW = "2026-10-25T12:00:00+00:00"


class FakeRemote:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.mode = None
        self.read_error = False

    def get_link(self, archive_id, link_id):
        if self.read_error:
            raise share.ShareError("Synthetic connection unavailable")
        row = self.rows.get(link_id)
        return copy.deepcopy(row) if row and row["archive_id"] == archive_id else None

    def rotate(self, archive_id, previous_id, candidate):
        self.writes.append(("rotate", archive_id, previous_id, copy.deepcopy(candidate)))
        if self.mode in {"before", "lie"}:
            if self.mode == "before":
                raise share.ShareError("Synthetic pre-commit failure")
            return {"success": True}
        existing = self.rows.get(candidate["id"])
        if existing:
            if existing["archive_id"] != archive_id or existing["token_hash"] != candidate["token_hash"]:
                raise share.ShareError("Synthetic conflict")
            return copy.deepcopy(existing)
        if previous_id:
            previous = self.rows[previous_id]
            if previous["archive_id"] != archive_id or previous["revoked_at"]:
                raise share.ShareError("Synthetic prior unavailable")
            if self.mode != "non_atomic":
                previous["revoked_at"] = NOW
        elif any(row["archive_id"] == archive_id and row["revoked_at"] is None for row in self.rows.values()):
            raise share.ShareError("Synthetic active conflict")
        row = {**candidate, "archive_id": archive_id, "created_at": NOW, "revoked_at": None, "expires_at": None}
        self.rows[candidate["id"]] = row
        if self.mode == "after":
            raise share.ShareError("Synthetic timeout after commit")
        return copy.deepcopy(row)

    def revoke(self, archive_id, current):
        self.writes.append(("revoke", archive_id, copy.deepcopy(current)))
        if self.mode in {"before", "lie"}:
            if self.mode == "before":
                raise share.ShareError("Synthetic pre-commit failure")
            return {"success": True}
        row = self.rows[current["id"]]
        if row["archive_id"] != archive_id or row["token_hash"] != current["token_hash"]:
            raise share.ShareError("Synthetic mismatch")
        row["revoked_at"] = row["revoked_at"] or NOW
        if self.mode == "after":
            raise share.ShareError("Synthetic timeout after commit")
        return copy.deepcopy(row)


class ShareTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.remote = FakeRemote()
        self.manager = share.ShareManager(self.temp.name, client=self.remote, config=CONFIG)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return json.loads(self.manager.state_path.read_text(encoding="utf-8"))

    def test_create_entropy_hash_boundary_and_idempotent_creation(self):
        result = self.manager.create()
        token = result["url"].split("#share=", 1)[1]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "active")
        state = self.state()
        self.assertEqual(state["current"]["token"], token)
        self.assertEqual(state["current"]["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(self.remote.rows))
        self.assertNotIn(token, json.dumps(self.remote.writes))
        self.assertEqual(self.manager.create()["url"], result["url"])
        self.assertEqual(len(self.remote.writes), 1)
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_rotate_and_revoke_touch_only_saved_archive_link(self):
        original = self.manager.create()
        old_id = self.state()["current"]["id"]
        self.remote.rows["unrelated"] = {"id": "unrelated", "archive_id": "other-family", "token_hash": "0" * 64, "created_at": NOW, "revoked_at": None, "expires_at": None}
        rotated = self.manager.rotate()
        new_id = self.state()["current"]["id"]
        self.assertNotEqual(rotated["url"], original["url"])
        self.assertIsNotNone(self.remote.rows[old_id]["revoked_at"])
        self.assertIsNone(self.remote.rows[new_id]["revoked_at"])
        revoked = self.manager.revoke()
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNone(revoked["url"])
        self.assertIsNotNone(self.remote.rows[new_id]["revoked_at"])
        self.assertIsNone(self.remote.rows["unrelated"]["revoked_at"])
        writes = len(self.remote.writes)
        self.assertEqual(self.manager.revoke()["status"], "revoked")
        self.assertEqual(len(self.remote.writes), writes)

    def test_timeout_before_commit_keeps_old_link_and_candidate_for_exact_retry(self):
        original = self.manager.create()
        self.remote.mode = "before"
        with self.assertRaises(share.ShareError):
            self.manager.rotate()
        pending = self.state()["pending"]
        self.assertEqual(pending["action"], "rotate")
        writes = len(self.remote.writes)
        status = self.manager.status()
        self.assertEqual(status["url"], original["url"])
        self.assertEqual(status["pending_action"], "rotate")
        self.assertEqual(len(self.remote.writes), writes, "GET status must not retry a cloud mutation")
        self.assertEqual(self.state()["pending"], pending)
        self.remote.mode = None
        rotated = self.manager.rotate()
        self.assertTrue(rotated["url"].endswith(pending["candidate"]["token"]))
        self.assertEqual(self.remote.writes[-1][3]["id"], pending["candidate"]["id"])
        self.assertIsNone(self.state()["pending"])

    def test_timeout_after_commit_verifies_success_without_losing_new_token(self):
        original = self.manager.create()
        self.remote.mode = "after"
        result = self.manager.rotate()
        self.assertEqual(result["status"], "active")
        self.assertNotEqual(result["url"], original["url"])
        self.assertEqual(self.manager.revoke()["status"], "revoked")

    def test_journal_write_failure_prevents_remote_change(self):
        self.manager.create()
        before = copy.deepcopy(self.remote.rows)
        with patch.object(self.manager, "_save", side_effect=share.ShareError("Synthetic disk unavailable")):
            with self.assertRaises(share.ShareError):
                self.manager.rotate()
        self.assertEqual(self.remote.rows, before)
        self.assertEqual(len(self.remote.writes), 1)

    def test_final_write_failure_recovers_candidate_without_second_rotation(self):
        self.manager.create()
        original_save = self.manager._save
        calls = 0

        def fail_second(state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise share.ShareError("Synthetic final disk write failure")
            original_save(state)

        with patch.object(self.manager, "_save", side_effect=fail_second):
            with self.assertRaises(share.ShareError):
                self.manager.rotate()
        candidate = self.state()["pending"]["candidate"]
        writes = len(self.remote.writes)
        result = self.manager.rotate()
        self.assertTrue(result["url"].endswith(candidate["token"]))
        self.assertEqual(len(self.remote.writes), writes)
        self.assertIsNone(self.state()["pending"])

    def test_delayed_commit_is_discovered_by_read_only_status(self):
        self.manager.create()
        self.remote.mode = "before"
        with self.assertRaises(share.ShareError):
            self.manager.rotate()
        pending = self.state()["pending"]
        self.manager.status()
        self.remote.mode = None
        self.remote.rotate(CONFIG["archive_id"], pending["previous_id"], {key: pending["candidate"][key] for key in ("id", "token_hash")})
        writes = len(self.remote.writes)
        status = self.manager.status()
        self.assertTrue(status["url"].endswith(pending["candidate"]["token"]))
        self.assertEqual(len(self.remote.writes), writes)

    def test_unverified_revoke_never_claims_success_or_loses_retry(self):
        original = self.manager.create()
        self.remote.mode = "lie"
        with self.assertRaises(share.ShareError):
            self.manager.revoke()
        self.assertEqual(self.manager.status()["url"], original["url"])
        self.assertEqual(self.state()["pending"]["action"], "revoke")
        with self.assertRaises(share.ShareError):
            self.manager.rotate()
        self.remote.mode = None
        self.assertEqual(self.manager.revoke()["status"], "revoked")

    def test_non_atomic_rotation_is_not_reported_success(self):
        self.manager.create()
        self.remote.mode = "non_atomic"
        with self.assertRaises(share.ShareError):
            self.manager.rotate()
        self.assertIsNotNone(self.state()["pending"])
        self.assertEqual(self.manager.status()["status"], "unavailable")
        self.assertIsNone(self.manager.status()["url"])

    def test_remote_mismatch_or_outage_never_exposes_unverified_link(self):
        self.manager.create()
        self.remote.read_error = True
        self.assertEqual(self.manager.status()["status"], "unavailable")
        self.assertIsNone(self.manager.status()["url"])
        self.remote.read_error = False
        self.remote.rows[self.state()["current"]["id"]]["token_hash"] = "0" * 64
        status = self.manager.status()
        self.assertEqual(status["status"], "unavailable")
        self.assertIsNone(status["url"])

    def test_local_state_cannot_target_another_project_or_archive(self):
        self.manager.create()
        writes = len(self.remote.writes)
        for config in ({**CONFIG, "archive_id": "another"}, {**CONFIG, "url": "https://another.supabase.co"}):
            manager = share.ShareManager(self.temp.name, client=self.remote, config=config)
            with self.assertRaises(share.ShareError):
                manager.revoke()
        self.assertEqual(len(self.remote.writes), writes)

    def test_expired_token_is_not_returned(self):
        self.manager.create()
        self.remote.rows[self.state()["current"]["id"]]["expires_at"] = "2000-01-01T00:00:00Z"
        status = self.manager.status()
        self.assertEqual(status["status"], "expired")
        self.assertIsNone(status["url"])

    def test_local_share_file_is_excluded_from_backup_and_family_data(self):
        token = self.manager.create()["url"].split("#share=", 1)[1]
        store = HealthStore(self.temp.name)
        try:
            self.assertNotIn(token, json.dumps(store.family_data()))
            backup = store.backup()
            with zipfile.ZipFile(store.backup_path(backup["name"])) as archive:
                self.assertFalse(any("share" in name for name in archive.namelist()))
                self.assertFalse(any(token.encode() in archive.read(name) for name in archive.namelist()))
        finally:
            store.close()

    def test_cli_prints_link_only_with_explicit_flag(self):
        self.manager.create()
        with patch.object(share, "ShareManager", return_value=self.manager):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(share.main(["status", "--data-dir", self.temp.name]), 0)
            self.assertNotIn("#share=", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(share.main(["status", "--data-dir", self.temp.name, "--show-link"]), 0)
            self.assertIn("#share=", output.getvalue())

    def test_share_token_is_excluded_from_public_build_and_source_release(self):
        from tools import build_site, package_release
        token = self.manager.create()["url"].split("#share=", 1)[1]
        project = Path(self.temp.name) / "synthetic-project"
        (project / "private").mkdir(parents=True)
        (project / "private" / "family-share.json").write_text(self.manager.state_path.read_text(encoding="utf-8"), encoding="utf-8")
        (project / "web").mkdir()
        for name in build_site.REQUIRED:
            (project / "web" / name).write_text("/* synthetic source */", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            result = build_site.build(project)
        self.assertFalse(any(token.encode() in path.read_bytes() for path in Path(result["output"]).iterdir()))
        self.assertNotIn("family-share.json", result["files"])
        destination = Path(self.temp.name) / "synthetic-release.zip"
        with patch.object(package_release, "ROOT", project):
            package_release.package(destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertFalse(any("private" in name or "family-share" in name for name in archive.namelist()))
            self.assertFalse(any(token.encode() in archive.read(name) for name in archive.namelist()))


class TransportTests(unittest.TestCase):
    def test_only_hash_sent_and_http_error_body_never_disclosed(self):
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                raise HTTPError(request.full_url, 401, "SECRET BODY", {}, io.BytesIO(b"sb_secret_sensitive_value"))

        client = share.SupabaseShareClient(CONFIG["url"], "sb_secret_synthetic_value", opener=Opener())
        token = "x" * 43
        candidate = {"id": "40f6b510-d0f6-40b2-a53c-57d29df7c046", "token_hash": share.token_hash(token)}
        with self.assertRaises(share.ShareError) as caught:
            client.rotate("family", None, candidate)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("sb_secret", str(caught.exception))
        self.assertNotIn(token, requests[0].data.decode())
        self.assertEqual(json.loads(requests[0].data)["p_token_hash"], candidate["token_hash"])
        self.assertEqual(share._NoRedirect().redirect_request(None, None, 302, "", {}, "https://untrusted.invalid"), None)


class ShareHTTPTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "test-output"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.store = HealthStore(self.temp.name)
        self.remote = FakeRemote()
        self.manager = share.ShareManager(self.temp.name, client=self.remote, config=CONFIG)
        self.patcher = patch("server.ShareManager", return_value=self.manager)
        self.patcher.start()
        self.server = LocalServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.patcher.stop()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, "{}" if method == "POST" else None, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        return result

    def test_get_bearer_and_mutations_require_csrf_and_same_origin(self):
        headers = {"X-CSRF-Token": self.server.csrf_token}
        for method, path in (("GET", "/api/share"), ("POST", "/api/share/create"), ("POST", "/api/share/rotate"), ("POST", "/api/share/revoke")):
            self.assertEqual(self.request(method, path)[0], 403)
            self.assertEqual(self.request(method, path, {**headers, "Origin": "https://untrusted.invalid"})[0], 403)
            self.assertEqual(self.request(method, path, {**headers, "Host": "untrusted.invalid"})[0], 403)
        self.assertEqual(self.remote.writes, [])
        status, created, response_headers = self.request("POST", "/api/share/create", headers)
        self.assertEqual(status, 200)
        self.assertEqual(created["status"], "active")
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertEqual(response_headers["Referrer-Policy"], "no-referrer")
        status, result, _ = self.request("GET", "/api/share", headers)
        self.assertEqual(status, 200)
        self.assertEqual(result["url"], created["url"])
        self.assertEqual(self.request("POST", "/api/share/delete-all", headers)[0], 404)
        self.assertEqual(self.request("POST", "/api/share/revoke", headers)[1]["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
