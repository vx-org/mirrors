# vx-mirrors

> Stable binary mirrors for [vx](https://github.com/loonghao/vx) providers.

vx is a universal dev tool manager. Some upstream release sources (e.g., ffmpeg) are
occasionally unreliable. This repository provides stable GitHub Releases mirrors so
`vx` can always download tools without hitting external mirror outages.

## How It Works

1. A GitHub Actions workflow runs nightly and on-demand to sync the latest releases
   from upstream sources into this repository's **GitHub Releases**.
2. `vx` providers point their `download_url` at `https://github.com/vx-org/mirrors/releases/download/<tool>-<version>/<asset>`.
3. If the mirror is ahead of upstream, users get the binary immediately — no external dependencies.

## Mirrored Tools

| Tool | Upstream | Mirror Tag Format | Notes |
|------|----------|-------------------|-------|
| ffmpeg | [GyanD/ffmpeg](https://github.com/GyanD/ffmpeg) | `ffmpeg-<version>` | Windows essentials build |
| witr | [pranshuparmar/witr](https://github.com/pranshuparmar/witr) | `witr-<version>` | All platforms |

## Mirror Tag Format

Each tool release is stored as a separate GitHub Release tag:

```
ffmpeg-7.1.1     → assets: ffmpeg-7.1.1-essentials_build.zip
witr-0.3.1       → assets: witr-linux-amd64, witr-darwin-amd64, witr-windows-amd64.zip
```

## Adding a New Tool

1. Add an entry in `mirrors/` with a `sync.yml` config.
2. Open a PR — the sync workflow will validate it.

## License

Binary files in releases remain under their original licenses. See each tool's
upstream repository for license details. This mirror exists purely for reliability;
no files are modified.
