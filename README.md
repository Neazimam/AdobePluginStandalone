# AdobePluginStandalone — auto-catalog with an AI brain

This repo hosts ONLY standalone exe-installer plugins (a .zip containing
`installer.exe` [+ `activator.exe`]). The workflow catalogs each release into
`After-Effect-HUB.json` automatically, using an **AI brain** (GitHub Models,
free) that analyzes each release and decides all the catalog metadata for you.

The EAGLE app fetches that JSON as a **secondary catalog** and merges it with
the primary one, so these plugins show up alongside the AdobePlugins catalog.

## Files

```
.github/workflows/build-catalog.yml   # the workflow (runs on release/push/manual)
scripts/build_catalog.py              # the catalog builder + AI brain
```

## 1. Add the two files to the repo

Copy these into the `AdobePluginStandalone` repo (root = the repo's top level):

```
.github/workflows/build-catalog.yml
scripts/build_catalog.py
```

Commit + push. The workflow uses the built-in `GITHUB_TOKEN` — no PAT, no
secrets required for the basics.

## 2. How the AI brain works

For each release, the script sends the AI a **complete picture** of what was
uploaded:

| Input to the AI brain | Where it comes from |
|---|---|
| Release tag | the git tag (e.g. `standalone`) |
| Release title | the release's title field |
| Release body | the release's description text |
| Zip asset filename | the `.zip` file's name |
| **Files inside the zip** | the script opens the zip and lists every file + size |

The AI then reads all of that and decides **every human-meaningful field** of
the catalog entry:

| AI decides | What it looks at |
|---|---|
| `id` (stable slug) | the installer exe's filename, the tag, the title |
| `name` (display name) | the product name (proper capitalization) |
| `version` | extracted from tag/title/asset/exe filename |
| `description` | one sentence about what the plugin does |
| `tags` | 3-5 relevant lowercase tags |
| `plugin_type` | `aeplugin` (default) or `core` for MediaCore plugins |

The script handles **only the deterministic fields** the AI can't know:

| Script computes | How |
|---|---|
| `sha256` | hashing the downloaded .zip |
| `size_bytes` | the .zip file size |
| `link` | the release asset's download URL |
| `last_updated` | the release's publish date |
| `category` | always `plugin` (this repo hosts plugins only) |

### Why the zip contents matter

The `.exe` filename inside the zip usually reveals the product. For example,
if the zip contains `DeepGlow_Setup.exe` + `activator.exe`, the AI brain
identifies the product as "DeepGlow" and generates:

```json
{
  "id": "deepglow",
  "name": "DeepGlow",
  "version": "1.1.1",
  "description": "GPU-accelerated glow effect for After Effects compositions.",
  "tags": ["glow", "light", "gpu", "motion"],
  "plugin_type": "aeplugin"
}
```

So you can upload a release with tag `standalone`, no title, no body — and the
AI brain figures out what the plugin is from the files inside the zip.

## 3. AI setup (optional but recommended)

The AI brain uses **GitHub Models** (free, public preview). It calls the
`gpt-4o-mini` model by default (override with the `AI_MODEL` repo variable).

The script tries the AI token in this order:
1. `AI_TOKEN` secret (a GitHub PAT with `models:read` scope)
2. `GITHUB_TOKEN` (the auto-provided Actions token)

If neither works, the script **falls back to heuristics** (tag as slug,
`"1.0"` version, empty description) so the workflow never fails. AI is the
brain, heuristics are the safety net.

### To enable AI with the default token

Usually nothing extra is needed — the `GITHUB_TOKEN` is already passed. Make
sure your account has GitHub Models access (free, public preview).

### If the default token doesn't work

Create a PAT with `models:read` scope and add it as a repo secret:

1. `https://github.com/settings/tokens` → **Generate new token (classic)**
2. Scope: select **`models:read`** (and `repo` if the repo is private)
3. Copy the token
4. Repo → **Settings → Secrets and variables → Actions → New repository secret**
5. Name: `AI_TOKEN` → paste the token → **Add secret**

### To switch the model

Set a repo variable (not a secret) named `AI_MODEL`:
1. Repo → **Settings → Secrets and variables → Actions → Variables tab**
2. **New variable** → Name: `AI_MODEL` → Value: e.g. `gpt-4o` → **Add variable**

Available models include `gpt-4o-mini` (default, fast), `gpt-4o` (smarter,
slower), `Phi-3.5-mini-instruct`, `Mistral-large`, etc.

## 4. Upload releases — any tag works

The AI brain reads the tag + everything else and decides the slug + version
itself. So **any tag format is accepted**:

| Tag | What happens |
|---|---|
| `standalone` | AI brain identifies the product from the zip contents |
| `ae-deepglow/1.1.1` | AI uses this directly (id=`ae-deepglow`, version=`1.1.1`) |
| `DeepGlow_1.2` | AI parses the slug + version from the tag |
| `adobe-plugin-v2` | AI derives slug `adobe-plugin` + version `2` |

### The release body (optional — AI fills in if missing)

You can override the AI's choices by setting fields explicitly in the body,
one `key: value` per line:

```
plugin_type: core
description: A fast GPU glow plugin for After Effects.
tags: motion, glow, light, gpu
```

Any field you set here **overrides** the AI — the AI only decides what's
missing. This is useful when you want to lock in a specific description or
tags.

### The asset

Attach **one .zip** to the release. The first `.zip` asset is used. Inside the
zip put your `installer.exe` (any name) and, if activation is needed,
`activator.exe` (exact name). EAGLE runs the installer automatically and shows
an **Activate** button when `activator.exe` is present.

The zip **must contain at least one `.exe`** — the workflow validates this and
skips any release whose zip has no `.exe` inside.

### Worked example

You upload a release:
- Tag: `standalone`
- Title: (empty)
- Body: (empty)
- Asset: `MyPlugin.zip` containing `MyPlugin_Setup.exe` + `activator.exe`

The AI brain sees the zip contains `MyPlugin_Setup.exe` + `activator.exe`, and
produces:
```json
{
  "id": "myplugin",
  "category": "plugin",
  "name": "MyPlugin",
  "version": "1.0",
  "last_updated": "2025-11-13",
  "link": "https://github.com/.../MyPlugin.zip",
  "plugin_type": "aeplugin",
  "description": "After Effects plugin with activator-based licensing.",
  "sha256": "a3f5b2...",
  "size_bytes": 12345678,
  "tags": ["plugin", "ae", "installer", "activator"]
}
```

No manual metadata entry needed — the brain does it.

## 5. Verify the workflow

1. Push a release. The **Build Catalog** action runs automatically.
2. Open the **Actions** tab → **Build Catalog** → watch it go green.
3. The workflow commits `After-Effect-HUB.json` to `main` with the entry.
4. Re-run manually any time from **Actions → Build Catalog → Run workflow**.

### Reading the logs

The log tells you exactly what the brain did for each release:
- `AI brain enabled (model: gpt-4o-mini).` — brain is active
- `ai: analyzed 'standalone' -> id=myplugin v1.0` — the brain decided the metadata
- `ok: myplugin 1.0 — MyPlugin.zip (12345 bytes)` — entry added to the catalog
- `ai: brain failed for 'standalone' — using fallback` — AI call failed, using heuristics
- `skip: zip '...' contains no .exe` — the zip had no .exe inside
- `skip: no .zip asset on release '...'` — no .zip attached

### Common issues

- **`AI brain disabled (no token)`** → no token available. The brain is skipped;
  heuristics are used. The catalog still builds — just without AI-generated
  descriptions.
- **`AI HTTP 401`** → the token doesn't have GitHub Models access. Add an
  `AI_TOKEN` secret (a PAT with `models:read` scope).
- **`AI HTTP 429`** → rate-limited. The script falls back for that release.
  Re-run the workflow later.
- **Release missing from JSON** → check the log for `skip:` lines.

## 6. Wire it into EAGLE as the secondary catalog

The EAGLE source fetches catalogs from URL constants in
`src/catalog/fetch.cpp`. Set `k_secondary_url` to the raw URL of this repo's
JSON:

```cpp
constexpr const char* k_secondary_url =
    "https://raw.githubusercontent.com/Neazimam/AdobePluginStandalone/"
    "refs/heads/main/After-Effect-HUB.json";
```

The fetch logic **merges** every catalog URL (it no longer falls back), so both
the primary (AdobePlugins) and this secondary (Standalone) catalogs appear
together in the app. Items are de-duplicated by `id`, so if the same slug
exists in both catalogs the primary wins.

Rebuild the EAGLE exe after this change.
