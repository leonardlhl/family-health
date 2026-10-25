import base64
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("github_configure", ROOT / "tools/github_configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
CONFIG = {"supabaseUrl": "https://example.supabase.co", "supabaseAnonKey": "sb_publishable_test_only", "archiveId": "family"}


class FakeGitHub:
    def __init__(self, variables=None):
        self.variables = dict(variables or {})
        self.calls = []
        self.owner_checked = False
        self.fail_variable = None
        self.workflow_state = "active"

    def verify_owner(self):
        self.owner_checked = True

    def call(self, method, path, data=None, allow_missing=False):
        self.calls.append((method, path, data))
        prefix = "/repos/leonardlhl/family-health"
        if not path.startswith(prefix):
            raise AssertionError("Wrong repository")
        if method == "GET" and path == prefix:
            return {"full_name": "leonardlhl/family-health"}
        if "/actions/variables/" in path:
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                return {"name": name, "value": self.variables[name]} if name in self.variables else None
            if method == "PATCH":
                if name == self.fail_variable:
                    raise configure.GitHubError("Test failure")
                self.variables[name] = data["value"]
                return {}
        if method == "POST" and path.endswith("/actions/variables"):
            if data["name"] == self.fail_variable:
                raise configure.GitHubError("Test failure")
            self.variables[data["name"]] = data["value"]
            return {}
        if method == "GET" and path.endswith("/actions/workflows/pages.yml"):
            return {"path": ".github/workflows/pages.yml", "state": self.workflow_state}
        if method == "POST" and path.endswith("/dispatches"):
            self.assert_dispatch(data)
            return {}
        raise AssertionError("Unexpected operation")

    def assert_dispatch(self, data):
        if data != {"ref": "main"}:
            raise AssertionError("Wrong branch")


class PublicConfigTests(unittest.TestCase):
    def test_creates_both_public_variables_then_dispatches(self):
        client = FakeGitHub()
        result = configure.perform(client, "connect", CONFIG)
        self.assertTrue(client.owner_checked)
        self.assertTrue(result["dispatched"])
        self.assertEqual(set(result["changed_variables"]), set(configure.VARIABLE_NAMES))
        self.assertTrue(client.calls[-1][1].endswith("/pages.yml/dispatches"))
        self.assertNotIn(CONFIG["supabaseAnonKey"], json.dumps(result))

    def test_unchanged_variables_are_not_rewritten(self):
        client = FakeGitHub(dict(zip(configure.VARIABLE_NAMES, (CONFIG["supabaseUrl"], CONFIG["supabaseAnonKey"]))))
        result = configure.perform(client, "configure", CONFIG)
        self.assertEqual(result["changed_variables"], [])
        self.assertFalse(result["dispatched"])
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))

    def test_updates_only_expected_existing_variables(self):
        client = FakeGitHub({"HEALTH_SUPABASE_URL": "https://old.supabase.co", "HEALTH_SUPABASE_ANON_KEY": "sb_publishable_old"})
        configure.perform(client, "configure", CONFIG)
        changed = [path.rsplit("/", 1)[1] for method, path, _ in client.calls if method == "PATCH"]
        self.assertEqual(set(changed), set(configure.VARIABLE_NAMES))

    def test_secret_keys_are_rejected_before_any_api_call(self):
        role = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        for key in ("sb_secret_do_not_publish", "eyJheader." + role + ".sig"):
            client = FakeGitHub()
            with self.assertRaises(ValueError):
                configure.perform(client, "connect", {**CONFIG, "supabaseAnonKey": key})
            self.assertEqual(client.calls, [])
            self.assertFalse(client.owner_checked)

    def test_partial_configuration_cannot_dispatch(self):
        client = FakeGitHub({"HEALTH_SUPABASE_URL": CONFIG["supabaseUrl"]})
        with self.assertRaises(configure.GitHubError):
            configure.perform(client, "dispatch")
        self.assertFalse(any(method == "POST" for method, _, _ in client.calls))

    def test_failed_second_variable_never_dispatches(self):
        client = FakeGitHub(); client.fail_variable = "HEALTH_SUPABASE_ANON_KEY"
        with self.assertRaises(configure.GitHubError):
            configure.perform(client, "connect", CONFIG)
        self.assertFalse(any(path.endswith("/dispatches") for _, path, _ in client.calls))

    def test_inactive_workflow_is_not_dispatched(self):
        client = FakeGitHub(dict(zip(configure.VARIABLE_NAMES, (CONFIG["supabaseUrl"], CONFIG["supabaseAnonKey"]))))
        client.workflow_state = "disabled_manually"
        with self.assertRaises(configure.GitHubError):
            configure.perform(client, "dispatch")
        self.assertFalse(any(path.endswith("/dispatches") for _, path, _ in client.calls))

    def test_empty_bootstrap_preserves_existing_pointer(self):
        sql = (ROOT / "supabase/002_bootstrap_empty.sql").read_text(encoding="utf-8").lower()
        self.assertIn("for update", sql)
        self.assertIn("if active_version is null then", sql)
        self.assertIn('"visits":[]', sql)
        self.assertIn('"attachments":[]', sql)
        self.assertNotIn("delete", sql)


if __name__ == "__main__":
    unittest.main()
