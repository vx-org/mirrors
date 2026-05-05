#!/usr/bin/env bash
# sync-tool.sh — Sync a single tool's latest N releases to vx-org/mirrors
#
# Usage:
#   ./scripts/sync-tool.sh mirrors/ffmpeg/sync.yml
#   GITHUB_TOKEN=xxx ./scripts/sync-tool.sh mirrors/witr/sync.yml
#
# Requirements: gh, curl, jq

set -euo pipefail

SYNC_FILE="${1:-}"
if [[ -z "$SYNC_FILE" ]]; then
  echo "Usage: $0 <path/to/sync.yml>" >&2
  exit 1
fi

# Parse sync.yml with basic field extraction (no yq dependency)
get_field() {
  grep "^${1}:" "$SYNC_FILE" | head -1 | sed 's/^[^:]*: *//' | tr -d '"'
}

TOOL=$(get_field "tool")
UPSTREAM_OWNER=$(grep "owner:" "$SYNC_FILE" | head -1 | sed 's/.*: *//' | tr -d '"')
UPSTREAM_REPO=$(grep "repo:" "$SYNC_FILE" | head -1 | sed 's/.*: *//' | tr -d '"')
TAG_PREFIX=$(get_field "tag_prefix")
KEEP_VERSIONS=$(get_field "keep_versions")
KEEP_VERSIONS="${KEEP_VERSIONS:-5}"

MIRROR_REPO="vx-org/mirrors"

echo "=== Syncing $TOOL from $UPSTREAM_OWNER/$UPSTREAM_REPO ==="
echo "    Mirror: $MIRROR_REPO  tag_prefix=$TAG_PREFIX  keep=$KEEP_VERSIONS"

# Fetch latest N upstream releases
RELEASES=$(gh api "repos/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases?per_page=${KEEP_VERSIONS}" \
  --jq '.[] | {tag: .tag_name, assets: [.assets[] | {name: .name, url: .browser_download_url}]}')

while IFS= read -r RELEASE_JSON; do
  UPSTREAM_TAG=$(echo "$RELEASE_JSON" | jq -r '.tag')
  VERSION="${UPSTREAM_TAG#v}"
  MIRROR_TAG="${TAG_PREFIX}${VERSION}"

  echo ""
  echo "--- $TOOL $VERSION (mirror tag: $MIRROR_TAG) ---"

  # Check if mirror tag already exists
  if gh release view "$MIRROR_TAG" --repo "$MIRROR_REPO" &>/dev/null; then
    echo "    Already mirrored, skipping."
    continue
  fi

  # Download all assets for this release
  TMP_DIR=$(mktemp -d)
  trap "rm -rf $TMP_DIR" EXIT

  ASSET_NAMES=()
  while IFS= read -r ASSET_JSON; do
    ASSET_NAME=$(echo "$ASSET_JSON" | jq -r '.name')
    ASSET_URL=$(echo "$ASSET_JSON" | jq -r '.url')
    DEST="$TMP_DIR/$ASSET_NAME"

    echo "    Downloading $ASSET_NAME ..."
    curl -fsSL "$ASSET_URL" -o "$DEST"
    ASSET_NAMES+=("$DEST")
  done < <(echo "$RELEASE_JSON" | jq -c '.assets[]')

  if [[ ${#ASSET_NAMES[@]} -eq 0 ]]; then
    echo "    No assets found, skipping."
    continue
  fi

  # Create mirror release and upload assets
  echo "    Creating mirror release $MIRROR_TAG ..."
  gh release create "$MIRROR_TAG" \
    --repo "$MIRROR_REPO" \
    --title "$TOOL $VERSION (mirror)" \
    --notes "Mirrored from https://github.com/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases/tag/${UPSTREAM_TAG}" \
    "${ASSET_NAMES[@]}"

  echo "    ✓ Mirrored $TOOL $VERSION"

done < <(echo "$RELEASES" | jq -c '.')

echo ""
echo "=== Done syncing $TOOL ==="
