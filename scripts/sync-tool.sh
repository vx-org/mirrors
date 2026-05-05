#!/usr/bin/env bash
# sync-tool.sh — Mirror a tool's GitHub releases into vx-org/mirrors
#
# Design goals:
#   - Permanently archive ALL versions (keep_versions=0 means keep forever)
#   - Never overwrite or delete existing mirror releases
#   - Support pre-release and regular releases
#   - Rename assets to a stable, version-embedded naming convention
#   - Handle GitHub API pagination to find all releases
#
# Usage:
#   ./scripts/sync-tool.sh mirrors/ffmpeg/sync.yml
#   ./scripts/sync-tool.sh mirrors/witr/sync.yml
#
# Env:
#   GH_TOKEN     — GitHub token (required, write access to vx-org/mirrors)
#   DRY_RUN=1    — Print what would be done, but don't create releases
#   FORCE=1      — Re-mirror even if release already exists (overwrites)

set -euo pipefail

SYNC_FILE="${1:-}"
if [[ -z "$SYNC_FILE" || ! -f "$SYNC_FILE" ]]; then
  echo "Usage: $0 <path/to/sync.yml>" >&2
  exit 1
fi

DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"
MIRROR_REPO="vx-org/mirrors"

# ---------------------------------------------------------------------------
# Minimal YAML field parser (no yq dependency)
# ---------------------------------------------------------------------------
yaml_get() {
  local key="$1"
  grep "^${key}:" "$SYNC_FILE" | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"' | tr -d "'"
}
yaml_get_nested() {
  local parent="$1" key="$2"
  awk "/^${parent}:/,/^[^ ]/" "$SYNC_FILE" | grep "^  ${key}:" | head -1 \
    | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"' | tr -d "'"
}

TOOL=$(yaml_get "tool")
UPSTREAM_OWNER=$(yaml_get_nested "upstream" "owner")
UPSTREAM_REPO=$(yaml_get_nested "upstream" "repo")
UPSTREAM_TAG_PREFIX=$(yaml_get_nested "upstream" "tag_prefix")
INCLUDE_PRERELEASE=$(yaml_get_nested "upstream" "include_prerelease")
TAG_FILTER=$(yaml_get_nested "upstream" "tag_filter")
MIRROR_TAG_PREFIX=$(yaml_get "tag_prefix")
KEEP_VERSIONS=$(yaml_get "keep_versions")
KEEP_VERSIONS="${KEEP_VERSIONS:-0}"   # 0 = keep all (permanent archive)
INCLUDE_PRERELEASE="${INCLUDE_PRERELEASE:-false}"

echo "================================================================"
echo "  Tool         : $TOOL"
echo "  Upstream     : $UPSTREAM_OWNER/$UPSTREAM_REPO"
echo "  Tag prefix   : upstream='${UPSTREAM_TAG_PREFIX}' mirror='${MIRROR_TAG_PREFIX}'"
echo "  Prerelease   : $INCLUDE_PRERELEASE"
echo "  Keep versions: ${KEEP_VERSIONS} (0=all)"
echo "  Mirror repo  : $MIRROR_REPO"
echo "  Dry run      : $DRY_RUN"
echo "================================================================"

# ---------------------------------------------------------------------------
# Fetch all releases (paginated)
# ---------------------------------------------------------------------------
echo ""
echo "Fetching releases from $UPSTREAM_OWNER/$UPSTREAM_REPO ..."

ALL_RELEASES="[]"
PAGE=1
while true; do
  PAGE_DATA=$(gh api \
    "repos/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases?per_page=100&page=${PAGE}" \
    2>/dev/null || echo "[]")

  COUNT=$(echo "$PAGE_DATA" | jq 'length')
  if [[ "$COUNT" -eq 0 ]]; then break; fi

  ALL_RELEASES=$(echo "$ALL_RELEASES $PAGE_DATA" | jq -s 'add')
  echo "  Fetched page $PAGE ($COUNT releases)"
  PAGE=$((PAGE + 1))
  [[ "$COUNT" -lt 100 ]] && break
done

TOTAL=$(echo "$ALL_RELEASES" | jq 'length')
echo "  Total upstream releases: $TOTAL"

# ---------------------------------------------------------------------------
# Filter releases
# ---------------------------------------------------------------------------
FILTER='.prerelease == false'
if [[ "$INCLUDE_PRERELEASE" == "true" ]]; then
  FILTER='true'
fi

if [[ -n "$TAG_FILTER" ]]; then
  FILTER="$FILTER and (.tag_name | test(\"${TAG_FILTER}\"))"
fi

FILTERED=$(echo "$ALL_RELEASES" | jq "[.[] | select($FILTER)]")
FILTERED_COUNT=$(echo "$FILTERED" | jq 'length')
echo "  After filter: $FILTERED_COUNT releases"

# Apply keep_versions limit (0 = keep all)
if [[ "$KEEP_VERSIONS" -gt 0 ]]; then
  FILTERED=$(echo "$FILTERED" | jq ".[0:${KEEP_VERSIONS}]")
  echo "  Limited to: $(echo "$FILTERED" | jq 'length') releases"
fi

# ---------------------------------------------------------------------------
# Parse asset patterns from sync.yml
# ---------------------------------------------------------------------------
# Extract asset patterns (lines under "assets:" block with "- pattern:")
ASSET_PATTERNS=()
while IFS= read -r line; do
  pattern=$(echo "$line" | sed 's/.*pattern:[[:space:]]*//' | tr -d '"' | tr -d "'")
  ASSET_PATTERNS+=("$pattern")
done < <(grep "    - pattern:" "$SYNC_FILE")

# Extract rename patterns (optional)
ASSET_RENAMES=()
while IFS= read -r line; do
  rename=$(echo "$line" | sed 's/.*rename:[[:space:]]*//' | tr -d '"' | tr -d "'")
  ASSET_RENAMES+=("$rename")
done < <(grep "      rename:" "$SYNC_FILE")

echo "  Asset patterns: ${#ASSET_PATTERNS[@]}"

# ---------------------------------------------------------------------------
# Sync each release
# ---------------------------------------------------------------------------
SYNCED=0
SKIPPED=0
FAILED=0

while IFS= read -r RELEASE; do
  UPSTREAM_TAG=$(echo "$RELEASE" | jq -r '.tag_name')
  IS_PRERELEASE=$(echo "$RELEASE" | jq -r '.prerelease')
  RELEASE_BODY=$(echo "$RELEASE" | jq -r '.body // ""')

  # Derive version: strip upstream tag prefix
  VERSION="$UPSTREAM_TAG"
  if [[ -n "$UPSTREAM_TAG_PREFIX" ]]; then
    VERSION="${UPSTREAM_TAG#${UPSTREAM_TAG_PREFIX}}"
  fi

  # Also strip leading 'n' for BtbN-style tags like "n8.1.1"
  VERSION="${VERSION#n}"

  MIRROR_TAG="${MIRROR_TAG_PREFIX}${VERSION}"

  echo ""
  echo "--- $TOOL $VERSION  (upstream: $UPSTREAM_TAG → mirror: $MIRROR_TAG) ---"

  # Check if already mirrored
  if [[ "$FORCE" != "1" ]]; then
    if gh release view "$MIRROR_TAG" --repo "$MIRROR_REPO" &>/dev/null; then
      echo "    [SKIP] Already mirrored."
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
  fi

  # Download matching assets
  TMP_DIR=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$TMP_DIR'" RETURN

  DOWNLOADED_FILES=()
  UPSTREAM_ASSETS=$(echo "$RELEASE" | jq -r '.assets[] | "\(.name)\t\(.browser_download_url)"')

  for i in "${!ASSET_PATTERNS[@]}"; do
    PATTERN="${ASSET_PATTERNS[$i]}"
    # Convert {semver} / {version} placeholders to regex
    REGEX="${PATTERN//\{semver\}/[0-9]+\\.[0-9]+(\\.[0-9]+)?}"
    REGEX="${REGEX//\{version\}/$VERSION}"

    while IFS=$'\t' read -r ASSET_NAME ASSET_URL; do
      if echo "$ASSET_NAME" | grep -qE "^${REGEX}$"; then
        # Determine output filename
        DEST_NAME="$ASSET_NAME"
        if [[ -n "${ASSET_RENAMES[$i]+x}" && -n "${ASSET_RENAMES[$i]}" ]]; then
          DEST_NAME="${ASSET_RENAMES[$i]//\{version\}/$VERSION}"
        fi
        DEST="$TMP_DIR/$DEST_NAME"

        echo "    Downloading $ASSET_NAME → $DEST_NAME ..."
        if [[ "$DRY_RUN" == "1" ]]; then
          echo "    [DRY RUN] Would download $ASSET_URL"
          touch "$DEST"
        else
          if ! curl -fsSL --retry 3 --retry-delay 5 "$ASSET_URL" -o "$DEST"; then
            echo "    [WARN] Failed to download $ASSET_NAME, skipping asset."
            continue
          fi
        fi
        DOWNLOADED_FILES+=("$DEST")
        break  # each pattern matches at most one asset per release
      fi
    done <<< "$UPSTREAM_ASSETS"
  done

  if [[ ${#DOWNLOADED_FILES[@]} -eq 0 ]]; then
    echo "    [WARN] No assets matched for $MIRROR_TAG, skipping release."
    SKIPPED=$((SKIPPED + 1))
    rm -rf "$TMP_DIR"
    continue
  fi

  echo "    Downloaded ${#DOWNLOADED_FILES[@]} asset(s)."

  # Build release notes
  NOTES="## $TOOL $VERSION

**Mirrored from:** https://github.com/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases/tag/${UPSTREAM_TAG}

### Original release notes
${RELEASE_BODY}

---
*This release is maintained by [vx-org/mirrors](https://github.com/vx-org/mirrors) for reliable vx provider downloads.*"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "    [DRY RUN] Would create release $MIRROR_TAG with ${#DOWNLOADED_FILES[@]} assets"
  else
    PRERELEASE_FLAG=""
    [[ "$IS_PRERELEASE" == "true" ]] && PRERELEASE_FLAG="--prerelease"

    if gh release create "$MIRROR_TAG" \
      --repo "$MIRROR_REPO" \
      --title "$TOOL $VERSION" \
      --notes "$NOTES" \
      $PRERELEASE_FLAG \
      "${DOWNLOADED_FILES[@]}"; then
      echo "    [OK] Created mirror release $MIRROR_TAG"
      SYNCED=$((SYNCED + 1))
    else
      echo "    [FAIL] Failed to create release $MIRROR_TAG"
      FAILED=$((FAILED + 1))
    fi
  fi

  rm -rf "$TMP_DIR"

done < <(echo "$FILTERED" | jq -c '.[]')

echo ""
echo "================================================================"
echo "  Done syncing $TOOL"
echo "  Synced: $SYNCED  |  Skipped: $SKIPPED  |  Failed: $FAILED"
echo "================================================================"

[[ "$FAILED" -gt 0 ]] && exit 1
exit 0
