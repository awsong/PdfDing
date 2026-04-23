#!/usr/bin/env bash
# PdfDing hot-patch applier (reusable).
#
# Applies a set of modified source files into a running PdfDing container
# without rebuilding the image. Designed for the runtime layout produced by
# the upstream Dockerfile: WhiteNoise with CompressedManifestStaticFilesStorage
# under /home/nonroot/pdfding.
#
# Usage:
#   ./hotpatch_apply.sh <container> <files_dir> [version_tag]
#
#   <container>   docker container name or id running pdfding
#   <files_dir>   directory containing the patched files, laid out under
#                 files/pdfding/... mirroring the app tree. Recognized paths:
#                   pdfding/static/js/<name>.js        -> also patches staticfiles/js/<name>.<hash>.js
#                   pdfding/static/js/pdfding/<name>.js-> also patches staticfiles/js/pdfding/<name>.<hash>.js
#                   pdfding/static/css/<name>.css      -> also patches staticfiles/css/<name>.<hash>.css
#                   pdfding/<app>/templates/<file>     -> copied verbatim, no hash dance
#                   pdfding/core/settings/version.py   -> optional, overrides version_tag below
#                   any other path                     -> copied verbatim to the same relative path
#   [version_tag] optional. If set (and no explicit version.py in files/), the
#                 script writes VERSION = '<version_tag>' into core/settings/version.py
#
# What it does:
#   1. Copies every file under <files_dir>/ into the container mirroring the
#      path relative to files/.
#   2. For every static file under pdfding/static/{js,css}/..., finds the
#      matching hashed copy in staticfiles/ and overwrites it in place
#      (keeping the existing hash so staticfiles.json stays valid), then
#      removes the stale .gz/.br siblings so WhiteNoise reserves fresh bytes.
#   3. Optionally writes core/settings/version.py.
#   4. Restarts the container.
#
# Rollback: snapshot the container before applying:
#   docker commit <container> pdfding:pre-patch-backup

set -euo pipefail

APP_ROOT="/home/nonroot/pdfding"

die() { echo "[hotpatch] ERROR: $*" >&2; exit 1; }
log() { echo "[hotpatch] $*"; }

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  cat >&2 <<EOF
Usage: $0 <container> <files_dir> [version_tag]

  <files_dir> must contain a tree rooted at pdfding/, e.g.
    <files_dir>/pdfding/static/js/pdfding/viewer_logged_in.js
    <files_dir>/pdfding/pdf/templates/viewer.html
    <files_dir>/pdfding/core/settings/version.py  (optional)
EOF
  exit 1
fi

CONTAINER="$1"
FILES_DIR="$(cd "$2" && pwd)"
VERSION_TAG="${3:-}"

command -v docker >/dev/null 2>&1 || die "docker CLI not found on host"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "container '$CONTAINER' not found"
[ -d "$FILES_DIR/pdfding" ] || die "files_dir missing pdfding/ subtree: $FILES_DIR"

log "container   : $CONTAINER"
log "files_dir   : $FILES_DIR"
log "app_root    : $APP_ROOT"
[ -n "$VERSION_TAG" ] && log "version_tag : $VERSION_TAG"

# Collect the list of regular files we want to copy.
mapfile -t REL_FILES < <(cd "$FILES_DIR" && find pdfding -type f | sort)
[ "${#REL_FILES[@]}" -gt 0 ] || die "no files under $FILES_DIR/pdfding/"

# --- 1. copy every file verbatim into the container's app tree ---------------
log "copying ${#REL_FILES[@]} file(s) into $APP_ROOT/ ..."
for rel in "${REL_FILES[@]}"; do
  dest_rel="${rel#pdfding/}"                # e.g. static/js/pdfding/foo.js
  dest_abs="$APP_ROOT/$dest_rel"
  dest_dir="$(dirname "$dest_abs")"
  docker exec "$CONTAINER" mkdir -p "$dest_dir"
  docker cp "$FILES_DIR/$rel" "$CONTAINER:$dest_abs"
done

# --- 2. for every patched static file, also patch its hashed staticfiles copy
log "patching hashed staticfiles copies ..."
patched_static=0
for rel in "${REL_FILES[@]}"; do
  case "$rel" in
    pdfding/static/*) ;;
    *) continue ;;
  esac

  static_rel="${rel#pdfding/static/}"       # e.g. js/pdfding/foo.js
  base="$(basename "$static_rel")"          # foo.js
  dir_rel="$(dirname "$static_rel")"        # js/pdfding
  name="${base%.*}"                         # foo
  ext="${base##*.}"                         # js
  hashed_glob="$APP_ROOT/staticfiles/$dir_rel/$name.*.$ext"

  # Find the existing hashed file (skip .gz/.br).
  hashed_path=$(docker exec "$CONTAINER" sh -c \
    "ls $hashed_glob 2>/dev/null | grep -v -E '\\.(gz|br)$' | head -1" || true)

  if [ -z "$hashed_path" ]; then
    log "  skip (no hashed copy found for $static_rel)"
    continue
  fi

  log "  $static_rel -> $hashed_path"
  docker cp "$FILES_DIR/$rel" "$CONTAINER:$hashed_path"
  docker exec "$CONTAINER" sh -c "rm -f '$hashed_path.gz' '$hashed_path.br'"
  patched_static=$((patched_static + 1))
done
log "patched $patched_static static file(s) in place"

# --- 3. optional version stamp ----------------------------------------------
if [ -n "$VERSION_TAG" ] && [ ! -f "$FILES_DIR/pdfding/core/settings/version.py" ]; then
  log "writing version.py = $VERSION_TAG"
  docker exec "$CONTAINER" sh -c \
    "echo \"VERSION = '$VERSION_TAG'\" > $APP_ROOT/core/settings/version.py"
fi

# --- 4. restart --------------------------------------------------------------
log "restarting container ..."
docker restart "$CONTAINER" >/dev/null

log "done. Hard-refresh the browser (Cmd+Shift+R) to bypass browser cache."
