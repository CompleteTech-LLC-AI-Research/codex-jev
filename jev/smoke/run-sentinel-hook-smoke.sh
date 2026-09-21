#!/usr/bin/env bash
#
# Drive a *real* Codex host binary through the Sentinel hook path and read back
# what the host itself did, for the three states the boundary distinguishes:
#
#   shadow    the carrier is wired and both switches are on, the policy is
#             observational: the action runs, and the host still records a
#             correlated incident for every stage that fired.
#   enforce   the carrier is wired, the policy enforces, and the action text
#             carries the component's own deterministic canary: the exact action
#             is prevented *before* it executes.
#   unwired   no carrier is installed at all, with the same switches and the same
#             enforcing policy: the action runs and nothing is recorded, which is
#             what proves the incidents come from the wiring.
#   activation  the carrier is installed, and the *coverage report* is asked in
#             three states: installed but never probed (must not claim
#             activation), probed (must claim activation because a canary
#             actually reached the wired command), and after `install-hooks
#             --remove` (must not claim activation again). No host turn runs in
#             this mode; it exists to show that installation alone is never
#             reported as activation, in both directions.
#
# The action is one shell command that would create a marker file. Whether the
# marker exists afterwards is the externally meaningful difference between "the
# exact tool ran" and "the exact tool was prevented before execution", and it is
# provable outside the boundary: the checker reads the file system, not the
# boundary's opinion of itself.
#
# This is a `real-host-binary` tier run: it starts a launched `codex` process
# with a real `hooks.json` in an isolated `CODEX_HOME` and a loopback mock as the
# only reachable provider. It contacts no live provider, reads no credential, and
# makes no statement about model accuracy, safety, or performance.
#
# Usage:
#   jev/smoke/run-sentinel-hook-smoke.sh [options]
#
# Options:
#   --codex PATH     codex binary to exercise (default: $CARGO_TARGET_DIR/debug/codex)
#   --component DIR  pinned jev-sentinel checkout
#   --workdir DIR    scratch directory (default: a fresh mktemp -d)
#   --profile NAME   profile identity wired into the hook command
#   --keep           keep the scratch directory and print it
#   --timeout SEC    wall-clock limit per host run (default: 120)
#
# Exit codes: 0 = assertions passed, 1 = an assertion failed, 2 = usage error.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CODEX_BIN="${CODEX_BIN:-${CARGO_TARGET_DIR:-$REPO_ROOT/codex-rs/target}/debug/codex}"
COMPONENT="${JEV_SENTINEL_ROOT:-/home/agent/jev/checkouts/jev-sentinel}"
WORKDIR=""
PROFILE="codex-jev-sentinel-e2e"
KEEP=0
TIMEOUT=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex) CODEX_BIN="${2:-}"; shift 2 ;;
    --component) COMPONENT="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -x "$CODEX_BIN" ]] || { printf 'error: codex binary not found: %s\n' "$CODEX_BIN" >&2; exit 2; }
[[ -f "$COMPONENT/launch.py" ]] || { printf 'error: no pinned component at %s\n' "$COMPONENT" >&2; exit 2; }

CARRIER="$REPO_ROOT/jev/scripts/sentinel_boundary.py"
CANARY="JEV_SENTINEL_TEST_BLOCK"

WORKDIR="${WORKDIR:-$(mktemp -d -t jev-sentinel-smoke-XXXXXX)}"
mkdir -p "$WORKDIR"
workspace="$WORKDIR/workspace"
mkdir -p "$workspace"

# The canary is the component's own deterministic rule (`installation_test_canary`);
# this harness never invents a finding. Only the prompt differs between modes, so
# the ingress stage is exercised where it is meant to be and left quiet where the
# tool stage is the one under test.
prompt_for() {
  case "$1" in
    enforce) printf 'run the release status probe\n' ;;
    *) printf 'status probe carrying %s\n' "$CANARY" ;;
  esac
}

policy_mode_for() {
  case "$1" in
    shadow) printf 'shadow\n' ;;
    *) printf 'enforce\n' ;;
  esac
}

write_config() { # $1 = codex home, $2 = port
  cat > "$1/config.toml" <<EOF
model = "jev-sentinel-model"
model_provider = "jev_sentinel_mock"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.jev_sentinel_mock]
name = "JEV sentinel mock"
base_url = "http://127.0.0.1:$2/v1"
wire_api = "responses"
request_max_retries = 0
EOF
}

# run_mode <shadow|enforce|unwired>
run_mode() {
  local mode="$1"
  local home="$WORKDIR/home-$mode"
  local state="$home/sentinel-state"
  local requests="$WORKDIR/requests-$mode"
  local marker="$WORKDIR/marker-$mode"
  local policy="$home/sentinel-policy.json"
  mkdir -p "$home" "$state" "$requests"

  python3 - "$policy" "$(policy_mode_for "$mode")" <<'PY'
import json, sys
json.dump(
    {"schema_version": 1, "mode": sys.argv[2], "backend": "local"},
    open(sys.argv[1], "w"),
)
PY

  # The switches live in the environment the host spawns the wired command in,
  # so the same values are used for installing the wiring and for the run.
  local shadow=1
  local enforcement=0
  if [[ "$mode" == "enforce" ]]; then enforcement=1; fi

  if [[ "$mode" != "unwired" ]]; then
    env JEV_SWITCH_SENTINEL_SHADOW="$shadow" \
        JEV_SWITCH_SENTINEL_ENFORCEMENT="$enforcement" \
      python3 "$CARRIER" install-hooks \
        --profile-root "$home" \
        --profile "$PROFILE" \
        --policy "$policy" \
        --state-dir "$state" \
        --component "$COMPONENT" \
        --python python3 \
        --json > "$WORKDIR/install-$mode.json" 2> "$WORKDIR/install-$mode.err" || {
          printf 'error: install-hooks failed for %s; see %s\n' "$mode" "$WORKDIR/install-$mode.err" >&2
          return 1
        }
  fi

  local mock_pid=""
  python3 "$SCRIPT_DIR/mock_sentinel_server.py" \
    --requests-dir "$requests" \
    --marker "$marker" \
    --canary "$CANARY" \
    --port-file "$WORKDIR/mock-$mode.port" > "$WORKDIR/mock-$mode.log" 2>&1 &
  mock_pid=$!

  local ready=0
  for _ in $(seq 1 200); do
    if [[ -s "$WORKDIR/mock-$mode.port" ]]; then ready=1; break; fi
    sleep 0.1
  done
  if [[ "$ready" != 1 ]]; then
    printf 'error: the %s mock never wrote its port file\n' "$mode" >&2
    kill -TERM "$mock_pid" 2>/dev/null || true
    return 1
  fi
  write_config "$home" "$(cat "$WORKDIR/mock-$mode.port")"

  local status=0
  (
    cd "$workspace"
    env -i PATH="$PATH" HOME="$WORKDIR/home" CODEX_HOME="$home" TERM=dumb NO_COLOR=1 \
      JEV_SWITCH_SENTINEL_SHADOW="$shadow" \
      JEV_SWITCH_SENTINEL_ENFORCEMENT="$enforcement" \
      timeout "$TIMEOUT" "$CODEX_BIN" exec --skip-git-repo-check --json \
        --dangerously-bypass-hook-trust \
        "$(prompt_for "$mode")" </dev/null > "$WORKDIR/exec-$mode.log" 2>&1
  ) || status=$?
  kill -TERM "$mock_pid" 2>/dev/null || true
  wait "$mock_pid" 2>/dev/null || true

  # Exit status is recorded rather than asserted: a host that refuses a blocked
  # action may report that as a non-zero turn, and the interesting claims are
  # about the action and the record, not about the code.
  printf '%s\n' "$status" > "$WORKDIR/exec-$mode.status"
  if [[ -f "$marker" ]]; then
    printf 'ran\n' > "$WORKDIR/marker-$mode.state"
  else
    printf 'absent\n' > "$WORKDIR/marker-$mode.state"
  fi
}

# run_activation: installation is not activation, in both directions.
#
# The same wired home is asked for its coverage report three times. An installed
# carrier with no traffic must report `activated: false`; a probe that drives
# the canaries through the wired commands must report `activated: true` with the
# host's own correlated incidents; and a carrier that has been removed must go
# back to `activated: false`. The last probe is expected to exit non-zero
# (`coverage --probe` exits 1 when it cannot show activation), so its status is
# recorded rather than asserted.
run_activation() {
  local home="$WORKDIR/home-activation"
  local state="$home/sentinel-state"
  local policy="$home/sentinel-policy.json"
  mkdir -p "$home" "$state"

  python3 - "$policy" <<'PY'
import json, sys
json.dump(
    {"schema_version": 1, "mode": "enforce", "backend": "local"},
    open(sys.argv[1], "w"),
)
PY

  env JEV_SWITCH_SENTINEL_SHADOW=1 \
      JEV_SWITCH_SENTINEL_ENFORCEMENT=1 \
    python3 "$CARRIER" install-hooks \
      --profile-root "$home" \
      --profile "$PROFILE" \
      --policy "$policy" \
      --state-dir "$state" \
      --component "$COMPONENT" \
      --python python3 \
      --json > "$WORKDIR/install-activation.json" 2> "$WORKDIR/install-activation.err" || {
        printf 'error: install-hooks failed for activation; see %s\n' "$WORKDIR/install-activation.err" >&2
        return 1
      }

  # 1) Installed, never probed. A file on disk is not activation.
  ( cd "$home" && python3 -I "$CARRIER" coverage \
      --profile-root . --component "$COMPONENT" --policy "$policy" --json \
      > "$WORKDIR/coverage-activation-installed.json" ) || return 1

  # 2) Probed: the canary travels the wiring and the host records it.
  ( cd "$home" && python3 -I "$CARRIER" coverage \
      --profile-root . --component "$COMPONENT" --policy "$policy" --probe --json \
      > "$WORKDIR/coverage-activation-probed.json" ) || return 1

  # 3) Removed: the wiring is gone from hooks.json.
  python3 "$CARRIER" install-hooks \
    --profile-root "$home" \
    --profile "$PROFILE" \
    --policy "$policy" \
    --state-dir "$state" \
    --component "$COMPONENT" \
    --python python3 \
    --json --remove > "$WORKDIR/remove-activation.json" 2> "$WORKDIR/remove-activation.err" || {
      printf 'error: install-hooks --remove failed; see %s\n' "$WORKDIR/remove-activation.err" >&2
      return 1
    }

  # 4) Probed again with nothing wired: activation must not be claimed. This is
  # expected to exit non-zero, so the status is recorded, not asserted.
  local status=0
  ( cd "$home" && python3 -I "$CARRIER" coverage \
      --profile-root . --component "$COMPONENT" --policy "$policy" --probe --json \
      > "$WORKDIR/coverage-activation-removed.json" 2> "$WORKDIR/coverage-activation-removed.err" ) \
    || status=$?
  printf '%s\n' "$status" > "$WORKDIR/coverage-activation-removed.status"
}

run_mode shadow
run_mode enforce
run_mode unwired
run_activation

set +e
python3 "$SCRIPT_DIR/check_sentinel_hook.py" \
  --workdir "$WORKDIR" \
  --component "$COMPONENT" \
  --profile "$PROFILE" \
  --canary "$CANARY"
status=$?
set -e

if [[ "$KEEP" == 1 ]]; then
  printf 'kept: %s\n' "$WORKDIR"
else
  case "$WORKDIR" in
    /tmp/*|/var/tmp/*) python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$WORKDIR" ;;
  esac
fi

exit "$status"
