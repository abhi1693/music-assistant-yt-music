#!/bin/sh
# Install the ytmusic provider into the running Music Assistant container.
#
# Portable across HAOS (BusyBox ash) and Supervised installs. Uses curl + tar
# instead of git so it runs on HAOS, where git is not available.
#
# Usage:
#   sh install_provider.sh [--force] [--repo-owner OWNER] [--ref REF] [--ma-id ID]
#                          [--python-version VER] [--config-dir DIR]
#                          [--no-restart] [--no-stage]
#
# What it does:
#   1. Downloads ytmusic/ from the repository at the requested ref.
#   2. Stages it at /config/custom_components/mass/providers/ytmusic
#      as a persistent recovery copy.
#   3. Copies it into the live MA container at
#      /app/venv/lib/<pythonX.Y>/site-packages/music_assistant/providers/.
#   4. Restarts the MA container so it picks up the new files.
#
# For automatic re-install across HA restarts, see WATCHER_ADDON.md or run
# install_watcher_addon.sh.

set -eu

REPO_OWNER="abhi1693"
REPO_NAME="music-assistant-yt-music"
PROVIDER_DIR="ytmusic"
RETIRED_PROVIDER_DIR="${PROVIDER_DIR}_free"

REF="master"
FORCE=0
MA_ID=""
PYTHON_VERSION=""
CONFIG_DIR=""
NO_RESTART=0
NO_STAGE=0

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# 'docker' missing is the single most common install failure on HAOS, because
# the official Terminal & SSH add-on is sandboxed and cannot reach the host
# Docker daemon. Give an actionable message instead of a bare "not found".
need_docker() {
    command -v docker >/dev/null 2>&1 && return 0
    die "the 'docker' command was not found.

This installer must run from a shell that can reach the host Docker daemon,
because it copies the provider into the Music Assistant container. The official
'Terminal & SSH' add-on is sandboxed and does not provide Docker, which is the
usual cause of this error on Home Assistant OS (for example HA Green or Yellow).

Two ways to proceed:
  A) Install the 'Advanced SSH & Web Terminal' community add-on, set its
     'Protection mode' to OFF, restart it, then re-run this one-liner there.
  B) Skip Docker in your shell: run install_watcher_addon.sh instead, then
     install and start the 'MA Provider Watcher' local add-on (Protection mode
     OFF). It injects the provider for you and also survives HA restarts.

See the README 'Installation' section for the full walkthrough."
}

usage() {
    cat <<EOF
Usage: sh install_provider.sh [options]

Options:
  --force, -f               Skip overwrite prompts
  --repo-owner OWNER        Repository owner (default: abhi1693)
  --ref REF                 Git ref (branch/tag/commit) to download (default: master)
  --ma-id ID                Music Assistant container ID (default: auto-detect)
  --python-version VER      MA Python version, e.g. python3.13 (default: auto-detect)
  --config-dir DIR          /config directory on the host
                            (default: auto-detect HAOS vs. Supervised)
  --no-restart              Skip the docker restart at the end
  --no-stage                Skip copying to /config/custom_components/mass/providers
  --help, -h                Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f) FORCE=1 ;;
        --repo-owner) shift; REPO_OWNER="${1:-}" ;;
        --ref) shift; REF="${1:-}" ;;
        --ma-id) shift; MA_ID="${1:-}" ;;
        --python-version) shift; PYTHON_VERSION="${1:-}" ;;
        --config-dir) shift; CONFIG_DIR="${1:-}" ;;
        --no-restart) NO_RESTART=1 ;;
        --no-stage) NO_STAGE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift || true
done

# URL of this script, used in re-run hints so they are copy-pasteable. Honors
# --repo-owner / --ref, and (crucially) shows the "sh -s --" pipe form: the
# documented install is "curl ... | sh", where a bare "--flag" is parsed by sh
# itself and fails with "sh: bad option".
SCRIPT_URL="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$REF/scripts/install_provider.sh"

# --- Preflight ---------------------------------------------------------------

log "Preflight checks..."
need curl
need tar
need mkdir
need cp
need rm
need_docker

# --- Detect MA container -----------------------------------------------------

if [ -z "$MA_ID" ]; then
    MA_ID="$(docker ps --format '{{.Names}}' 2>/dev/null \
             | grep -E '^addon_[0-9a-f]+_music_assistant(_beta|_nightly|_dev)?$' \
             | head -n1 || true)"
    if [ -z "$MA_ID" ]; then
        MA_ID="addon_d5369777_music_assistant"
        log "WARN: could not auto-detect MA container; using fallback '$MA_ID'."
        log "      Verify with: docker ps | grep music"
        log "      then re-run with the right id, e.g.:"
        log "        curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <ID>"
    else
        log "Detected MA container: $MA_ID"
    fi
fi

# Confirm the MA container actually exists before we go further.
docker inspect "$MA_ID" >/dev/null 2>&1 \
    || die "MA container '$MA_ID' not found. Re-run with the right name, e.g.:
  curl -fsSL $SCRIPT_URL | sh -s -- --ma-id <ID>"

# --- Detect Python version ---------------------------------------------------

if [ -z "$PYTHON_VERSION" ]; then
    PYTHON_VERSION="$(docker exec "$MA_ID" sh -c 'ls /app/venv/lib/ 2>/dev/null' \
                      | grep -E '^python3\.[0-9]+$' \
                      | head -n1 || true)"
    if [ -z "$PYTHON_VERSION" ]; then
        PYTHON_VERSION="python3.13"
        log "WARN: could not auto-detect Python version; using fallback '$PYTHON_VERSION'."
    else
        log "Detected MA Python version: $PYTHON_VERSION"
    fi
fi

DST_DIR="/app/venv/lib/$PYTHON_VERSION/site-packages/music_assistant/providers"

# --- Detect /config directory (for staging) ---------------------------------

if [ "$NO_STAGE" -ne 1 ] && [ -z "$CONFIG_DIR" ]; then
    if [ -d /mnt/data/supervisor/homeassistant ]; then
        CONFIG_DIR="/mnt/data/supervisor/homeassistant"
        log "Detected HAOS config path: $CONFIG_DIR"
    elif [ -d /usr/share/hassio/homeassistant ]; then
        CONFIG_DIR="/usr/share/hassio/homeassistant"
        log "Detected Supervised config path: $CONFIG_DIR"
    else
        log "WARN: could not detect /config path; skipping staging step."
        log "      Pass --config-dir DIR to enable staging."
        NO_STAGE=1
    fi
fi

# --- Download repo tarball --------------------------------------------------

TMPDIR="$(mktemp -d 2>/dev/null || mktemp -d -t mip)"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM

TARBALL_URL="https://codeload.github.com/$REPO_OWNER/$REPO_NAME/tar.gz/$REF"
log "Downloading $TARBALL_URL"
curl -fsSL "$TARBALL_URL" -o "$TMPDIR/repo.tar.gz" \
    || die "download failed (check --ref or your network)"

log "Extracting..."
tar -xzf "$TMPDIR/repo.tar.gz" -C "$TMPDIR" \
    || die "extraction failed (corrupt archive?)"

SAFE_REF="$(printf '%s' "$REF" | tr '/' '-')"
SRC_ROOT="$TMPDIR/$REPO_NAME-$SAFE_REF"
[ -d "$SRC_ROOT/$PROVIDER_DIR" ] \
    || die "$PROVIDER_DIR/ not found in archive at $SRC_ROOT"

# --- Stage to /config -------------------------------------------------------

if [ "$NO_STAGE" -ne 1 ]; then
    STAGE_DIR="$CONFIG_DIR/custom_components/mass/providers"
    STAGE_TARGET="$STAGE_DIR/$PROVIDER_DIR"
    RETIRED_STAGE_TARGET="$STAGE_DIR/$RETIRED_PROVIDER_DIR"

    if [ -e "$STAGE_TARGET" ]; then
        if [ "$FORCE" -ne 1 ]; then
            printf '%s already exists. Overwrite? [y/N] ' "$STAGE_TARGET"
            read -r reply
            case "$reply" in
                y|Y|yes|YES) ;;
                *) die "aborted by user (use --force to skip this prompt)" ;;
            esac
        fi
        log "Removing existing $STAGE_TARGET"
        rm -rf "$STAGE_TARGET"
    fi

    log "Staging to $STAGE_TARGET"
    mkdir -p "$STAGE_DIR"
    if [ -e "$RETIRED_STAGE_TARGET" ]; then
        log "Removing retired provider path $RETIRED_STAGE_TARGET"
        rm -rf "$RETIRED_STAGE_TARGET"
    fi
    cp -R "$SRC_ROOT/$PROVIDER_DIR" "$STAGE_TARGET"
fi

# --- Copy into MA container -------------------------------------------------

log "Copying provider into $MA_ID:$DST_DIR/"
# Remove any stale copy inside the container so docker cp doesn't merge into it.
docker exec "$MA_ID" rm -rf \
    "$DST_DIR/$PROVIDER_DIR" \
    "$DST_DIR/$RETIRED_PROVIDER_DIR" \
    2>/dev/null || true

docker cp "$SRC_ROOT/$PROVIDER_DIR" "$MA_ID:$DST_DIR/" \
    || die "docker cp failed. Is the MA container running?"
log "Provider files copied OK"

# --- Restart MA -------------------------------------------------------------

if [ "$NO_RESTART" -ne 1 ]; then
    log "Restarting $MA_ID..."
    docker restart "$MA_ID" >/dev/null \
        || die "docker restart failed"
    log "MA restarted. It may take ~10s to come back up."
else
    log "Skipping restart (--no-restart). Run 'docker restart $MA_ID' to apply."
fi

# --- Done -------------------------------------------------------------------

cat <<EOF

Install complete.

Next steps:
  1. Open Music Assistant -> Settings -> Music sources -> Add
     and select "YouTube Music".
  2. (Optional) For library sync, set Authentication to "Browser cookie"
     and paste your music.youtube.com cookie. See README.md -> Authentication.
  3. To survive Home Assistant restarts, install the watcher add-on:
     sh install_watcher_addon.sh

Re-run anytime to upgrade (pass --force so the overwrite prompt does not stall a
curl-pipe run). If autodetect picked wrong values, override with:
  curl -fsSL $SCRIPT_URL | sh -s -- --force --ma-id <ID> --python-version <pythonX.Y>
EOF
