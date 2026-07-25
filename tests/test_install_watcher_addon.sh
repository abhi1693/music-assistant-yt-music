#!/bin/sh
# Tests for scripts/install_watcher_addon.sh
#
# Run from the repo root:   sh tests/test_install_watcher_addon.sh
# Or as a CI step.
#
# Network-dependent tests auto-skip if GitHub is unreachable. Set
# SKIP_NETWORK_TESTS=1 to skip them unconditionally.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/install_watcher_addon.sh"
REPO_NAME="music-assistant-yt-music"

PASS=0
FAIL=0
SKIP=0

red()    { printf '\033[31m%s\033[0m' "$*"; }
green()  { printf '\033[32m%s\033[0m' "$*"; }
yellow() { printf '\033[33m%s\033[0m' "$*"; }

pass() { PASS=$((PASS+1)); printf '  %s %s\n' "$(green PASS)" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %s %s\n' "$(red   FAIL)" "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
skip() { SKIP=$((SKIP+1)); printf '  %s %s\n' "$(yellow SKIP)" "$1"; }

assert_eq() {
    # assert_eq <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        pass "$1"
    else
        fail "$1" "expected: $2 / actual: $3"
    fi
}

assert_contains() {
    # assert_contains <name> <needle> <haystack>
    case "$3" in
        *"$2"*) pass "$1" ;;
        *)      fail "$1" "expected to contain: $2" ;;
    esac
}

assert_file_exists() {
    # assert_file_exists <name> <path>
    if [ -f "$2" ]; then
        pass "$1"
    else
        fail "$1" "missing file: $2"
    fi
}

# --- Section 1: script structure / preflight (no network) -------------------

printf '\n== Script structure ==\n'

assert_file_exists "installer script exists" "$SCRIPT"

shebang="$(head -n1 "$SCRIPT")"
assert_eq "shebang is POSIX sh" "#!/bin/sh" "$shebang"

if sh -n "$SCRIPT" 2>/dev/null; then
    pass "POSIX sh -n syntax check"
else
    fail "POSIX sh -n syntax check"
fi

# Bashisms guard (best-effort; not exhaustive). Skips comments and
# heredoc bodies are not parsed semantically here -- a hit is still
# worth flagging for human review.
bashisms="$(grep -nE '\[\[|^[[:space:]]*local |<<<|\$\{[A-Za-z_]+,,\}|\$\{[A-Za-z_]+\^\^\}' "$SCRIPT" \
            | grep -v '^[[:space:]]*#' || true)"
if [ -z "$bashisms" ]; then
    pass "no obvious bashisms in installer"
else
    fail "no obvious bashisms in installer" "$bashisms"
fi

# --- MA container auto-detect regex (issue #35) -----------------------------

printf '\n== MA container auto-detect regex ==\n'

# Beta/nightly/dev MA installs name their container e.g.
# "addon_d5369777_music_assistant_beta"; auto-detect must match those, not only
# the stable "..._music_assistant" name (issue #35). Extract the exact pattern
# the script uses so this test tracks the real code, then exercise it (this also
# proves the ERE is portable to the dash/BusyBox grep the CI runs under).
MA_PAT="$(sed -n "s/.*grep -E '\(\^addon_[^']*\)'.*/\1/p" "$SCRIPT" | head -n1)"

if [ -n "$MA_PAT" ]; then
    pass "extracted MA-detect regex from script"
    assert_contains "MA-detect regex allows a channel suffix" "music_assistant(" "$MA_PAT"
    for _name in addon_d5369777_music_assistant \
                 addon_d5369777_music_assistant_beta \
                 addon_ff_music_assistant_nightly \
                 addon_ff_music_assistant_dev; do
        if printf '%s\n' "$_name" | grep -qE "$MA_PAT"; then
            pass "MA-detect regex matches $_name"
        else
            fail "MA-detect regex matches $_name" "expected a match"
        fi
    done
    for _name in addon_ff_ma_provider_watcher \
                 addon_ff_music_assistant_watcher \
                 addon_ff_some_music_assistant_x \
                 music_assistant; do
        if printf '%s\n' "$_name" | grep -qE "$MA_PAT"; then
            fail "MA-detect regex rejects $_name" "unexpected match"
        else
            pass "MA-detect regex rejects $_name"
        fi
    done
else
    fail "extracted MA-detect regex from script" "could not find the grep -E '^addon_...' line"
fi

# Recovery hints (including the one baked into the generated run.sh) must use the
# pipe-safe "sh -s --" form: the documented install is "curl ... | sh", where a
# bare "--flag" is parsed by sh itself and fails with "sh: bad option" (issue
# #35). Guard the fix against regressing.
src="$(cat "$SCRIPT")"
assert_contains "re-run hints use the sh -s -- separator" "sh -s --" "$src"
case "$src" in
    *"then re-run with --ma-id ID"*) fail "no bare 're-run with --ma-id ID' hint remains" ;;
    *) pass "no bare 're-run with --ma-id ID' hint remains" ;;
esac
case "$src" in
    *"sh install_watcher_addon.sh --force --ma-id"*) fail "no bare 'sh install_watcher_addon.sh --force --ma-id' hint remains" ;;
    *) pass "no bare 'sh install_watcher_addon.sh --force --ma-id' hint remains" ;;
esac
case "$src" in
    *"re-run install_watcher_addon.sh with --ma-id"*) fail "no bare run.sh 're-run install_watcher_addon.sh with --ma-id' hint remains" ;;
    *) pass "no bare run.sh 're-run install_watcher_addon.sh with --ma-id' hint remains" ;;
esac

printf '\n== Usage / error paths ==\n'

help_out="$(sh "$SCRIPT" --help 2>&1)"; help_rc=$?
assert_eq "--help exits 0" "0" "$help_rc"
assert_contains "--help prints Usage:" "Usage:" "$help_out"
assert_contains "--help mentions --force" "--force" "$help_out"
assert_contains "--help mentions --ref" "--ref" "$help_out"
assert_contains "--help mentions --ma-id" "--ma-id" "$help_out"
assert_contains "--help mentions --python-version" "--python-version" "$help_out"

bad_out="$(sh "$SCRIPT" --bogus-option 2>&1)"; bad_rc=$?
if [ "$bad_rc" -ne 0 ]; then
    pass "unknown option exits non-zero"
else
    fail "unknown option exits non-zero" "exit code was 0"
fi
assert_contains "unknown option mentions the option" "--bogus-option" "$bad_out"

missing_dir_out="$(sh "$SCRIPT" --addons-dir /nonexistent/path/does/not/exist 2>&1)"; missing_dir_rc=$?
if [ "$missing_dir_rc" -ne 0 ]; then
    pass "missing --addons-dir target exits non-zero"
else
    fail "missing --addons-dir target exits non-zero" "exit code was 0"
fi
assert_contains "missing dir error mentions the path" "/nonexistent/path/does/not/exist" "$missing_dir_out"

# Auto-detection failure message must list the modern apps/local candidates, not
# only the legacy addons/local path (issue #22: HAOS 18+ renamed addons -> apps).
# --ma-id / --python-version are passed so this never depends on docker.
autodetect_out="$(sh "$SCRIPT" --force --ma-id x --python-version python3.13 2>&1)"; autodetect_rc=$?
if [ "$autodetect_rc" -ne 0 ]; then
    pass "auto-detect with no known dirs exits non-zero"
    assert_contains "auto-detect error lists apps/local candidates" "apps/local" "$autodetect_out"
    assert_contains "auto-detect error mentions in-container /addons path" "/addons" "$autodetect_out"
    case "$autodetect_out" in
        *"/root/addons"*) fail "auto-detect error drops non-standard /root/addons" ;;
        *) pass "auto-detect error drops non-standard /root/addons" ;;
    esac
else
    skip "auto-detect found a real add-ons dir on this host -- failure path not exercised"
fi

# --- Section 2: end-to-end install ------------------------------------------

printf '\n== End-to-end install ==\n'

if ! command -v curl >/dev/null 2>&1; then
    skip "curl not available -- skipping end-to-end tests"
else
    TMP_ADDONS="$(mktemp -d)"
    trap 'rm -rf "$TMP_ADDONS"' EXIT INT TERM
    FIXTURE_ROOT="$TMP_ADDONS/$REPO_NAME-master"
    FIXTURE_ARCHIVE="$TMP_ADDONS/provider-fixture.tar.gz"
    mkdir -p "$FIXTURE_ROOT"
    cp -R "$REPO_ROOT/ytmusic" "$FIXTURE_ROOT/ytmusic"
    tar -czf "$FIXTURE_ARCHIVE" -C "$TMP_ADDONS" "$REPO_NAME-master"
    rm -rf "$FIXTURE_ROOT"
    FIXTURE_URL="file://$FIXTURE_ARCHIVE"

    install_out="$(YTMUSIC_TARBALL_URL_OVERRIDE="$FIXTURE_URL" sh "$SCRIPT" \
        --force \
        --addons-dir "$TMP_ADDONS" \
        --ma-id addon_TESTID_music_assistant \
        --python-version python3.99 2>&1)"
    install_rc=$?

    assert_eq "install exits 0" "0" "$install_rc"
    assert_contains "install output reports completion" "Install complete" "$install_out"
    assert_contains "install output reports next steps" "Next steps:" "$install_out"

    ADDON="$TMP_ADDONS/ma_provider_watcher"
    assert_file_exists "config.yaml created"  "$ADDON/config.yaml"
    assert_file_exists "build.yaml created"   "$ADDON/build.yaml"
    assert_file_exists "Dockerfile created"   "$ADDON/Dockerfile"
    assert_file_exists "run.sh created"       "$ADDON/run.sh"
    assert_file_exists "ytmusic/__init__.py copied"   "$ADDON/ytmusic/__init__.py"
    assert_file_exists "ytmusic/manifest.json copied" "$ADDON/ytmusic/manifest.json"

    if [ -x "$ADDON/run.sh" ]; then
        pass "run.sh is executable"
    else
        # chmod is best-effort (silently ignored on filesystems without exec bit)
        skip "run.sh executable bit (filesystem may not support it)"
    fi

    config="$(cat "$ADDON/config.yaml")"
    assert_contains "config.yaml has slug"      "slug: ma_provider_watcher" "$config"
    assert_contains "config.yaml has docker_api" "docker_api: true"          "$config"
    assert_contains "config.yaml has boot auto"  "boot: auto"                "$config"
    # Version must be build-stamped (1.0.<timestamp>), not the static "1.0.0",
    # so HA detects a change and rebuilds the cached image on re-run (issue #22).
    assert_contains "config.yaml version is build-stamped" 'version: "1.0.2' "$config"
    case "$config" in
        *'version: "1.0.0"'*) fail "config.yaml version is not the static 1.0.0" ;;
        *) pass "config.yaml version is not the static 1.0.0" ;;
    esac

    runsh="$(cat "$ADDON/run.sh")"
    assert_contains "run.sh has substituted MA ID" 'MA="addon_TESTID_music_assistant"' "$runsh"
    assert_contains "run.sh has substituted Python version" "/python3.99/" "$runsh"
    assert_contains "run.sh shebang is bash" "#!/usr/bin/env bash" "$runsh"
    assert_contains "run.sh logs the watched container name on start" "Watching for container name" "$runsh"
    assert_contains "run.sh has misconfig diagnostic function" "warn_if_ma_misconfigured" "$runsh"
    assert_contains "run.sh references MISSING_GRACE_SECONDS" "MISSING_GRACE_SECONDS" "$runsh"
    assert_contains "run.sh diagnostic mentions --ma-id remedy" "--ma-id" "$runsh"
    assert_contains "run.sh removes the retired provider path" \
        'retired_provider="ytmusic""_free"' "$runsh"

    if bash -n "$ADDON/run.sh" 2>/dev/null; then
        pass "generated run.sh passes bash -n"
    elif command -v bash >/dev/null 2>&1; then
        fail "generated run.sh passes bash -n"
    else
        skip "bash not available to syntax-check generated run.sh"
    fi

    dockerfile="$(cat "$ADDON/Dockerfile")"
    assert_contains "Dockerfile copies provider" "COPY ytmusic/ /provider/ytmusic/" "$dockerfile"
    assert_contains "Dockerfile installs docker-cli" "docker-cli" "$dockerfile"

    # --- Idempotency ---
    printf '\n== Idempotency ==\n'

    # Plant a sentinel file to detect overwrite behavior.
    printf 'SENTINEL_FROM_PREVIOUS_INSTALL\n' > "$ADDON/SENTINEL"

    # Without --force, "n" answer should abort and leave the install untouched.
    abort_out="$(
        printf 'n\n' \
            | env YTMUSIC_TARBALL_URL_OVERRIDE="$FIXTURE_URL" \
                sh "$SCRIPT" --addons-dir "$TMP_ADDONS" \
                    --ma-id x --python-version python3.13 2>&1
    )"
    abort_rc=$?
    if [ "$abort_rc" -ne 0 ]; then
        pass "re-install without --force and 'n' answer aborts"
    else
        fail "re-install without --force and 'n' answer aborts" "exit code was 0"
    fi
    assert_contains "abort output mentions --force" "--force" "$abort_out"
    assert_file_exists "aborted re-install leaves sentinel intact" "$ADDON/SENTINEL"

    # With --force, sentinel must be gone afterwards.
    YTMUSIC_TARBALL_URL_OVERRIDE="$FIXTURE_URL" \
        sh "$SCRIPT" --force --addons-dir "$TMP_ADDONS" \
        --ma-id addon_FORCED_music_assistant --python-version python3.42 \
        >/dev/null 2>&1
    if [ ! -f "$ADDON/SENTINEL" ]; then
        pass "--force overwrites prior install (sentinel removed)"
    else
        fail "--force overwrites prior install (sentinel removed)"
    fi
    runsh2="$(cat "$ADDON/run.sh" 2>/dev/null || echo '')"
    assert_contains "--force re-substitutes MA ID" 'MA="addon_FORCED_music_assistant"' "$runsh2"
    assert_contains "--force re-substitutes Python version" "/python3.42/" "$runsh2"

    # --- Auto-detection fallback ---
    # When no docker is present (or no MA-named container is running), the
    # script must warn and fall back to the documented defaults rather than
    # exit. This is the path BusyBox/HAOS-style sandboxed runs hit.
    printf '\n== Auto-detection fallback ==\n'

    rm -rf "$ADDON"
    fallback_out="$(
        YTMUSIC_TARBALL_URL_OVERRIDE="$FIXTURE_URL" \
            sh "$SCRIPT" --force --addons-dir "$TMP_ADDONS" 2>&1
    )"
    fallback_rc=$?
    assert_eq "fallback install exits 0" "0" "$fallback_rc"

    if printf '%s' "$fallback_out" | grep -q 'could not auto-detect MA container'; then
        pass "warns when MA container is not detected"
        runsh3="$(cat "$ADDON/run.sh" 2>/dev/null || echo '')"
        assert_contains "fallback MA ID baked into run.sh" \
            'MA="addon_d5369777_music_assistant"' "$runsh3"
    else
        skip "test environment auto-detected an MA container -- warning path not exercised"
    fi

    if printf '%s' "$fallback_out" | grep -q 'could not auto-detect Python version'; then
        pass "warns when Python version is not detected"
        runsh3="$(cat "$ADDON/run.sh" 2>/dev/null || echo '')"
        assert_contains "fallback Python version baked into run.sh" \
            "/python3.13/" "$runsh3"
    else
        skip "test environment auto-detected a Python version -- fallback path not exercised"
    fi
fi

# --- Summary ----------------------------------------------------------------

printf '\n== Summary ==\n'
printf '  passed:  %s\n' "$PASS"
printf '  failed:  %s\n' "$FAIL"
printf '  skipped: %s\n' "$SKIP"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
