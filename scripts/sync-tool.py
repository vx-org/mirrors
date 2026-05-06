#!/usr/bin/env python3
"""
sync-tool.py — Mirror a tool's GitHub releases into vx-org/mirrors.

Permanently archives ALL versions (keep_versions: 0 = keep forever).
Never overwrites or deletes existing mirror releases.

Usage:
    python scripts/sync-tool.py mirrors/ffmpeg/sync.yml
    python scripts/sync-tool.py mirrors/witr/sync.yml

Env:
    GH_TOKEN   GitHub token with write access to vx-org/mirrors
    DRY_RUN=1  Print what would happen, create nothing
    FORCE=1    Re-mirror even if release already exists
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

try:
    import yaml  # PyYAML — available on GitHub Actions ubuntu runners
    def load_yaml(path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)
except ImportError:
    # Minimal fallback: parse simple YAML without PyYAML
    def load_yaml(path: str) -> dict:  # type: ignore[misc]
        import re as _re
        result: dict = {}
        stack: list[tuple[int, dict]] = [(0, result)]

        with open(path) as f:
            for raw in f:
                line = raw.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                stripped = line.lstrip()

                # List item
                if stripped.startswith("- "):
                    content = stripped[2:]
                    kv = _re.match(r"^(\w[\w_-]*):\s*(.*)", content)
                    parent = stack[-1][1]
                    if not isinstance(parent, list):
                        # find nearest list parent or convert
                        pass
                    item: dict | str = {}
                    if kv:
                        item = {kv.group(1): kv.group(2).strip('"').strip("'")}
                    else:
                        item = content.strip('"').strip("'")
                    if isinstance(parent, list):
                        parent.append(item)
                    continue

                kv_m = _re.match(r"^([\w_-]+):\s*(.*)", stripped)
                if not kv_m:
                    continue

                key = kv_m.group(1)
                val_raw = kv_m.group(2).strip().strip('"').strip("'")

                # Pop stack to correct indent level
                while len(stack) > 1 and stack[-1][0] >= indent:
                    stack.pop()

                parent_dict = stack[-1][1]
                if not isinstance(parent_dict, dict):
                    continue

                if val_raw == "" or val_raw == "|" or val_raw == ">":
                    new_dict: dict = {}
                    parent_dict[key] = new_dict
                    stack.append((indent + 2, new_dict))
                else:
                    # Type coercion
                    if val_raw.lower() == "true":
                        parent_dict[key] = True
                    elif val_raw.lower() == "false":
                        parent_dict[key] = False
                    elif _re.match(r"^\d+$", val_raw):
                        parent_dict[key] = int(val_raw)
                    else:
                        parent_dict[key] = val_raw
        return result


def gh(args: list[str], capture: bool = True) -> str:
    """Run a gh CLI command."""
    cmd = ["gh"] + args
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed:\n{r.stderr}")
        return r.stdout.strip()
    else:
        subprocess.run(cmd, check=True)
        return ""


def gh_json(args: list[str]) -> object:
    return json.loads(gh(args))


# ---------------------------------------------------------------------------
# Fast release existence check via pre-fetched tag set
# ---------------------------------------------------------------------------

_MIRROR_TAGS_CACHE: set[str] | None = None

def _load_mirror_tags(repo: str) -> set[str]:
    """Fetch all existing release tags from the mirror repo in one API call."""
    global _MIRROR_TAGS_CACHE
    if _MIRROR_TAGS_CACHE is not None:
        return _MIRROR_TAGS_CACHE
    tags: set[str] = set()
    page = 1
    print(f"  [cache] Loading existing mirror releases from {repo} ...")
    while True:
        data = gh_json(["api",
            f"repos/{repo}/releases?per_page=100&page={page}"])
        assert isinstance(data, list)
        if not data:
            break
        for r in data:
            tags.add(r["tag_name"])
        page += 1
        if len(data) < 100:
            break
    _MIRROR_TAGS_CACHE = tags
    print(f"  [cache] {len(tags)} existing releases cached — skipping will be instant")
    return tags


def release_exists(repo: str, tag: str) -> bool:
    """Fast O(1) check using pre-fetched tag cache."""
    return tag in _load_mirror_tags(repo)


def mark_release_created(tag: str) -> None:
    """Update the in-memory cache after a new release is created."""
    if _MIRROR_TAGS_CACHE is not None:
        _MIRROR_TAGS_CACHE.add(tag)


def download_file(url: str, dest: Path) -> bool:
    """Download url to dest, following redirects. Uses curl if available (more robust)."""
    for attempt in range(3):
        try:
            # Use curl for better redirect handling and progress
            r = subprocess.run(
                ["curl", "-fsSL", "--retry", "2", "--max-time", "300",
                 "-o", str(dest), url],
                capture_output=True, text=True
            )
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return True
            # Fallback to urllib
            req = urllib.request.Request(url, headers={"User-Agent": "vx-mirrors/1.0"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception as e:
            print(f"    [retry {attempt+1}/3] {e}")
    return False


def asset_matches(name: str, pattern: str, version: str) -> bool:
    """Check if an asset name matches the pattern with {semver}/{version} replaced."""
    regex = pattern
    regex = regex.replace("{semver}", r"[0-9]+\.[0-9]+(\.[0-9]+)?")
    regex = regex.replace("{version}", re.escape(version))
    return bool(re.fullmatch(regex, name))


def sync_multi_version(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """
    Generic multi-version handler: a SINGLE upstream release contains assets for
    MULTIPLE tool versions. Extract each version via named regex group 'version'.

    Used for: python (astral-sh/python-build-standalone), etc.
    """
    tool          = cfg["tool"]
    upstream      = cfg["upstream"]
    up_owner      = upstream["owner"]
    up_repo       = upstream["repo"]
    up_tag_prefix = upstream.get("tag_prefix", "")
    inc_pre       = upstream.get("include_prerelease", False)
    asset_patterns = upstream.get("asset_patterns", [])
    mirror_prefix = cfg["tag_prefix"]

    print("=" * 64)
    print(f"  Tool    : {tool}  (multi_version mode)")
    print(f"  Upstream: {up_owner}/{up_repo}")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    # Fetch latest releases (paginated)
    all_releases: list[dict] = []
    page = 1
    while True:
        data = gh_json(["api",
            f"repos/{up_owner}/{up_repo}/releases?per_page=100&page={page}"])
        assert isinstance(data, list)
        if not data:
            break
        all_releases.extend(data)
        print(f"  Page {page}: {len(data)} releases")
        page += 1
        if len(data) < 100:
            break

    releases = [r for r in all_releases if inc_pre or not r["prerelease"]]
    print(f"  Total releases to scan: {len(releases)}")

    # Collect all versions seen across all releases (avoid duplicate mirror releases)
    all_versions: dict[str, list[tuple[str, str, str]]] = {}  # version → [(aname, url, rename)]

    for rel in releases:
        up_tag = rel["tag_name"]
        assets_map = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}

        for aname, aurl in assets_map.items():
            for pat_def in asset_patterns:
                regex   = pat_def["regex"]
                rename_tpl = pat_def.get("rename", "")
                m = re.fullmatch(regex, aname)
                if m:
                    ver = m.group("version")
                    rename = rename_tpl.replace("{version}", ver) if rename_tpl else aname
                    # Only keep the first (newest) occurrence per version+platform
                    key_name = rename or aname
                    entries = all_versions.setdefault(ver, [])
                    if not any(e[2] == key_name for e in entries):
                        entries.append((aname, aurl, key_name))
                    break

    versions = sorted(all_versions.keys(),
                      key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()),
                      reverse=True)
    print(f"  Detected {len(versions)} distinct versions")

    synced = skipped = failed = 0

    for version in versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for aname, aurl, dest_name in all_versions[version]:
                dest = tmp / dest_name
                print(f"    DL  {aname} → {dest_name}")
                if dry_run:
                    dest.touch()
                elif not download_file(aurl, dest):
                    print(f"    [WARN] Download failed: {aname}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## {tool} {version}\n\n"
                f"**Source:** https://github.com/{up_owner}/{up_repo}\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo,
                                            uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_btbn_ffmpeg(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """
    Special handler for BtbN/FFmpeg-Builds.
    All versioned assets live in the single "latest" release.
    We extract each version from asset names and mirror them separately.
    """
    tool          = cfg["tool"]
    upstream      = cfg["upstream"]
    up_owner      = upstream["owner"]
    up_repo       = upstream["repo"]
    release_tag   = upstream.get("release_tag", "latest")
    asset_patterns = upstream.get("asset_patterns", [])
    mirror_prefix = cfg["tag_prefix"]

    print("=" * 64)
    print(f"  Tool    : {tool}  (btbn_ffmpeg mode)")
    print(f"  Upstream: {up_owner}/{up_repo}  tag={release_tag}")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    # Fetch the "latest" release assets
    rel = gh_json(["api", f"repos/{up_owner}/{up_repo}/releases/tags/{release_tag}"])
    assert isinstance(rel, dict)
    assets_map = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    print(f"\n  Found {len(assets_map)} assets in '{release_tag}' release.")

    # Group assets by version
    # key: version string, value: list of (asset_name, url, rename)
    version_assets: dict[str, list[tuple[str, str, str]]] = {}
    for aname, aurl in assets_map.items():
        for pat_def in asset_patterns:
            regex = pat_def["regex"]
            rename_tpl = pat_def.get("rename", "")
            m = re.fullmatch(regex, aname)
            if m:
                ver = m.group("version")
                rename = rename_tpl.replace("{version}", ver) if rename_tpl else aname
                version_assets.setdefault(ver, []).append((aname, aurl, rename))
                break

    versions = sorted(version_assets.keys(), reverse=True)
    print(f"  Detected versions: {versions}")

    synced = skipped = failed = 0

    for version in versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for aname, aurl, dest_name in version_assets[version]:
                dest = tmp / dest_name
                print(f"    DL  {aname} → {dest_name}")
                if dry_run:
                    dest.touch()
                elif not download_file(aurl, dest):
                    print(f"    [WARN] Download failed: {aname}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## {tool} {version}\n\n"
                f"**Source:** https://ffmpeg.org  "
                f"**Builds by:** https://github.com/{up_owner}/{up_repo}\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )

            if dry_run:
                print(f"    [DRY RUN] Would create {mirror_tag} with {len(uploaded)} assets.")
                synced += 1
                continue

            cmd = [
                "gh", "release", "create", mirror_tag,
                "--repo", mirror_repo,
                "--title", f"{tool} {version}",
                "--notes", notes,
            ] + [str(f) for f in uploaded]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                print(f"    [OK] {mirror_tag} created.")
                synced += 1
            else:
                print(f"    [FAIL] {r.stderr.strip()}")
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def _create_mirror_release(
    tool: str,
    version: str,
    mirror_tag: str,
    mirror_repo: str,
    uploaded: list,
    notes: str,
    dry_run: bool,
) -> str:
    """Create a GitHub release in the mirror repo. Returns 'ok', 'dry', or 'fail'."""
    if dry_run:
        print(f"    [DRY RUN] Would create {mirror_tag} with {len(uploaded)} assets.")
        return "dry"
    cmd = [
        "gh", "release", "create", mirror_tag,
        "--repo", mirror_repo,
        "--title", f"{tool} {version}",
        "--notes", notes,
    ] + [str(f) for f in uploaded]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"    [OK] {mirror_tag} created.")
        mark_release_created(mirror_tag)
        return "ok"
    else:
        print(f"    [FAIL] {r.stderr.strip()}")
        return "fail"


def sync_nodejs_org(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for nodejs.org official CDN."""
    tool         = cfg["tool"]
    mirror_prefix = cfg["tag_prefix"]

    PLATFORMS = [
        ("windows", "x64",   "win-x64",       "zip"),
        ("linux",   "x64",   "linux-x64",      "tar.xz"),
        ("linux",   "arm64", "linux-arm64",    "tar.xz"),
        ("macos",   "x64",   "darwin-x64",     "tar.gz"),
        ("macos",   "arm64", "darwin-arm64",   "tar.gz"),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (nodejs_org mode)")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as resp:
        releases = json.loads(resp.read())

    versions = [r["version"].lstrip("v") for r in releases]
    print(f"  Found {len(versions)} versions on nodejs.org")

    synced = skipped = failed = 0

    for version in versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, platform_str, ext in PLATFORMS:
                filename = f"node-v{version}-{platform_str}.{ext}"
                url = f"https://nodejs.org/dist/v{version}/{filename}"
                dest = tmp / filename
                print(f"    DL  {url}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {filename}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## Node.js {version}\n\n"
                f"**Source:** https://nodejs.org/dist/v{version}/\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_go_dev(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for go.dev official CDN."""
    tool         = cfg["tool"]
    mirror_prefix = cfg["tag_prefix"]

    PLATFORMS = [
        ("windows", "x64",   "windows", "amd64", "zip"),
        ("linux",   "x64",   "linux",   "amd64", "tar.gz"),
        ("linux",   "arm64", "linux",   "arm64", "tar.gz"),
        ("macos",   "x64",   "darwin",  "amd64", "tar.gz"),
        ("macos",   "arm64", "darwin",  "arm64", "tar.gz"),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (go_dev mode)")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    with urllib.request.urlopen(
        "https://go.dev/dl/?mode=json&include=all", timeout=30
    ) as resp:
        releases = json.loads(resp.read())

    versions = [r["version"].lstrip("go") for r in releases if r.get("stable")]
    print(f"  Found {len(versions)} stable versions on go.dev")

    synced = skipped = failed = 0

    for version in versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, go_os, go_arch, ext in PLATFORMS:
                filename = f"go{version}.{go_os}-{go_arch}.{ext}"
                url = f"https://go.dev/dl/{filename}"
                dest = tmp / filename
                print(f"    DL  {url}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {filename}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## Go {version}\n\n"
                f"**Source:** https://go.dev/dl/\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_rust_rustup(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for rustup-init from static.rust-lang.org."""
    tool          = cfg["tool"]
    upstream      = cfg["upstream"]
    up_owner      = upstream.get("owner", "rust-lang")
    up_repo       = upstream.get("repo", "rustup")
    mirror_prefix = cfg["tag_prefix"]

    # (os_label, arch_label, triple, exe_suffix)
    PLATFORMS = [
        ("windows", "x64",   "x86_64-pc-windows-msvc",      ".exe"),
        ("linux",   "x64",   "x86_64-unknown-linux-gnu",     ""),
        ("linux",   "arm64", "aarch64-unknown-linux-gnu",    ""),
        ("macos",   "x64",   "x86_64-apple-darwin",          ""),
        ("macos",   "arm64", "aarch64-apple-darwin",         ""),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (rust_rustup mode)")
    print(f"  Upstream: {up_owner}/{up_repo}")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    # Fetch versions from GitHub releases
    all_versions: list[str] = []
    page = 1
    while True:
        data = gh_json([
            "api",
            f"repos/{up_owner}/{up_repo}/releases?per_page=100&page={page}"
        ])
        assert isinstance(data, list)
        if not data:
            break
        for r in data:
            tag = r["tag_name"]
            if not r.get("prerelease"):
                all_versions.append(tag.lstrip("v"))
        if len(data) < 100:
            break
        page += 1
    print(f"  Found {len(all_versions)} versions from GitHub releases")

    synced = skipped = failed = 0

    for version in all_versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, triple, exe_suffix in PLATFORMS:
                src_name  = f"rustup-init{exe_suffix}"
                dest_name = f"rustup-{version}-{os_name}-{arch}{exe_suffix}"
                url = (
                    f"https://static.rust-lang.org/rustup/archive"
                    f"/{version}/{triple}/{src_name}"
                )
                dest = tmp / dest_name
                print(f"    DL  {url}  → {dest_name}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {url}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## rustup {version}\n\n"
                f"**Source:** https://static.rust-lang.org/rustup/archive/{version}/\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_hashicorp(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for HashiCorp tools (terraform, vault, etc.) from releases.hashicorp.com."""
    tool          = cfg["tool"]
    upstream      = cfg["upstream"]
    up_owner      = upstream.get("owner", "hashicorp")
    up_repo       = upstream.get("repo", tool)
    mirror_prefix = cfg["tag_prefix"]

    # (os_label, arch_label, hc_os, hc_arch)
    PLATFORMS = [
        ("windows", "x64",   "windows", "amd64"),
        ("linux",   "x64",   "linux",   "amd64"),
        ("linux",   "arm64", "linux",   "arm64"),
        ("macos",   "x64",   "darwin",  "amd64"),
        ("macos",   "arm64", "darwin",  "arm64"),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (hashicorp mode)")
    print(f"  Upstream: {up_owner}/{up_repo}")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    all_versions: list[str] = []
    page = 1
    while True:
        data = gh_json([
            "api",
            f"repos/{up_owner}/{up_repo}/releases?per_page=100&page={page}"
        ])
        assert isinstance(data, list)
        if not data:
            break
        for r in data:
            if not r.get("prerelease"):
                all_versions.append(r["tag_name"].lstrip("v"))
        if len(data) < 100:
            break
        page += 1
    print(f"  Found {len(all_versions)} versions from GitHub releases")

    synced = skipped = failed = 0

    for version in all_versions:
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, hc_os, hc_arch in PLATFORMS:
                filename = f"{up_repo}_{version}_{hc_os}_{hc_arch}.zip"
                url = (
                    f"https://releases.hashicorp.com"
                    f"/{up_repo}/{version}/{filename}"
                )
                dest = tmp / filename
                print(f"    DL  {url}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {filename}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## {tool} {version}\n\n"
                f"**Source:** https://releases.hashicorp.com/{up_repo}/{version}/\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_adoptium(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for Eclipse Temurin JDK from Adoptium API."""
    tool          = cfg["tool"]
    mirror_prefix = cfg["tag_prefix"]

    # (os_label, arch_label, adoptium_os, adoptium_arch, ext)
    PLATFORMS = [
        ("windows", "x64",   "windows", "x64",     "zip"),
        ("linux",   "x64",   "linux",   "x64",     "tar.gz"),
        ("linux",   "arm64", "linux",   "aarch64", "tar.gz"),
        ("macos",   "x64",   "mac",     "x64",     "tar.gz"),
        ("macos",   "arm64", "mac",     "aarch64", "tar.gz"),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (adoptium mode)")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    with urllib.request.urlopen(
        "https://api.adoptium.net/v3/info/available_releases", timeout=30
    ) as resp:
        release_info = json.loads(resp.read())

    lts_versions: list[int] = release_info.get("available_lts_releases", [])
    print(f"  LTS versions: {lts_versions}")

    synced = skipped = failed = 0

    for major in lts_versions:
        version = str(major)
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, ad_os, ad_arch, ext in PLATFORMS:
                dest_name = f"jdk-{major}-{os_name}-{arch}.{ext}"
                url = (
                    f"https://api.adoptium.net/v3/binary/latest"
                    f"/{major}/ga/{ad_os}/{ad_arch}/jdk/hotspot/normal/eclipse"
                )
                dest = tmp / dest_name
                print(f"    DL  {url}  → {dest_name}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {dest_name}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## Eclipse Temurin JDK {major} (LTS)\n\n"
                f"**Source:** https://adoptium.net/temurin/releases/?version={major}\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def sync_dotnet_microsoft(cfg: dict, mirror_repo: str, dry_run: bool, force: bool) -> int:
    """Handler for .NET SDK from Microsoft CDN."""
    tool          = cfg["tool"]
    mirror_prefix = cfg["tag_prefix"]

    # (os_label, arch_label, rid, ext)
    PLATFORMS = [
        ("windows", "x64",   "win-x64",     "zip"),
        ("linux",   "x64",   "linux-x64",   "tar.gz"),
        ("linux",   "arm64", "linux-arm64", "tar.gz"),
        ("macos",   "x64",   "osx-x64",     "tar.gz"),
        ("macos",   "arm64", "osx-arm64",   "tar.gz"),
    ]

    print("=" * 64)
    print(f"  Tool    : {tool}  (dotnet_microsoft mode)")
    print(f"  Mirror  : {mirror_repo}")
    print(f"  Dry run : {dry_run}  Force: {force}")
    print("=" * 64)

    with urllib.request.urlopen(
        "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/releases-index.json",
        timeout=30,
    ) as resp:
        index = json.loads(resp.read())

    lts_channels = [
        ch for ch in index.get("releases-index", [])
        if ch.get("release-type", "").upper() == "LTS"
        and ch.get("support-phase", "") not in ("eol", "end-of-life")
    ]
    print(f"  Found {len(lts_channels)} active LTS channels")

    synced = skipped = failed = 0

    for channel in lts_channels:
        version = channel.get("latest-sdk", "")
        if not version:
            continue
        mirror_tag = f"{mirror_prefix}{version}"
        print(f"\n--- {tool} {version}  (→ {mirror_tag}) ---")

        if not force and release_exists(mirror_repo, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        tmp = Path(tempfile.mkdtemp())
        try:
            uploaded: list[Path] = []
            for os_name, arch, rid, ext in PLATFORMS:
                filename = f"dotnet-sdk-{version}-{rid}.{ext}"
                url = (
                    f"https://dotnetcli.azureedge.net/dotnet/Sdk/{version}/{filename}"
                )
                dest = tmp / filename
                print(f"    DL  {url}")
                if dry_run:
                    dest.touch()
                elif not download_file(url, dest):
                    print(f"    [WARN] Download failed: {filename}")
                    continue
                uploaded.append(dest)

            if not uploaded:
                print(f"    [WARN] No assets — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## .NET SDK {version}\n\n"
                f"**Channel:** {channel.get('channel-version', '')} (LTS)\n"
                f"**Source:** https://dotnet.microsoft.com/download/dotnet\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )
            result = _create_mirror_release(tool, version, mirror_tag, mirror_repo, uploaded, notes, dry_run)
            if result in ("ok", "dry"):
                synced += 1
            else:
                failed += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: sync-tool.py <mirrors/*/sync.yml>", file=sys.stderr)
        return 1

    sync_file = sys.argv[1]
    if not os.path.isfile(sync_file):
        print(f"File not found: {sync_file}", file=sys.stderr)
        return 1

    DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
    FORCE   = os.environ.get("FORCE", "0") == "1"
    MIRROR_REPO = "vx-org/mirrors"

    cfg = load_yaml(sync_file)
    tool           = cfg["tool"]
    upstream       = cfg["upstream"]
    mirror_prefix  = cfg["tag_prefix"]
    keep_versions  = cfg.get("keep_versions", 0)

    # Dispatch to special handler if needed (before reading owner/repo)
    up_type = upstream.get("type", "github_release")
    if up_type == "skip":
        reason = upstream.get("reason", "no reason given")
        print(f"[SKIP] {tool}: {reason}")
        return 0
    if up_type == "btbn_ffmpeg":
        return sync_btbn_ffmpeg(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "multi_version":
        return sync_multi_version(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "nodejs_org":
        return sync_nodejs_org(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "go_dev":
        return sync_go_dev(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "rust_rustup":
        return sync_rust_rustup(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "hashicorp":
        return sync_hashicorp(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "adoptium":
        return sync_adoptium(cfg, MIRROR_REPO, DRY_RUN, FORCE)
    if up_type == "dotnet_microsoft":
        return sync_dotnet_microsoft(cfg, MIRROR_REPO, DRY_RUN, FORCE)

    # github_release (default): now safe to read owner/repo
    up_owner       = upstream["owner"]
    up_repo        = upstream["repo"]
    up_tag_prefix  = upstream.get("tag_prefix", "")
    inc_pre        = upstream.get("include_prerelease", False)
    tag_filter_re  = upstream.get("tag_filter", None)
    asset_defs     = upstream.get("assets", [])

    print("=" * 64)
    print(f"  Tool          : {tool}")
    print(f"  Upstream      : {up_owner}/{up_repo}")
    print(f"  Upstream pfx  : '{up_tag_prefix}'")
    print(f"  Mirror pfx    : '{mirror_prefix}'")
    print(f"  Pre-release   : {inc_pre}")
    print(f"  Keep versions : {keep_versions} (0=all)")
    print(f"  Mirror repo   : {MIRROR_REPO}")
    print(f"  Dry run       : {DRY_RUN}  Force: {FORCE}")
    print("=" * 64)

    # Fetch all releases (paginated)
    print("\nFetching releases ...")
    all_releases: list[dict] = []
    page = 1
    while True:
        data = gh_json([
            "api",
            f"repos/{up_owner}/{up_repo}/releases?per_page=100&page={page}"
        ])
        assert isinstance(data, list)
        if not data:
            break
        all_releases.extend(data)
        print(f"  Page {page}: {len(data)} releases")
        page += 1
        if len(data) < 100:
            break
    print(f"  Total: {len(all_releases)} releases")

    # Filter
    releases = [
        r for r in all_releases
        if (inc_pre or not r["prerelease"])
        and (not tag_filter_re or re.search(tag_filter_re, r["tag_name"]))
    ]
    print(f"  After filter: {len(releases)} releases")

    if keep_versions > 0:
        releases = releases[:keep_versions]

    synced = skipped = failed = 0

    for rel in releases:
        up_tag = rel["tag_name"]
        is_pre = rel["prerelease"]
        body   = rel.get("body") or ""

        version = up_tag
        if up_tag_prefix and version.startswith(up_tag_prefix):
            version = version[len(up_tag_prefix):]
        # BtbN uses n8.1.1 style
        if version.startswith("n") and version[1:2].isdigit():
            version = version[1:]

        mirror_tag = f"{mirror_prefix}{version}"

        print(f"\n--- {tool} {version}  ({up_tag} → {mirror_tag}) ---")

        if not FORCE and release_exists(MIRROR_REPO, mirror_tag):
            print("    [SKIP] Already mirrored.")
            skipped += 1
            continue

        # Match and download assets
        upstream_assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        tmp = Path(tempfile.mkdtemp())

        try:
            uploaded: list[Path] = []
            for asset_def in asset_defs:
                pat = asset_def.get("pattern", "")
                rename_tpl = asset_def.get("rename", "")

                for aname, aurl in upstream_assets.items():
                    if asset_matches(aname, pat, version):
                        dest_name = rename_tpl.replace("{version}", version) if rename_tpl else aname
                        dest = tmp / dest_name
                        print(f"    DL  {aname} → {dest_name}")
                        if DRY_RUN:
                            dest.touch()
                        elif not download_file(aurl, dest):
                            print(f"    [WARN] Download failed for {aname}, skipping.")
                            continue
                        uploaded.append(dest)
                        break  # one asset per pattern

            if not uploaded:
                print(f"    [WARN] No assets matched — skipping {mirror_tag}.")
                skipped += 1
                continue

            notes = (
                f"## {tool} {version}\n\n"
                f"**Mirrored from:** "
                f"https://github.com/{up_owner}/{up_repo}/releases/tag/{up_tag}\n\n"
                f"{body}\n\n"
                f"---\n*Permanent archive by "
                f"[vx-org/mirrors](https://github.com/vx-org/mirrors)*"
            )

            if DRY_RUN:
                print(f"    [DRY RUN] Would create {mirror_tag} with {len(uploaded)} assets.")
                synced += 1
                continue

            cmd = [
                "gh", "release", "create", mirror_tag,
                "--repo", MIRROR_REPO,
                "--title", f"{tool} {version}",
                "--notes", notes,
            ]
            if is_pre:
                cmd.append("--prerelease")
            cmd += [str(f) for f in uploaded]

            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                print(f"    [OK] {mirror_tag} created.")
                synced += 1
            else:
                print(f"    [FAIL] {r.stderr.strip()}")
                failed += 1

        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  {tool} done — synced={synced}  skipped={skipped}  failed={failed}")
    print("=" * 64)
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
