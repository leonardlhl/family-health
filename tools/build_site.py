"""Build the empty family viewer from an explicit allowlist. Never includes private data."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = ("index.html", "app.js", "styles.css", "i18n.js", "cloud.js", "config.js", "icon.svg", "manifest.webmanifest")
REQUIRED = ("index.html", "app.js", "styles.css", "i18n.js", "cloud.js", "config.js")


def validate_public_config(url, key, archive_id, require_cloud=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", archive_id):
        raise ValueError("Invalid archive ID.")
    if not url and not key and not require_cloud:
        return {"supabaseUrl": "", "supabaseAnonKey": "", "archiveId": archive_id}
    if not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url):
        raise ValueError("A valid HTTPS Supabase project URL is required.")
    if key.startswith("sb_publishable_"):
        if not re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]+", key):
            raise ValueError("Malformed publishable key.")
    elif key.startswith("eyJ"):
        try:
            part = key.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if payload.get("role") != "anon":
                raise ValueError()
        except (ValueError, IndexError, UnicodeDecodeError):
            raise ValueError("Only an anon key can be included in the viewer. Never use service_role.") from None
    else:
        raise ValueError("Use an anon or publishable key, never a secret/service-role key.")
    return {"supabaseUrl": url, "supabaseAnonKey": key, "archiveId": archive_id}


def build(root=ROOT, require_cloud=False):
    root = Path(root).resolve()
    source = root / "web"
    output = root / "dist"
    if source.is_symlink() or source.resolve().parent != root:
        raise ValueError("Unsafe application source path.")
    # Fixed child paths only; never accept a user-provided recursive deletion destination.
    if output.is_symlink() or output.resolve().parent != root:
        raise ValueError("Unsafe build output path.")
    for name in REQUIRED:
        path = source / name
        if not path.is_file() or path.is_symlink() or path.resolve().parent != source.resolve():
            raise ValueError(f"Missing or unsafe application asset: {name}")
    config = validate_public_config(os.environ.get("HEALTH_SUPABASE_URL", "").rstrip("/"),
                                    os.environ.get("HEALTH_SUPABASE_ANON_KEY", ""),
                                    os.environ.get("HEALTH_ARCHIVE_ID", "family"), require_cloud)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir()
    for name in FILES:
        path = source / name
        if name == "config.js" or not path.is_file():
            continue
        if path.is_symlink() or path.resolve().parent != source.resolve():
            raise ValueError("Symlinked assets cannot be published.")
        shutil.copyfile(path, output / name)
    (output / "config.js").write_text("// Public settings only.\nexport const cloudConfig = Object.freeze(" + json.dumps(config, ensure_ascii=False) + ");\nexport default cloudConfig;\n", encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return {"output": str(output), "configured": bool(config["supabaseUrl"]), "files": sorted(p.name for p in output.iterdir())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-cloud", action="store_true", help="Fail unless public cloud configuration is supplied")
    args = parser.parse_args()
    try:
        print(json.dumps(build(require_cloud=args.require_cloud), ensure_ascii=False))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Build failed: {error}\n")
