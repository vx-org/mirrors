#!/usr/bin/env bash
# sync-tool.sh — Mirror a tool's GitHub releases into vx-org/mirrors
#
# Permanently archives ALL versions (keep_versions: 0 = keep forever).
# Never overwrites or deletes existing mirror releases.
#
# Usage:
#   ./scripts/sync-tool.sh mirrors/ffmpeg/sync.yml
#
# Env:
#   GH_TOKEN   GitHub token with write access to vx-org/mirrors
#   DRY_RUN=1  Print what would happen, create nothing
#   FORCE=1    Re-mirror even if release already exists

set -euo pipefail

SYNC_FILE="${1:-}"
[[ -z "$SYNC_FILE" || ! -f "$SYNC_FILE" ]] && { echo "Usage: $0 <mirrors/*/sync.yml>" >&2; exit 1; }

DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"
MIRROR_REPO="vx-org/mirrors"

# Simple grep-based field extractor — handles any indentation level
_get() { grep -m1 "^[[:space:]]*${1}:" "$SYNC_FILE" | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"'"'" ; }

TOOL=$(_get "tool")
UPSTREAM_OWNER=$(_get "owner")
UPSTREAM_REPO=$(_get "repo")
UPSTREAM_TAG_PREFIX=$(_get "tag_prefix" | head -1)   # may match mirror tag_prefix too; take first
INCLUDE_PRERELEASE=$(_get "include_prerelease")
TAG_FILTER=$(_get "tag_filter")
KEEP_VERSIONS=$(_get "keep_versions"); KEEP_VERSIONS="${KEEP_VERSIONS:-0}"
INCLUDE_PRERELEASE="${INCLUDE_PRERELEASE:-false}"

# Mirror tag_prefix is the LAST occurrence of tag_prefix (top-level, not nested)
MIRROR_TAG_PREFIX=$(grep "^tag_prefix:" "$SYNC_FILE" | tail -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"'"'")

echo "================================================================"
echo "  Tool          : $TOOL"
echo "  Upstream      : $UPSTREAM_OWNER/$UPSTREAM_REPO"
echo "  Upstream pfx  : '${UPSTREAM_TAG_PREFIX}'"
echo "  Mirror pfx    : '${MIRROR_TAG_PREFIX}'"
echo "  Pre-release   : $INCLUDE_PRERELEASE"
echo "  Keep versions : ${KEEP_VERSIONS} (0=all)"
echo "  Mirror repo   : $MIRROR_REPO"
echo "  Dry run       : $DRY_RUN  Force: $FORCE"
echo "================================================================"

# ---------------------------------------------------------------------------
# Fetch all releases (paginated, up to 1000)
# ---------------------------------------------------------------------------
echo ""
echo "Fetching releases from $UPSTREAM_OWNER/$UPSTREAM_REPO ..."
ALL_RELEASES="[]"
PAGE=1
while true; do
  DATA=$(gh api "repos/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases?per_page=100&page=${PAGE}" 2>/dev/null || echo "[]")
  COUNT=$(echo "$DATA" | jq 'length')
  [[ "$COUNT" -eq 0 ]] && break
  ALL_RELEASES=$(echo "$ALL_RELEASES $DATA" | jq -s 'add')
  echo "  Page $PAGE: $COUNT releases"
  PAGE=$((PAGE + 1))
  [[ "$COUNT" -lt 100 ]] && break
done
echo "  Total: $(echo "$ALL_RELEASES" | jq 'length') releases"

# ---------------------------------------------------------------------------
# Filter by pre-release and tag pattern
# ---------------------------------------------------------------------------
JQ_FILTER='.prerelease == false'
[[ "$INCLUDE_PRERELEASE" == "true" ]] && JQ_FILTER='true'
[[ -n "$TAG_FILTER" ]] && JQ_FILTER="$JQ_FILTER and (.tag_name | test(\"${TAG_FILTER}\"))"

FILTERED=$(echo "$ALL_RELEASES" | jq "[.[] | select($JQ_FILTER)]")
echo "  After filter: $(echo "$FILTERED" | jq 'length') releases"

[[ "$KEEP_VERSIONS" -gt 0 ]] && FILTERED=$(echo "$FILTERED" | jq ".[0:${KEEP_VERSIONS}]")

# ---------------------------------------------------------------------------
# Parse asset patterns from sync.yml  (lines: "    - pattern: ...")
# ---------------------------------------------------------------------------
mapfile -t ASSET_PATTERNS < <(grep "- pattern:" "$SYNC_FILE" | sed 's/.*pattern:[[:space:]]*//' | tr -d '"'"'")
mapfile -t ASSET_RENAMES  < <(grep "      rename:" "$SYNC_FILE" | sed 's/.*rename:[[:space:]]*//' | tr -d '"'"'")
echo "  Asset patterns: ${#ASSET_PATTERNS[@]}"

# ---------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------
SYNCED=0; SKIPPED=0; FAILED=0

while IFS= read -r REL; do
  UPSTREAM_TAG=$(echo "$REL" | jq -r '.tag_name')
  IS_PRE=$(echo "$REL" | jq -r '.prerelease')
  BODY=$(echo "$REL" | jq -r '.body // ""')

  # Derive version: strip upstream tag prefix (v / n / custom)
  VERSION="$UPSTREAM_TAG"
  [[ -n "$UPSTREAM_TAG_PREFIX" ]] && VERSION="${VERSION#${UPSTREAM_TAG_PREFIX}}"
  VERSION="${VERSION#n}"   # BtbN uses n8.1.1 style

  MIRROR_TAG="${MIRROR_TAG_PREFIX}${VERSION}"

  echo ""
  echo "--- $TOOL $VERSION  ($UPSTREAM_TAG → $MIRROR_TAG) ---"

  if [[ "$FORCE" != "1" ]] && gh release view "$MIRROR_TAG" --repo "$MIRROR_REPO" &>/dev/null; then
    echo "    [SKIP] Already mirrored."
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # ---- Download matching assets ------------------------------------------
  TMP_DIR=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$TMP_DIR'" RETURN

  UPLOADED=()
  UPSTREAM_ASSETS=$(echo "$REL" | jq -r '.assets[] | "\(.name)\t\(.browser_download_url)"')

  for i in "${!ASSET_PATTERNS[@]}"; do
    PAT="${ASSET_PATTERNS[$i]}"
    # Turn {semver}/{version} placeholders into a permissive regex
    RE="${PAT//\{semver\}/[0-9]+\\.[0-9]+(\\.[0-9]+)?}"
    RE="${RE//\{version\}/$VERSION}"

    while IFS=$'\t' read -r ANAME AURL; do
      if echo "$ANAME" | grep -qE "^${RE}$"; then
        DEST_NAME="$ANAME"
        if [[ -n "${ASSET_RENAMES[$i]+x}" && -n "${ASSET_RENAMES[$i]}" ]]; then
          DEST_NAME="${ASSET_RENAMES[$i]//\{version\}/$VERSION}"
        fi
        DEST="$TMP_DIR/$DEST_NAME"
        echo "    DL  $ANAME → $DEST_NAME"
        if [[ "$DRY_RUN" == "1" ]]; then
          touch "$DEST"
        else
          curl -fsSL --retry 3 --retry-delay 5 "$AURL" -o "$DEST" || { echo "    [WARN] download failed, skipping."; continue; }
        fi
        UPLOADED+=("$DEST")
        break
      fi
    done <<< "$UPSTREAM_ASSETS"
  done

  if [[ ${#UPLOADED[@]} -eq 0 ]]; then
    echo "    [WARN] No assets matched — skipping $MIRROR_TAG."
    SKIPPED=$((SKIPPED + 1))
    rm -rf "$TMP_DIR"; continue
  fi

  # ---- Create mirror release ---------------------------------------------
  NOTES="## $TOOL $VERSION

**Mirrored from:** https://github.com/${UPSTREAM_OWNER}/${UPSTREAM_REPO}/releases/tag/${UPSTREAM_TAG}

${BODY}

---
*Permanent archive by [vx-org/mirrors](https://github.com/vx-org/mirrors)*"

  PRE_FLAG=""; [[ "$IS_PRE" == "true" ]] && PRE_FLAG="--prerelease"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "    [DRY RUN] Would create $MIRROR_TAG with ${#UPLOADED[@]} assets."
    SYNCED=$((SYNCED + 1))
  elif gh release create "$MIRROR_TAG" \
      --repo "$MIRROR_REPO" \
      --title "$TOOL $VERSION" \
      --notes "$NOTES" \
      $PRE_FLAG \
      "${UPLOADED[@]}"; then
    echo "    [OK] $MIRROR_TAG created."
    SYNCED=$((SYNCED + 1))
  else
    echo "    [FAIL] Could not create $MIRROR_TAG."
    FAILED=$((FAILED + 1))
  fi

  rm -rf "$TMP_DIR"

done < <(echo "$FILTERED" | jq -c '.[]')

echo ""
echo "================================================================"
echo "  $TOOL done — synced=$SYNCED  skipped=$SKIPPED  failed=$FAILED"
echo "================================================================"
[[ "$FAILED" -gt 0 ]] && exit 1; exit 0
