#!/usr/bin/env python3
"""
Build After-Effect-HUB.json from THIS repo's own GitHub releases, using an
AI BRAIN that analyzes each release and decides all the catalog metadata.

This repo hosts ONLY standalone exe-installer plugins — a .zip containing
installer.exe (any name) [+ activator.exe]. The workflow catalogs each
release into a flat JSON array that the EAGLE app fetches as a secondary
catalog. Every entry has category="plugin" hardcoded.

THE AI BRAIN (GitHub Models, free, uses GITHUB_TOKEN)
----------------------------------------------------
For each release, the script sends the AI a complete picture:
  - the release tag, title, body
  - the .zip asset filename
  - the LIST OF FILES INSIDE THE ZIP (names + sizes) — so the AI can
    identify the product from the installer's filename, e.g.
    "DeepGlow_Setup.exe" -> product is "DeepGlow"

The AI then decides ALL the human-meaningful metadata:
  - id (stable slug)
  - name (display name)
  - version
  - description (one sentence)
  - tags (3-5 relevant)
  - plugin_type (aeplugin or core)

The script handles ONLY the deterministic fields the AI can't know:
  - sha256 (hash of the downloaded zip)
  - size_bytes (zip file size)
  - link (the release asset's download URL)
  - last_updated (the release's publish date)
  - category (always "plugin" — this repo hosts plugins only)

If the AI call fails (network, auth, rate-limit, bad JSON), the script
falls back to heuristics (tag as slug, "1.0" version, empty description).
The workflow ALWAYS succeeds — AI is the brain, but heuristics are the
safety net.

TAG FORMATS
-----------
Any tag is accepted. The AI reads the tag + everything else and decides
the slug + version itself. Examples that all work:
  - standalone
  - ae-deepglow/1.1.1
  - DeepGlow_1.2
  - adobe-plugin-v2

Run by .github/workflows/build-catalog.yml. Requires the gh CLI (authenticated
via GH_TOKEN) and the GITHUB_REPOSITORY env var (auto-set by Actions).

OUTPUT
------
After-Effect-HUB.json — a flat JSON array, one object per .zip ASSET (a
release may host multiple plugins — each zip is its own entry), sorted by
name (case-insensitive) for a stable, reviewable diff.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CATALOG_FILE = "After-Effect-HUB.json"

# GitHub Models endpoint + model. Free tier, uses the GITHUB_TOKEN for auth.
# Override the model with the AI_MODEL env var if you want a different one.
AI_ENDPOINT = "https://models.inference.ai.azure.com/chat/completions"
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
AI_TIMEOUT = 45  # seconds per call — the brain does more work, so allow more time


# ---------- GitHub CLI helper ----------

def gh(*args):
    """Run a gh CLI command, return stdout. Exits on failure."""
    try:
        return subprocess.run(
            ["gh", *args], check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"gh command failed: {' '.join(e.cmd)}\n{e.stderr}")


# ---------- release listing + parsing ----------

def list_releases(repo):
    """List all non-draft releases as dicts (GitHub API shape)."""
    out = gh("api", f"repos/{repo}/releases", "--paginate", "--jq", ".[]")
    releases = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            releases.append(json.loads(line))
    return [r for r in releases if not r.get("draft")]


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


def sanitize_slug(text):
    """Force a string into a URL-safe slug: lowercase + hyphens."""
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def all_zip_assets(assets):
    """Return every .zip asset in the release (not just the first).

    A single release may host MULTIPLE plugins — e.g. the `standalone`
    release has Saber_1.0.40.zip + Topaz.Video.AI.Pro-7.1.6.zip. Each zip
    is its own product and must become its own catalog entry, so we return
    ALL of them and the main loop processes each independently.
    """
    return [a for a in (assets or [])
            if (a.get("name") or "").lower().endswith(".zip")]


# ---------- AI brain (GitHub Models) ----------

def call_ai(prompt, token):
    """Call GitHub Models. Returns the assistant's text, or None on failure."""
    if not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 600,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(AI_ENDPOINT, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        print(f"  AI HTTP {e.code}: {e.reason}", file=sys.stderr)
        return None
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"  AI call failed: {e}", file=sys.stderr)
        return None


def list_zip_contents(path, max_files=40):
    """Return a list of 'filename (size bytes)' strings for files in the zip.
    Capped at max_files so huge zips don't blow up the AI prompt."""
    contents = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                contents.append(f"{info.filename} ({info.file_size} bytes)")
                if len(contents) >= max_files:
                    contents.append(f"... ({len(zf.namelist()) - max_files} more files)")
                    break
    except (zipfile.BadZipFile, OSError) as e:
        print(f"  could not read zip contents: {e}", file=sys.stderr)
    return contents


def analyze_with_ai(tag, title, body, asset_name, zip_contents, token):
    """The AI brain. Sees everything about the release + the zip's file list,
    and decides all the catalog metadata. Returns a dict or None on failure."""
    zip_listing = "\n".join(f"  - {f}" for f in zip_contents) or "  (empty or unreadable)"
    body_excerpt = (body or "")[:800].replace("\n", " ")
    prompt = (
        "You are the brain behind an After Effects plugin catalog. "
        "You are given a GitHub release for an AE plugin installer. "
        "Analyze ALL the information below and decide the catalog entry's "
        "metadata.\n\n"
        f"RELEASE TAG: {tag}\n"
        f"RELEASE TITLE: {title or '(none)'}\n"
        f"RELEASE BODY: {body_excerpt or '(none)'}\n"
        f"ZIP ASSET FILENAME: {asset_name or '(none)'}\n"
        f"FILES INSIDE THE ZIP:\n{zip_listing}\n\n"
        "The .exe inside the zip is the installer — its name usually reveals "
        "the product. 'activator.exe' means the plugin needs activation. "
        "Use everything above to identify what this plugin is.\n\n"
        "Decide and return ONLY a JSON object (no markdown fences, no "
        "explanation) with EXACTLY these fields:\n"
        "{\n"
        '  "id": "stable-url-safe-slug",\n'
        '  "name": "Human-readable Display Name",\n'
        '  "version": "version-string",\n'
        '  "description": "one sentence, max 120 chars, describing what it does",\n'
        '  "tags": ["t1", "t2", "t3"],\n'
        '  "plugin_type": "aeplugin"\n'
        "}\n\n"
        "RULES:\n"
        "- id: 3-30 chars, lowercase letters/digits/hyphens ONLY. STABLE across "
        "version bumps (so updates are recognized). Derive from the product "
        "name, NOT the version. e.g. 'DeepGlow_Setup.exe' -> 'deepglow'.\n"
        "- name: the product's display name with proper capitalization "
        "(e.g. 'DeepGlow', 'Color Mamba'). DO NOT include the version.\n"
        "- version: extract from the tag, title, asset filename, or the exe "
        "filename. If the tag is 'slug/version' use that version. "
        "Default '1.0' if unclear.\n"
        "- description: ONE sentence, max 120 chars. Be specific about what "
        "the plugin does (glow, color grading, particles, motion blur, etc.). "
        "Do not say 'a plugin' — describe its function.\n"
        "- tags: 3-5 lowercase, relevant tags. Choose from common AE plugin "
        "categories: motion, glow, color, grade, gpu, particles, transition, "
        "audio, 3d, blur, light, text, shape, animation, render, script, ui.\n"
        "- plugin_type: 'aeplugin' (default, installs to AE Plug-ins folder) "
        "or 'core' (installs to the shared MediaCore folder). Use 'core' only "
        "if the product is clearly a core/shared plugin.\n"
        "- If the body explicitly sets any of these fields, RESPECT that value "
        "and do not override it."
    )
    response = call_ai(prompt, token)
    if not response:
        return None
    # Strip markdown code fences if the model wrapped the JSON.
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```(?:json)?\s*", "", response)
        response = re.sub(r"\s*```$", "", response)
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        print(f"  AI returned non-JSON: {response[:160]}", file=sys.stderr)
        return None


# ---------- file helpers ----------

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


# ---------- main ----------

def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        sys.exit("GITHUB_REPOSITORY env var is not set.")

    # AI token: prefer AI_TOKEN secret (a PAT) if set, else the Actions
    # GITHUB_TOKEN. If neither, the brain is skipped and heuristics are used.
    ai_token = os.environ.get("AI_TOKEN") or os.environ.get("GH_TOKEN") or ""
    ai_enabled = bool(ai_token)
    if ai_enabled:
        print(f"AI brain enabled (model: {AI_MODEL}).")
    else:
        print("AI brain disabled (no token). Using heuristic fallback.")

    releases = list_releases(repo)
    releases.sort(key=lambda r: r.get("published_at") or "")

    entries = []
    seen_ids = set()
    tmp_dir = "/tmp/catalog-assets"

    for rel in releases:
        tag = rel.get("tag_name", "")
        title = (rel.get("name") or "").strip()
        body = rel.get("body", "")

        # A release may host MULTIPLE plugins (multiple .zip assets). Each
        # zip is its own product and becomes its own catalog entry —
        # processed independently so two zips on one release don't bleed.
        zips = all_zip_assets(rel.get("assets"))
        if not zips:
            print(f"  skip: no .zip asset on release '{tag}'", file=sys.stderr)
            continue

        # Parse body ONCE per release — body-level overrides (plugin_type,
        # description, tags) apply to every zip on this release.
        body_meta = parse_body(body)
        body_plugin_type = body_meta.get("plugin_type")
        body_description = body_meta.get("description")
        body_tags = ([t.strip() for t in body_meta["tags"].split(",") if t.strip()]
                     if body_meta.get("tags") else [])

        for asset in zips:
            asset_name = asset.get("name", "")

            # --- download + validate the zip ---
            local_path = download_asset(tag, asset_name, tmp_dir)
            sha, size = file_sha256_and_size(local_path)

            if not zip_has_exe(local_path):
                print(f"  skip: zip '{asset_name}' on '{tag}' contains no .exe — "
                      f"this repo hosts exe/activator zips only", file=sys.stderr)
                os.remove(local_path)
                continue

            # --- read the zip's file listing (the AI brain needs this) ---
            zip_contents = list_zip_contents(local_path)
            os.remove(local_path)

            # --- the AI brain: analyze everything, decide all metadata ---
            slug, name, version, description, tags, plugin_type = (
                None, None, None, None, [], "aeplugin"
            )

            # Explicit body fields always win — these OVERRIDE the AI's
            # choices and apply to every zip on this release.
            if body_plugin_type:
                plugin_type = body_plugin_type
            if body_description:
                description = body_description
            if body_tags:
                tags = list(body_tags)

            need_ai = (slug is None or name is None or version is None
                       or description is None or not tags)
            ai_result = None
            if ai_enabled and need_ai:
                ai_result = analyze_with_ai(tag, title, body, asset_name,
                                            zip_contents, ai_token)
                if ai_result:
                    if slug is None and ai_result.get("id"):
                        slug = sanitize_slug(ai_result["id"])
                    if name is None and ai_result.get("name"):
                        name = str(ai_result["name"]).strip()
                    if version is None and ai_result.get("version"):
                        version = str(ai_result["version"]).strip()
                    if description is None and ai_result.get("description"):
                        description = str(ai_result["description"]).strip()
                    if not tags and ai_result.get("tags"):
                        suggested = ai_result["tags"]
                        if isinstance(suggested, list):
                            tags = [str(t).strip().lower() for t in suggested
                                    if str(t).strip()][:5]
                    # plugin_type: only override if the body didn't set it.
                    if not body_plugin_type and ai_result.get("plugin_type"):
                        pt = str(ai_result["plugin_type"]).strip().lower()
                        if pt in ("aeplugin", "core"):
                            plugin_type = pt
                    print(f"  ai: analyzed '{asset_name}' -> id={slug} v{version}")
                else:
                    print(f"  ai: brain failed for '{asset_name}' — using fallback",
                          file=sys.stderr)

            # --- heuristic fallbacks (safety net — always run) ---
            if not slug:
                # Derive from the asset filename (more specific than the
                # tag when a release has multiple zips).
                stem = Path(asset_name).stem
                slug = sanitize_slug(stem) or sanitize_slug(tag) or "plugin"
            if not name:
                name = title or slug_to_name(slug)
            if not version:
                version = "1.0"

            pub = rel.get("published_at") or ""
            last_updated = pub[:10] if pub else datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # --- dedup by slug (newer release wins) ---
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
