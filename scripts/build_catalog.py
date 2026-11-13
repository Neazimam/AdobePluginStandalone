#!/usr/bin/env python3
"""
Build After-Effect-HUB.json from THIS repo's own GitHub releases.

This repo hosts ONLY standalone exe-installer plugins — a .zip containing
installer.exe (any name) [+ activator.exe]. The workflow catalogs each
release into a flat JSON array that the EAGLE app fetches as a secondary
catalog. Every entry has category="plugin" hardcoded.

Run by .github/workflows/build-catalog.yml. Requires the gh CLI (authenticated
via GH_TOKEN) and the GITHUB_REPOSITORY env var (auto-set by Actions).

RELEASE CONVENTION
------------------
  Tag:    <slug>/<version>
          slug      = URL-safe id, lowercase + hyphens, STABLE across version
                      bumps (e.g. "ae-deepglow"). Becomes the catalog "id".
          version   = the version string (e.g. 1.2.0, 2, 1.0-beta)
          Example:  ae-deepglow/1.1.1
                    color-mamba/3.1
  Title:  display name (e.g. "AE DeepGlow"). Falls back to a title-cased slug.
  Body:   OPTIONAL, one `key: value` line per field:
            plugin_type: core        # default "aeplugin"; "core" for MediaCore
            description: ...          # optional blurb
            tags: motion, glow, light  # comma-separated
  Asset:  ONE .zip containing installer.exe [+ activator.exe]. The FIRST .zip
          asset is used. The zip MUST contain at least one .exe — releases
          whose zip has no .exe inside are SKIPPED.

OUTPUT
------
After-Effect-HUB.json — a flat JSON array, one object per release, sorted by
name (case-insensitive) for a stable, reviewable diff.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CATALOG_FILE = "After-Effect-HUB.json"
# <slug>/<version>  — slug is lowercase + hyphens + digits, version is anything.
TAG_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)/(.+)$")


def gh(*args):
    """Run a gh CLI command, return stdout. Exits on failure."""
    try:
        return subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"gh command failed: {' '.join(e.cmd)}\n{e.stderr}")


def list_releases(repo):
    """List all non-draft releases as dicts (GitHub API shape)."""
    out = gh("api", f"repos/{repo}/releases", "--paginate", "--jq", ".[]")
    releases = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            releases.append(json.loads(line))
    return [r for r in releases if not r.get("draft")]


def parse_tag(tag):
    """<slug>/<version> -> (slug, version) or None if the tag is malformed."""
    m = TAG_RE.match(tag or "")
    if not m:
        return None
    slug, version = m.group(1), m.group(2)
    if not slug or not version:
        return None
    return slug, version


def parse_body(body):
    """Parse optional 'key: value' lines from the release body."""
    meta = {}
    if not body:
        return meta
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-z_]+)\s*:\s*(.*)$", line)
        if m:
            meta[m.group(1).strip()] = m.group(2).strip()
    return meta


def slug_to_name(slug):
    """ae-deepglow -> 'Ae Deepglow' (hyphens to spaces, title-cased)."""
    return " ".join(w.capitalize() for w in slug.split("-"))


def first_zip_asset(assets):
    for a in assets or []:
        if (a.get("name") or "").lower().endswith(".zip"):
            return a
    return None


def download_asset(tag, asset_name, dest_dir):
    """Download one asset from a release into dest_dir. Returns the local path."""
    os.makedirs(dest_dir, exist_ok=True)
    gh("release", "download", tag, "--pattern", asset_name,
       "--dir", dest_dir, "--clobber")
    return os.path.join(dest_dir, asset_name)


def file_sha256_and_size(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def zip_has_exe(path):
    """Return True if the zip contains at least one .exe entry."""
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".exe"):
                    return True
    except (zipfile.BadZipFile, OSError):
        return False
    return False


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        sys.exit("GITHUB_REPOSITORY env var is not set.")

    releases = list_releases(repo)
    # Sort by published_at ascending so the LATEST release for a slug wins.
    releases.sort(key=lambda r: r.get("published_at") or "")

    entries = []
    seen_ids = set()
    tmp_dir = "/tmp/catalog-assets"

    for rel in releases:
        tag = rel.get("tag_name", "")
        parsed = parse_tag(tag)
        if not parsed:
            print(f"  skip: tag '{tag}' is not <slug>/<version> "
                  f"(slug must be lowercase + hyphens)", file=sys.stderr)
            continue
        slug, version = parsed

        asset = first_zip_asset(rel.get("assets"))
        if not asset:
            print(f"  skip: no .zip asset on release '{tag}'", file=sys.stderr)
            continue

        name = (rel.get("name") or "").strip() or slug_to_name(slug)
        body_meta = parse_body(rel.get("body", ""))
        plugin_type = body_meta.get("plugin_type", "aeplugin")
        description = body_meta.get("description", "")
        tags_raw = body_meta.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

        pub = rel.get("published_at") or ""
        last_updated = pub[:10] if pub else datetime.now(timezone.utc).strftime("%Y-%m-%d")

        asset_name = asset.get("name", "")
        local_path = download_asset(tag, asset_name, tmp_dir)
        sha, size = file_sha256_and_size(local_path)

        # This repo hosts ONLY exe-installer zips — the zip MUST contain at
        # least one .exe. Validate BEFORE dedup so a bad newer release doesn't
        # wipe out a good older entry for the same slug.
        if not zip_has_exe(local_path):
            print(f"  skip: zip '{asset_name}' on '{tag}' contains no .exe — "
                  f"this repo hosts exe/activator zips only", file=sys.stderr)
            os.remove(local_path)
            continue
        os.remove(local_path)

        # Dedup by slug — replace any older entry (releases are sorted oldest
        # first, so a later release for the same slug is the newer one).
        if slug in seen_ids:
            print(f"  note: replacing older release for slug '{slug}'",
                  file=sys.stderr)
            entries = [e for e in entries if e["id"] != slug]
        seen_ids.add(slug)

        entry = {
            "id": slug,
            "category": "plugin",
            "name": name,
            "version": version,
            "last_updated": last_updated,
            "link": asset.get("browser_download_url", ""),
            "plugin_type": plugin_type,
            "description": description,
            "sha256": sha,
            "size_bytes": size,
            "tags": tags,
        }
        entries.append(entry)
        print(f"  ok: {slug} {version} — {asset_name} ({size} bytes)")

    entries.sort(key=lambda e: e["name"].lower())

    Path(CATALOG_FILE).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(entries)} entries to {CATALOG_FILE}")


if __name__ == "__main__":
    main()
