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


def release_exists(repo: str, tag: str) -> bool:
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        capture_output=True, text=True
    )
    return r.returncode == 0


def download_file(url: str, dest: Path) -> bool:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
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
    up_owner       = upstream["owner"]
    up_repo        = upstream["repo"]
    mirror_prefix  = cfg["tag_prefix"]
    keep_versions  = cfg.get("keep_versions", 0)

    # Dispatch to special handler if needed
    up_type = upstream.get("type", "github_release")
    if up_type == "btbn_ffmpeg":
        return sync_btbn_ffmpeg(cfg, MIRROR_REPO, DRY_RUN, FORCE)

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
