#!/usr/bin/env bash
#
# Build the pinned codex-jev fork in an isolated configuration.
#
# The build always happens in a throwaway git worktree checked out at the host
# base commit recorded in jev/compatibility-manifest.json. The integration
# layer (jev/) is copied in, the manifest patches are applied (or deliberately
# skipped), the profile is validated fail-closed, and the compile runs with an
# isolated CODEX_HOME so no user configuration, credential, or session leaks in.
#
# This script never runs WSL shutdown/terminate commands. It does not touch an
# active agent profile, and it writes only inside its build directory (default
# .jev-build/, which is git-ignored) and the isolated worktree it creates.
#
# Usage: jev/scripts/build-pinned-codex.sh [options]
#   --no-patches          Build the pinned base WITHOUT the manifest patches
#                         (the disable path; pairs with the isolated-build profile).
#   --profile PATH        Integration profile to validate and record.
#   --build-dir DIR       Build root (default: $JEV_BUILD_DIR or <repo>/.jev-build).
#   --codex-home DIR      Isolated CODEX_HOME (default: <build-dir>/codex-home).
#   --target-dir DIR      CARGO_TARGET_DIR (default: <build-dir>/target).
#   --release             Build release binaries (default: debug).
#   --openssl-vendored    Container workaround: build OpenSSL from source in the
#                         throwaway worktree. Never committed; see BUILD.md.
#   --reuse               Reuse an existing worktree instead of recreating it.
#   --check               Validate pin, patch state, profile, and fixtures only.
#   --verify-fixtures     Run the offline fixture validator as part of the build.
#   --smoke               Launch the built binary (codex --version) after building.
#                         On by default; disable with --no-smoke.
#   --print-provenance    Print the provenance JSON path and exit.
#   --clean               Remove the isolated worktree and build directory.
#   -h, --help            Show this help.
#
# Exit codes: 0 = success, 1 = failure, 2 = usage error.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$REPO_ROOT/jev/compatibility-manifest.json"

BUILD_DIR="${JEV_BUILD_DIR:-$REPO_ROOT/.jev-build}"
PATCHES="applied"
PROFILE=""
RELEASE=0
REUSE=0
CHECK_ONLY=0
VERIFY_FIXTURES=0
SMOKE=1
OPENSSL_VENDORED=0
PRINT_PROVENANCE=0
CLEAN=0

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage_error() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

info() {
    printf '[jev-build] %s\n' "$*"
}

remove_worktree() {
    # Remove a possibly locked or partially created worktree without touching
    # the working checkout. Falls back to prune plus a direct delete.
    if git -C "$REPO_ROOT" worktree list --porcelain | grep -q "^worktree $WORKTREE$"; then
        git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" 2>/dev/null \
            || git -C "$REPO_ROOT" worktree remove --force --force "$WORKTREE" 2>/dev/null \
            || true
    fi
    git -C "$REPO_ROOT" worktree prune
    python3 - "$WORKTREE" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-patches) PATCHES="absent"; shift ;;
        --profile) [ "$#" -ge 2 ] || usage_error "--profile needs a path"; PROFILE="$2"; shift 2 ;;
        --build-dir) [ "$#" -ge 2 ] || usage_error "--build-dir needs a path"; BUILD_DIR="$2"; shift 2 ;;
        --codex-home) [ "$#" -ge 2 ] || usage_error "--codex-home needs a path"; CODEX_HOME_OVERRIDE="$2"; shift 2 ;;
        --target-dir) [ "$#" -ge 2 ] || usage_error "--target-dir needs a path"; TARGET_DIR_OVERRIDE="$2"; shift 2 ;;
        --release) RELEASE=1; shift ;;
        --openssl-vendored) OPENSSL_VENDORED=1; shift ;;
        --reuse) REUSE=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --verify-fixtures) VERIFY_FIXTURES=1; shift ;;
        --smoke) SMOKE=1; shift ;;
        --no-smoke) SMOKE=0; shift ;;
        --print-provenance) PRINT_PROVENANCE=1; shift ;;
        --clean) CLEAN=1; shift ;;
        -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) usage_error "unknown option: $1" ;;
    esac
done

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
command -v git >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

WORKTREE="$BUILD_DIR/pinned-base"
CODEX_HOME="${CODEX_HOME_OVERRIDE:-$BUILD_DIR/codex-home}"
TARGET_DIR="${TARGET_DIR_OVERRIDE:-$BUILD_DIR/target}"
PROVENANCE="$BUILD_DIR/provenance.json"

manifest_field() {
    python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
node = data
for key in sys.argv[2].split("."):
    node = node[key]
print(node)
PY
}

BASE="$(manifest_field host.base_commit)"
[ -n "$BASE" ] || die "manifest host.base_commit is empty"

if [ -z "$PROFILE" ]; then
    if [ "$PATCHES" = "absent" ]; then
        PROFILE="$REPO_ROOT/jev/profiles/isolated-build.json"
    else
        PROFILE="$REPO_ROOT/jev/profiles/integrated-offline.json"
    fi
fi
PROFILE="$(cd -- "$(dirname -- "$PROFILE")" && pwd)/$(basename -- "$PROFILE")"
[ -f "$PROFILE" ] || die "profile not found: $PROFILE"

if [ "$CLEAN" -eq 1 ]; then
    info "removing isolated worktree and build directory"
    remove_worktree
    python3 - "$BUILD_DIR" <<'PY'
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
    info "clean complete; the working checkout is untouched"
    exit 0
fi

info "host base $BASE"
info "patch state: $PATCHES"
info "profile: $PROFILE"
info "build dir: $BUILD_DIR"

git -C "$REPO_ROOT" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null \
    || die "pinned base commit $BASE is not available in this checkout"

mkdir -p "$BUILD_DIR"

if [ -d "$WORKTREE/.git" ] || [ -f "$WORKTREE/.git" ]; then
    if [ "$REUSE" -eq 1 ]; then
        current="$(git -C "$WORKTREE" rev-parse HEAD)"
        [ "$current" = "$BASE" ] || die "existing worktree is at $current, not the pinned base $BASE"
        info "reusing worktree at $WORKTREE"
    else
        info "recreating worktree at $WORKTREE"
        remove_worktree
        git -C "$REPO_ROOT" worktree add --detach "$WORKTREE" "$BASE" >/dev/null
    fi
else
    git -C "$REPO_ROOT" worktree add --detach "$WORKTREE" "$BASE" >/dev/null
fi

info "copying the integration layer into the isolated worktree"
python3 - "$REPO_ROOT/jev" "$WORKTREE/jev" <<'PY'
import shutil, sys
src, dst = sys.argv[1], sys.argv[2]
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
PY

if [ "$PATCHES" = "applied" ]; then
    info "applying manifest patches"
    python3 "$WORKTREE/jev/scripts/apply-patches.py" --repo-root "$WORKTREE"
else
    info "skipping manifest patches (disable path)"
    python3 "$WORKTREE/jev/scripts/apply-patches.py" --repo-root "$WORKTREE" --check
fi

info "validating pin, patch state, and profile"
python3 "$WORKTREE/jev/scripts/verify-manifest.py" \
    --repo-root "$WORKTREE" --patch-state "$PATCHES" --check-checkout
python3 "$WORKTREE/jev/scripts/verify-manifest.py" \
    --repo-root "$WORKTREE" --profile "$PROFILE"

if [ "$VERIFY_FIXTURES" -eq 1 ]; then
    info "validating deterministic offline fixtures"
    python3 "$WORKTREE/jev/scripts/run-offline-fixtures.py"
fi

info "preparing isolated CODEX_HOME at $CODEX_HOME"
mkdir -p "$CODEX_HOME"
chmod 700 "$CODEX_HOME" 2>/dev/null || true
cp "$WORKTREE/jev/fixtures/codex-home/config.toml" "$CODEX_HOME/config.toml"
if [ -e "$CODEX_HOME/auth.json" ]; then
    die "isolated CODEX_HOME unexpectedly contains auth.json; refusing to build"
fi

# Keep the C toolchain's scratch files inside the build directory. A small or
# full /tmp is a common CI/container failure mode (aws-lc-sys writes large
# temporary assembly there), so never rely on the default.
mkdir -p "$BUILD_DIR/tmp"

if [ "$CHECK_ONLY" -eq 1 ]; then
    info "check-only: pin, patch state, profile, and fixtures validated; not compiling"
    exit 0
fi

PROFILE_KIND="debug"
CARGO_ARGS=(build -p codex-cli --bin codex)
if [ "$RELEASE" -eq 1 ]; then
    PROFILE_KIND="release"
    CARGO_ARGS+=(--release)
fi

if [ "$OPENSSL_VENDORED" -eq 1 ]; then
    info "container workaround: building OpenSSL from source in the throwaway worktree"
    python3 - "$WORKTREE/codex-rs/core/Cargo.toml" <<'PY'
import sys
path = sys.argv[1]
marker = "[target.x86_64-unknown-linux-gnu.dependencies]"
text = open(path, encoding="utf-8").read()
if marker not in text:
    text += (
        "\n# JEV container build workaround (throwaway worktree only): no system\n"
        "# OpenSSL development files are available, so vendor OpenSSL for gnu too.\n"
        + marker + "\n"
        'openssl-sys = { workspace = true, features = ["vendored"] }\n'
    )
    open(path, "w", encoding="utf-8").write(text)
PY
elif grep -q "JEV container build workaround" "$WORKTREE/codex-rs/core/Cargo.toml"; then
    die "worktree carries an unexpectedly patched Cargo.toml; recreate without --reuse"
fi

info "building codex ($PROFILE_KIND) with an isolated environment"
env -i \
    PATH="$PATH" \
    HOME="${HOME:-/tmp}" \
    CODEX_HOME="$CODEX_HOME" \
    TMPDIR="$BUILD_DIR/tmp" \
    CARGO_TARGET_DIR="$TARGET_DIR" \
    RUST_MIN_STACK=8388608 \
    CARGO_TERM_COLOR="${CARGO_TERM_COLOR:-never}" \
    bash -c 'cd "$1/codex-rs" && shift && exec cargo "$@"' _ "$WORKTREE" "${CARGO_ARGS[@]}"

BIN="$TARGET_DIR/$PROFILE_KIND/codex"
[ -x "$BIN" ] || die "expected binary not found: $BIN"

if [ "$SMOKE" -eq 1 ]; then
    info "smoke: launching the isolated binary"
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" TMPDIR="$BUILD_DIR/tmp" CODEX_HOME="$CODEX_HOME" "$BIN" --version >/dev/null \
        || die "the isolated binary failed to launch"
    info "smoke: isolated binary launched successfully"
fi

BIN_SHA="$(python3 - "$BIN" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(65536), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"

RUSTC_VERSION="$(cd "$WORKTREE/codex-rs" && rustc --version 2>/dev/null || echo unknown)"
CARGO_VERSION="$(cd "$WORKTREE/codex-rs" && cargo --version 2>/dev/null || echo unknown)"

python3 - "$PROVENANCE" <<PY
import json, platform, sys, time
import pathlib

manifest = json.load(open("$MANIFEST", encoding="utf-8"))
profile = json.load(open("$PROFILE", encoding="utf-8"))
patches = manifest["patches"] if "$PATCHES" == "applied" else []
record = {
    "schema": "jev.build-provenance/1",
    "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "tier": "offline fixture",
    "integration_host": manifest["integration"]["repository"],
    "base_commit": "$BASE",
    "patch_state": "$PATCHES",
    "patches": [{"id": p["id"], "order": p["order"], "sha256": p["sha256"]} for p in patches],
    "profile_id": profile["id"],
    "features": profile.get("features", {}),
    "platform": {"os": platform.system(), "arch": platform.machine()},
    "toolchain": {"rustc": "$RUSTC_VERSION", "cargo": "$CARGO_VERSION"},
    "container_openssl_vendored": bool($OPENSSL_VENDORED),
    "codex_home": "$CODEX_HOME",
    "target_dir": "$TARGET_DIR",
    "binary": "$BIN",
    "binary_sha256": "$BIN_SHA",
    "profile_kind": "$PROFILE_KIND",
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

info "provenance written to $PROVENANCE"
cat "$PROVENANCE"

info "build complete"
info "run the isolated binary:  CODEX_HOME=$CODEX_HOME $BIN --version"
info "rollback:  jev/scripts/build-pinned-codex.sh --clean   (see jev/BUILD.md)"

if [ "$PRINT_PROVENANCE" -eq 1 ]; then
    printf '%s\n' "$PROVENANCE"
fi
