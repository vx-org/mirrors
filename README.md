# vx-mirrors

> Permanent binary archive for [vx](https://github.com/loonghao/vx) provider downloads.

## Why This Exists

Some upstream release sources are unreliable or temporary:

- Official download sites (ffmpeg.org) host only the **latest** build
- Third-party mirrors (johnvansickle.com, evermeet.cx) occasionally go offline
- GitHub releases from small projects can be deleted or renamed by maintainers
- Old versions disappear when projects do `keep_latest: 3`

This repository **permanently archives every version** of each tool, so `vx` can
always download the exact version it needs — even years later.

## Design

```
vx provider (provider.star)
    │
    │  download_url → github_asset_url("vx-org", "mirrors", "ffmpeg-8.1.1", asset)
    ▼
vx-org/mirrors  GitHub Releases
    │
    │  tag: ffmpeg-8.1.1
    │  assets:
    │    ffmpeg-8.1.1-win64-lgpl.zip
    │    ffmpeg-8.1.1-linux64-lgpl.tar.xz
    │    ffmpeg-8.1.1-linuxarm64-lgpl.tar.xz
    ▼
  Permanent archive ✓  (releases are never deleted)
```

### Mirror Tag Format

Each tool release uses a namespaced tag to avoid conflicts:

| Tool    | Mirror Tag       | Example         |
|---------|-----------------|-----------------|
| ffmpeg  | `ffmpeg-{ver}`  | `ffmpeg-8.1.1`  |
| witr    | `witr-{ver}`    | `witr-0.3.1`    |

### Sync Configuration

Each tool has a `mirrors/<tool>/sync.yml` that declares:

- **Upstream source** (GitHub owner/repo, tag prefix, pre-release policy)
- **Asset patterns** to download (with optional rename for stable naming)
- **keep_versions: 0** — archive all versions forever

## Mirrored Tools

| Tool | Upstream | Platforms | Notes |
|------|----------|-----------|-------|
| [ffmpeg](mirrors/ffmpeg/sync.yml) | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) | win64, linux64, linuxarm64 | Official static builds from ffmpeg.org |
| [witr](mirrors/witr/sync.yml) | [pranshuparmar/witr](https://github.com/pranshuparmar/witr) | all platforms | Process introspection tool |

## Adding a New Tool

1. Create `mirrors/<toolname>/sync.yml`:

```yaml
tool: mytool
upstream:
  type: github_release
  owner: upstream-owner
  repo:  upstream-repo
  tag_prefix: "v"          # strip "v" from tag to get version
  include_prerelease: false
  assets:
    - pattern: "mytool-{version}-linux-amd64.tar.gz"
      platforms: [linux/x64]
    - pattern: "mytool-{version}-windows-amd64.zip"
      platforms: [windows/x64]

tag_prefix: "mytool-"      # mirror tag prefix
keep_versions: 0           # 0 = archive all versions permanently
license: MIT
source_url: https://github.com/upstream-owner/upstream-repo
```

2. Open a PR — the sync workflow validates and runs on merge.

3. Update the corresponding `vx` provider's `download_url` to use `vx-org/mirrors`.

## Sync Schedule

- **Nightly** at 02:00 UTC — detects new upstream releases automatically
- **Manual trigger** — `Actions → Sync Mirrors → Run workflow`
  - Optional: specify a single tool, enable dry-run, or force re-sync

## Using in vx Providers

```python
# In your provider.star:
load("@vx//stdlib:github.star", "make_fetch_versions", "github_asset_url")

# Fetch available versions from our mirror tags
fetch_versions = make_fetch_versions("vx-org", "mirrors", tag_prefix = "ffmpeg-")

# Download from our mirror
def download_url(ctx, version):
    asset = "ffmpeg-{}-win64-lgpl.zip".format(version)
    return github_asset_url("vx-org", "mirrors", "ffmpeg-" + version, asset)
```

## License

Binary files in releases remain under their original upstream licenses.
No files are modified — this is a pure mirror for reliability.
See each tool's `sync.yml` for the original license and source URL.
