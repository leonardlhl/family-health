"""Set public Supabase GitHub variables and dispatch the fixed family-health Pages workflow.

Only public settings are accepted. GitHub credentials use the existing in-memory
Git Credential Manager helper and are never printed or persisted.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from build_site import validate_public_config
from github_publish import GitHub, GitHubError, OWNER, REPOSITORY, read_credential

ENDPOINT = f"/repos/{OWNER}/{REPOSITORY}"
VARIABLE_NAMES = ("HEALTH_SUPABASE_URL", "HEALTH_SUPABASE_ANON_KEY")
WORKFLOW = "pages.yml"


def load_public_config(path=None):
    if path:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        allowed = {"supabaseUrl", "supabaseAnonKey", "archiveId"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Public config accepts only supabaseUrl, supabaseAnonKey and archiveId; never a secret key.")
        url = value.get("supabaseUrl", "")
        key = value.get("supabaseAnonKey", "")
        archive_id = value.get("archiveId", "family")
    else:
        url = os.environ.get("HEALTH_SUPABASE_URL", "")
        key = os.environ.get("HEALTH_SUPABASE_ANON_KEY", "")
        archive_id = os.environ.get("HEALTH_ARCHIVE_ID", "family")
    return checked_config({"supabaseUrl": url, "supabaseAnonKey": key, "archiveId": archive_id})


def checked_config(config):
    if not isinstance(config, dict):
        raise ValueError("Public config must be an object.")
    url = config.get("supabaseUrl", "")
    key = config.get("supabaseAnonKey", "")
    archive_id = config.get("archiveId", "family")
    if not all(isinstance(value, str) for value in (url, key, archive_id)):
        raise ValueError("Public config values must be strings.")
    if archive_id != "family":
        raise ValueError("This workflow is fixed to the family archive.")
    return validate_public_config(url.rstrip("/"), key, archive_id, require_cloud=True)


def remote_public_config(client):
    values = {}
    for name in VARIABLE_NAMES:
        result = client.call("GET", f"{ENDPOINT}/actions/variables/{name}", allow_missing=True)
        if result is None:
            raise GitHubError("Both public Supabase repository variables must be configured before deployment.")
        values[name] = result.get("value", "")
    return checked_config({"supabaseUrl": values[VARIABLE_NAMES[0]], "supabaseAnonKey": values[VARIABLE_NAMES[1]], "archiveId": "family"})


def perform(client, action, config=None):
    if action not in {"configure", "dispatch", "connect", "check"}:
        raise ValueError("Unsupported public configuration operation.")
    # Validate before any API request or mutation, including when called as a library.
    expected = checked_config(config) if action in {"configure", "connect"} else None
    client.verify_owner()
    repository = client.call("GET", ENDPOINT, allow_missing=True)
    if repository is None:
        raise GitHubError("The dedicated family-health repository does not exist.")
    full_name = repository.get("full_name", f"{OWNER}/{REPOSITORY}")
    if full_name.lower() != f"{OWNER}/{REPOSITORY}":
        raise GitHubError("GitHub repository identity mismatch.")
    changed = []
    if expected:
        values = {VARIABLE_NAMES[0]: expected["supabaseUrl"], VARIABLE_NAMES[1]: expected["supabaseAnonKey"]}
        for name, value in values.items():
            path = f"{ENDPOINT}/actions/variables/{name}"
            existing = client.call("GET", path, allow_missing=True)
            if existing is None:
                client.call("POST", ENDPOINT + "/actions/variables", {"name": name, "value": value})
                changed.append(name)
            elif existing.get("value") != value:
                client.call("PATCH", path, {"name": name, "value": value})
                changed.append(name)
    actual = remote_public_config(client)
    if expected and actual != expected:
        raise GitHubError("Public variables did not verify; Pages was not dispatched.")
    dispatched = False
    if action in {"dispatch", "connect"}:
        workflow_path = f"{ENDPOINT}/actions/workflows/{WORKFLOW}"
        workflow = client.call("GET", workflow_path)
        if workflow.get("path") != ".github/workflows/pages.yml" or workflow.get("state") != "active":
            raise GitHubError("The expected Pages workflow is missing or inactive; no deployment was dispatched.")
        client.call("POST", workflow_path + "/dispatches", {"ref": "main"})
        dispatched = True
    # Deliberately do not return public key values or credential fields.
    return {"repository": f"{OWNER}/{REPOSITORY}", "public_config_verified": True,
            "changed_variables": changed, "dispatched": dispatched,
            "actions_url": f"https://github.com/{OWNER}/{REPOSITORY}/actions/workflows/{WORKFLOW}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "dispatch", "connect", "check"))
    parser.add_argument("--directory", required=True, help="Explicit source-only release repository directory")
    parser.add_argument("--public-config", help="Optional JSON containing public settings only; otherwise reads HEALTH_SUPABASE_* environment variables")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        parser.exit(1, "Public configuration failed: release directory does not exist.\n")
    try:
        config = load_public_config(args.public_config) if args.action in {"configure", "connect"} else None
        credential = read_credential(directory)
        result = perform(GitHub(credential), args.action, config)
        print(json.dumps(result, ensure_ascii=False))
    except (GitHubError, OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        # Never echo input/config parsing exceptions or server response bodies.
        print("Public configuration failed. Check public settings, GitHub sign-in and repository permissions; no credentials were printed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
