"""Narrow GitHub Pages setup helper. Credentials stay in memory and only reach api.github.com.

This helper does not stage, commit, push, copy files or alter repository visibility.
The owner must audit and push the source-only release repository separately.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

OWNER = "leonardlhl"
REPOSITORY = "family-health"
API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_credential(directory):
    """Use Git Credential Manager without printing, approving or saving credential material."""
    result = subprocess.run(
        ["git", "-C", str(directory), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n", text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        check=False,
    )
    if result.returncode:
        raise GitHubError("GitHub credential lookup failed; sign in using Git Credential Manager.")
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
    if values.get("host", "github.com") != "github.com" or values.get("protocol", "https") != "https":
        raise GitHubError("Credential host did not match GitHub HTTPS.")
    password = values.get("password", "")
    if not password:
        raise GitHubError("No GitHub credential is available.")
    return password


class GitHub:
    def __init__(self, credential):
        self.credential = credential
        self.opener = build_opener(NoRedirect)

    def call(self, method, path, data=None, allow_missing=False):
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise GitHubError("Invalid GitHub API path.")
        url = API + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise GitHubError("GitHub API origin mismatch.")
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(url, data=body, method=method, headers={
            "Authorization": "Bearer " + self.credential,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "FamilyHealth-SourcePublisher",
        })
        try:
            with self.opener.open(request, timeout=45) as response:
                content = response.read()
                return json.loads(content) if content else {}
        except HTTPError as error:
            if error.code == 404 and allow_missing:
                return None
            # Do not expose server bodies, request details, headers or credential helper errors.
            raise GitHubError(f"GitHub API action failed (HTTP {error.code}).") from None
        except (URLError, TimeoutError):
            raise GitHubError("GitHub API connection failed.") from None

    def verify_owner(self):
        user = self.call("GET", "/user")
        if str(user.get("login", "")).lower() != OWNER:
            raise GitHubError("The signed-in GitHub account is not the intended repository owner.")
        return OWNER


def perform(client, action):
    client.verify_owner()
    endpoint = f"/repos/{OWNER}/{REPOSITORY}"
    repository = client.call("GET", endpoint, allow_missing=True)
    if action == "check":
        return {"owner": OWNER, "repository": REPOSITORY, "exists": repository is not None,
                "private": repository.get("private") if repository else None,
                "url": repository.get("html_url") if repository else None}
    if action == "create":
        if repository is None:
            repository = client.call("POST", "/user/repos", {
                "name": REPOSITORY, "description": "Family Health — source-only family medical archive viewer",
                "private": False, "auto_init": False, "has_wiki": False, "has_projects": False,
            })
            created = True
        else:
            created = False
        return {"created": created, "url": repository.get("html_url"), "private": repository.get("private"),
                "note": "No files were uploaded and existing repository visibility was preserved."}
    if repository is None:
        raise GitHubError("The dedicated family-health repository does not exist yet.")
    if action == "pages":
        pages = client.call("GET", endpoint + "/pages", allow_missing=True)
        if pages is None:
            pages = client.call("POST", endpoint + "/pages", {"build_type": "workflow"})
            changed = True
        elif pages.get("build_type") != "workflow":
            # Do not replace a potentially existing Pages publishing source silently.
            raise GitHubError("Pages already uses another publishing source; inspect it before changing it.")
        else:
            changed = False
        return {"pages_enabled": True, "changed": changed, "url": pages.get("html_url"), "build_type": pages.get("build_type")}
    if action == "status":
        pages = client.call("GET", endpoint + "/pages", allow_missing=True)
        runs = client.call("GET", endpoint + "/actions/runs?per_page=3")
        return {"pages": {key: pages.get(key) for key in ("html_url", "status", "build_type")} if pages else None,
                "runs": [{key: run.get(key) for key in ("id", "name", "status", "conclusion", "html_url", "head_sha")}
                         for run in runs.get("workflow_runs", [])]}
    raise GitHubError("Unsupported GitHub operation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "create", "pages", "status"))
    parser.add_argument("--directory", required=True, help="Explicit source-only release repository directory")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        parser.exit(1, "GitHub setup failed: release directory does not exist.\n")
    try:
        credential = read_credential(directory)
        result = perform(GitHub(credential), args.action)
        print(json.dumps(result, ensure_ascii=False))
    except (GitHubError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        # Exception strings from subprocess/OSError could contain command context; keep them generic.
        message = str(error) if isinstance(error, GitHubError) else "Local credential lookup or response parsing failed."
        print("GitHub setup failed: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
