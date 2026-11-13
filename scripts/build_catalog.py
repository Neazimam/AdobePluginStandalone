#!/usr/bin/env python3
"""
Build After-Effect-HUB.json from THIS repo's own GitHub releases, using a
BUILT-IN BRAIN (pure Python, no AI API, no keys, no secrets).

This repo hosts ONLY standalone exe-installer plugins — a .zip containing
installer.exe (any name) [+ activator.exe]. The workflow catalogs each
release into a flat JSON array that the EAGLE app fetches as a secondary
catalog. Every entry has category="plugin" hardcoded.

THE BRAIN (no AI, no API, no keys)
---------------------------------
For each release, the brain reads everything it can see:
  - the release tag, title, body
  - the .zip asset filename
  - the LIST OF FILES INSIDE THE ZIP (names + sizes)

...and figures out the catalog metadata using pure-Python heuristics:
  - id (stable slug): derived from the installer .exe's name, stripped of
    common installer/version suffixes (e.g. "DeepGlow_Setup_v3.exe" -> "deepglow")
  - name (display name): the product name, title-cased
  - version: extracted from the tag/title/asset/exe filename via regex
  - description: built from the product name + inferred category + activator presence
  - tags: inferred from keyword matches in the exe/zip/title (glow, color, motion, ...)
  - plugin_type: always "standalone" — these are exe installers EAGLE must
    RUN (not plugin files to copy into the AE Plug-ins folder). The release
    body can override this if a release genuinely needs a different type.

The script handles the deterministic fields too:
  - sha256 (hash of the downloaded zip)
  - size_bytes (zip file size)
  - link (the release asset's download URL)
  - last_updated (the release's publish date)
  - category (always "plugin" — this repo hosts plugins only)

TAG FORMATS — any tag works (the brain reads the zip to figure out the product)
-----------------------------------------------------------------------------
  - standalone
  - ae-deepglow/1.1.1
  - DeepGlow_1.2
  - adobe-plugin-v2

If you set a field explicitly in the release body (one `key: value` per line),
that value OVERRIDES the brain's choice:
  plugin_type: standalone
  description: A fast GPU glow plugin for After Effects.
  tags: motion, glow, light, gpu

Run by .github/workflows/build-catalog.yml. Requires the gh CLI (authenticated
via GH_TOKEN — already there for git) and the GITHUB_REPOSITORY env var.

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

# Tag in the exact <slug>/<version> convention — used directly, no brain needed.
TAG_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)/(.+)$")

# Version patterns, tried in order. Matches 1.2, 1.2.3, 1.2.3.4, v1.2, V1.2.3,
# optionally with -beta / -rc1 / -alpha suffixes.
VERSION_RE = re.compile(
    r"v?(?P<ver>\d+\.\d+(?:\.\d+){0,2}(?:[-_.]?(?:beta|alpha|rc|pre)\d?)?)",
    re.IGNORECASE,
)

# Suffixes/substrings stripped from an installer .exe filename to reveal the
# product name. Order matters — longer/more-specific first. Matched case-
# insensitively, with any of [-_ ] as the leading separator allowed.
INSTALLER_SUFFIXES = [
    "_setup_v", "-setup-v", " setup v", "_setup_", "-setup-", " setup ",
    "_setup",   "-setup",   " setup",
    "_installer_v", "-installer-v", " installer v",
    "_installer_", "-installer-", " installer ",
    "_installer", "-installer", " installer",
    "_win_v",    "-win-v",    " win v",
    "_win64_v",  "-win64-v",  " win64 v",
    "_x64_v",    "-x64-v",    " x64 v",
    "_final_v",  "-final-v",  " final v",
    "_full_v",   "-full-v",   " full v",
    "_win64",    "-win64",    " win64",
    "_win32",    "-win32",    " win32",
    "_x64",      "-x64",      " x64",
    "_x86",      "-x86",      " x86",
    "_final",    "-final",    " final",
    "_full",     "-full",     " full",
    "_ae",       "-ae",       " ae",
    "_setup",    "-setup",    "setup",
    "_installer", "-installer", "installer",
    "_plugin",   "-plugin",   " plugin",
    "_v",        "-v",        " v",        # version prefix — must come late
    "_version",  "-version",  " version",
    # NOTE: a trailing "_v3" / "-v3" (digit after v) is handled by the
    # digit-cut fallback below, not by an entry here.
]

# Keyword -> tags to add when the keyword appears in any of the brain's inputs.
# Keys are matched case-insensitively as substrings.
KEYWORD_TAGS = {
    "glow":        ["glow", "light"],
    "light":       ["light"],
    "color":       ["color", "grade"],
    "colour":      ["color", "grade"],
    "grade":       ["grade", "color"],
    "grading":     ["grade", "color"],
    "mamba":       ["color", "grade"],
    "motion":      ["motion"],
    "move":        ["motion"],
    "particle":    ["particles"],
    "particles":   ["particles"],
    "blur":        ["blur"],
    "motionblur":  ["blur", "motion"],
    "motion-blur": ["blur", "motion"],
    "transition":  ["transition"],
    "transitions": ["transition"],
    "3d":          ["3d"],
    "3-d":         ["3d"],
    "depth":       ["depth", "3d"],
    "audio":       ["audio"],
    "sound":       ["audio"],
    "text":        ["text"],
    "titler":      ["text"],
    "title":       ["text"],
    "shape":       ["shape"],
    "shaper":      ["shape"],
    "flare":       ["lens", "light"],
    "lens":        ["lens"],
    "optics":      ["lens", "light"],
    "noise":       ["noise"],
    "denoise":     ["noise", "denoise"],
    "grain":       ["grain", "film"],
    "film":        ["film", "grain"],
    "shadow":      ["shadow"],
    "lighting":    ["light", "lighting"],
    "render":      ["render"],
    "renderer":    ["render"],
    "gpu":         ["gpu"],
    "cuda":        ["gpu"],
    "opencl":      ["gpu"],
    "metal":       ["gpu"],
    "script":      ["script"],
    "panel":       ["ui", "panel"],
    "ui":          ["ui"],
    "extension":   ["ui", "extension"],
    "fx":          ["fx"],
    "effect":      ["fx"],
    "effects":     ["fx"],
    "aer":         ["render"],
    "aescript":    ["script"],
    "plug-in":     ["plugin"],
    "plugin":      ["plugin"],
}


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


def first_zip_asset(assets):
    for a in assets or []:
        if (a.get("name") or "").lower().endswith(".zip"):
            return a
    return None


# ---------- THE BRAIN (pure Python heuristics) ----------

def sanitize_slug(text):
    """Force a string into a URL-safe slug: lowercase + hyphens."""
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def title_case_name(text):
    """Turn 'deepglow' / 'deep_glow' / 'deep-glow' -> 'DeepGlow' (joined).

    For two-word product names we keep them joined as one capitalized token,
    matching how AE plugins are typically branded ('DeepGlow', 'ColorMamba').
    """
    parts = re.split(r"[-_\s]+", (text or "").strip())
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "".join(p[:1].upper() + p[1:] for p in parts)


def extract_version(*candidates):
    """Try to find a version string in any of the candidate strings.
    Returns the first match (e.g. '1.2.3') or None."""
    for text in candidates:
        if not text:
            continue
        m = VERSION_RE.search(text)
        if m:
            ver = m.group("ver").strip()
            # Normalize separators in the suffix (1.2.3-beta vs 1.2.3.beta).
            return ver
    return None


def strip_installer_suffix(filename):
    """Remove the .exe extension and any installer/version suffixes to reveal
    the product name.

    Examples:
      'DeepGlow_Setup_v3.exe'  -> 'DeepGlow'
      'Saber1.0.40.exe'       -> 'Saber'        (version glued, no separator)
      'ColorMamba_3.1.exe'    -> 'ColorMamba'
      'ParticleFX-v2.0.exe'   -> 'ParticleFX'
    """
    # Drop the .exe extension first.
    name = re.sub(r"\.exe$", "", filename, flags=re.IGNORECASE)

    # 1) Cut at the first version pattern. This handles BOTH separated
    #    ('DeepGlow_1.2.3') and glued ('Saber1.0.40') versions — and it's the
    #    most reliable signal because product names rarely contain a dotted
    #    digit run. VERSION_RE requires a dotted version (1.2, 1.2.3, ...).
    vm = VERSION_RE.search(name)
    if vm and vm.start() > 0:
        candidate = name[:vm.start()].rstrip("-_ .")
        if candidate and any(c.isalpha() for c in candidate):
            return candidate

    # 2) Try each known installer suffix pattern (case-insensitive). Stop at
    #    the first that matches.
    lowered = name.lower()
    for suf in INSTALLER_SUFFIXES:
        idx = lowered.find(suf.lower())
        if idx > 0:
            candidate = name[:idx]
            if candidate and any(c.isalnum() for c in candidate):
                return candidate

    # 3) Fallback: cut at the first digit run preceded by a separator. Catches
    #    a bare integer version like 'DeepGlow_3' or 'DeepGlow 3' (no dot).
    m = re.search(r"[-_\s]\d", name)
    if m:
        candidate = name[:m.start()]
        if candidate and any(c.isalnum() for c in candidate):
            return candidate

    return name


def find_installer_exe(zip_contents):
    """Pick the most likely installer .exe from the zip's file list.
    Returns the bare filename (no path) or None.

    Heuristic: the installer is usually the largest .exe that is NOT named
    'activator.exe' / 'crack.exe' / 'patch.exe' / 'keygen.exe'.
    """
    exe_candidates = []
    for entry in zip_contents:
        # zip_contents entries look like "filename (size bytes)" — split that.
        # Take everything before the last " (".
        bare = entry.rsplit(" (", 1)[0].strip()
        # Strip directory components.
        bare = bare.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not bare.lower().endswith(".exe"):
            continue
        stem = bare[:-4].lower()
        if stem in ("activator", "crack", "patch", "keygen", "loader",
                    "register", "license", "serial"):
            continue
        # Pull the size out if present.
        size = 0
        size_m = re.search(r"\((\d+)\s*bytes?\)", entry)
        if size_m:
            size = int(size_m.group(1))
        exe_candidates.append((bare, size))
    if not exe_candidates:
        return None
    # Largest .exe wins — the installer is almost always the biggest.
    exe_candidates.sort(key=lambda c: c[1], reverse=True)
    return exe_candidates[0][0]


def has_activator(zip_contents):
    """True if the zip contains an activator-like .exe."""
    for entry in zip_contents:
        bare = entry.rsplit(" (", 1)[0].strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = bare.lower()
        if stem.endswith(".exe"):
            stem = stem[:-4]
        if stem in ("activator", "crack", "patch", "keygen", "loader",
                    "register", "license"):
            return True
    return False


def infer_tags(*candidates):
    """Scan the candidate strings for known keywords and collect tags.
    Returns a de-duplicated, ordered list of lowercase tags (max 5)."""
    haystack = " ".join(c or "" for c in candidates).lower()
    tags = []
    seen = set()
    for keyword, tag_list in KEYWORD_TAGS.items():
        if keyword in haystack:
            for t in tag_list:
                if t not in seen:
                    seen.add(t)
                    tags.append(t)
        if len(tags) >= 5:
            break
    return tags[:5]


def build_description(name, tags, activator, slug=""):
    """Assemble a one-sentence description from the inferred pieces.

    Only 'real' category tags (glow, color, motion, ...) shape the description.
    Metadata tags (activator, plugin, after-effects) and the product's OWN slug
    (added as a tag so the entry is searchable by name) are filtered out, since
    the slug isn't a category — describing "Saber" as "a saber plugin" is noise.
    """
    excluded = {"activator", "plugin", "after-effects"}
    if slug:
        excluded.add(slug.lower())
    category_tags = [t for t in tags if t.lower() not in excluded]
    if category_tags:
        primary = category_tags[0]
        article = "an" if primary[0] in "aeiou" else "a"
        desc = f"{name} — {article} {primary} plugin for After Effects."
    else:
        desc = f"{name} — a plugin for After Effects."
    if activator:
        desc += " Includes activator for licensing."
    return desc


def analyze_with_brain(tag, title, body, asset_name, zip_contents):
    """The built-in brain. Reads everything and returns the catalog metadata.

    Pure Python — no API, no network, no keys. Always returns a complete
    dict; never raises.
    """
    installer_exe = find_installer_exe(zip_contents)
    activator = has_activator(zip_contents)

    # --- name + slug (prefer the installer exe's product name) ---
    if installer_exe:
        product = strip_installer_suffix(installer_exe)
    else:
        product = ""
    if not product:
        product = title or ""
    if not product:
        product = asset_name or ""
        product = re.sub(r"\.zip$", "", product, flags=re.IGNORECASE)

    name = title_case_name(product) or (title or "Plugin")
    slug = sanitize_slug(product) or sanitize_slug(tag) or "plugin"

    # --- version (scan tag, title, asset, installer exe, in that order) ---
    version = extract_version(tag, title, asset_name, installer_exe) or "1.0"

    # --- tags (scan everything for keywords) ---
    tags = infer_tags(installer_exe or "", asset_name or "", title or "", tag or "")
    # The activator tag is metadata, not a real category. Track keyword tags
    # separately so the default-tag fallback fires when nothing real was found.
    keyword_tags = list(tags)
    if activator and "activator" not in tags:
        tags.append("activator")
    if not keyword_tags:
        # No real category keywords matched. Use the generic defaults PLUS the
        # product slug as a tag, so the entry is searchable by its own name
        # (e.g. a 'Saber' plugin with no keyword match can still be found by
        # searching 'saber').
        name_tag = slug if slug and slug not in ("plugin",) else ""
        tags = ["plugin", "after-effects"]
        if name_tag:
            tags.append(name_tag)
        if activator:
            tags.append("activator")

    # --- plugin_type ---
    # --- plugin_type ---
    # This repo hosts ONLY standalone exe installers — EAGLE must RUN the
    # exe (and activator if present), not copy files into the AE Plug-ins
    # folder. So the default is always "standalone". The release body can
    # still override it if a release genuinely needs a different type.
    plugin_type = "standalone"

    # --- description ---
    description = build_description(name, tags, activator, slug)

    return {
        "id": slug,
        "name": name,
        "version": version,
        "description": description,
        "tags": tags,
        "plugin_type": plugin_type,
    }


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


def list_zip_contents(path, max_files=40):
    """Return a list of 'filename (size bytes)' strings for files in the zip.
    Capped at max_files so huge zips don't blow up the brain's view."""
    contents = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                contents.append(f"{info.filename} ({info.file_size} bytes)")
                if len(contents) >= max_files:
                    total = len(zf.namelist())
                    contents.append(f"... ({total - max_files} more files)")
                    break
    except (zipfile.BadZipFile, OSError) as e:
        print(f"  could not read zip contents: {e}", file=sys.stderr)
    return contents


# ---------- main ----------

def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        sys.exit("GITHUB_REPOSITORY env var is not set.")

    print("Brain: built-in (no API, no keys).")

    releases = list_releases(repo)
    releases.sort(key=lambda r: r.get("published_at") or "")

    entries = []
    seen_ids = set()
    tmp_dir = "/tmp/catalog-assets"

    for rel in releases:
        tag = rel.get("tag_name", "")
        title = (rel.get("name") or "").strip()
        body = rel.get("body", "")

        asset = first_zip_asset(rel.get("assets"))
        if not asset:
            print(f"  skip: no .zip asset on release '{tag}'", file=sys.stderr)
            continue

        asset_name = asset.get("name", "")

        # --- download + validate the zip ---
        local_path = download_asset(tag, asset_name, tmp_dir)
        sha, size = file_sha256_and_size(local_path)

        if not zip_has_exe(local_path):
            print(f"  skip: zip '{asset_name}' on '{tag}' contains no .exe — "
                  f"this repo hosts exe/activator zips only", file=sys.stderr)
            os.remove(local_path)
            continue

        # --- read the zip's file listing (the brain's eyes) ---
        zip_contents = list_zip_contents(local_path)
        os.remove(local_path)

        # --- the brain: analyze everything ---
        result = analyze_with_brain(tag, title, body, asset_name, zip_contents)
        slug = result["id"]
        name = result["name"]
        version = result["version"]
        description = result["description"]
        tags = result["tags"]
        plugin_type = result["plugin_type"]

        # --- explicit body fields OVERRIDE the brain ---
        body_meta = parse_body(body)
        if body_meta.get("plugin_type"):
            plugin_type = body_meta["plugin_type"]
        if body_meta.get("description"):
            description = body_meta["description"]
        if body_meta.get("tags"):
            tags = [t.strip() for t in body_meta["tags"].split(",") if t.strip()]

        # If the tag is the exact <slug>/<version> convention, prefer that.
        m = TAG_RE.match(tag or "")
        if m:
            slug = m.group(1)
            version = m.group(2)

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
        print(f"  ok: {slug} {version} ({name}) — {asset_name} ({size} bytes)")

    entries.sort(key=lambda e: e["name"].lower())

    Path(CATALOG_FILE).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(entries)} entries to {CATALOG_FILE}")


if __name__ == "__main__":
    main()
