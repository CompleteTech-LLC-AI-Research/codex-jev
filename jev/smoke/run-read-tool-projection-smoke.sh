#!/usr/bin/env bash
#
# Drive the built Codex host through a transcript that carries an eligible
# duplicate read pair, once with the projection switch on, once with it off, and
# once with no bus adapter at all, then assert the outgoing view was projected,
# that the switch-off body is an exact reset, and that the persisted transcript
# was never touched. This is the `real-host-binary` evidence for #5 criterion 3.
#
# Usage:
#   jev/smoke/run-read-tool-projection-smoke.sh [options]
#
# Options:
#   --codex PATH     codex binary to exercise (default: $CARGO_TARGET_DIR/release/codex)
#   --workdir DIR    scratch directory (default: a fresh mktemp -d)
#   --keep           keep the scratch directory and print it
#   --timeout SEC    wall-clock limit for each host run (default: 180)
#
# Exit codes: 0 = assertions passed, 1 = an assertion failed, 2 = usage error.
#
# The read tool the transcript calls is the memories extension's `read`: it is
# the only read route in this fork that answers with a deterministic body, which
# is what makes a byte-identical duplicate pair observable at all. The fixture
# therefore enables `[features] memories` and `[memories] dedicated_tools`.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CODEX_BIN="${CODEX_BIN:-${CARGO_TARGET_DIR:-$REPO_ROOT/codex-rs/target}/release/codex}"
WORKDIR=""
KEEP=0
TIMEOUT=180

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex) CODEX_BIN="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -x "$CODEX_BIN" ]] || { printf 'error: codex binary not found: %s\n' "$CODEX_BIN" >&2; exit 2; }

WORKDIR="${WORKDIR:-$(mktemp -d -t jev-projection-smoke-XXXXXX)}"
mkdir -p "$WORKDIR"
workspace="$WORKDIR/workspace"
mkdir -p "$workspace"

STAGE_COMMAND="python3 $REPO_ROOT/jev/tests/bus_stage_stub/dedup.py"

# Every run reads the same fixture files. The first two projection paths are the
# same file, which is what makes their bodies byte-identical; the rest are
# distinct so they can never join that group.
populate_memories() {
  python3 - "$1" <<'PY'
import os
import sys

root = os.path.join(sys.argv[1], "memories")
os.makedirs(root, exist_ok=True)
body = "".join(
    f"line {index:03d}: deterministic memory probe content alphabet soup\n"
    for index in range(1, 41)
)
with open(os.path.join(root, "jev-probe.txt"), "w") as handle:
    handle.write(body)
for index in range(3, 12):
    with open(os.path.join(root, f"probe-{index:02d}.txt"), "w") as handle:
        handle.write(body)
PY
}

write_config() { # $1 = codex home, $2 = port
  cat > "$1/config.toml" <<EOF
model = "jev-probe-model"
model_provider = "jev_probe_mock"
approval_policy = "never"

[features]
memories = true

[memories]
use_memories = true
dedicated_tools = true

[model_providers.jev_probe_mock]
name = "JEV probe mock"
base_url = "http://127.0.0.1:$2/v1"
wire_api = "responses"
request_max_retries = 0
EOF
}

# run_mode <on|off|none>
#
# All three modes share one CODEX_HOME path and are separated by snapshotting
# the rollout directory between them. Keeping the path identical is what lets
# the checker compare the two runs on the wire bytes alone: a differing scratch
# path would leak into the transcript through the skills and environment
# preambles and make an otherwise exact reset look different.
run_mode() {
  local mode="$1"
  local home="$WORKDIR/codex-home"
  local requests="$WORKDIR/requests-$mode"
  mkdir -p "$home" "$requests"
  populate_memories "$home"

  local mock_pid=""
  python3 "$SCRIPT_DIR/mock_responses_server.py" \
    --script projection --requests-dir "$requests" \
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

  local -a bus_env=()
  case "$mode" in
    on)
      bus_env=(
        JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=1
        JEV_BUS_ADAPTER="$REPO_ROOT/jev/scripts/bus_boundary.py"
        JEV_BUS_PYTHON=python3
        JEV_BUS_STAGE_DEDUP="$STAGE_COMMAND"
      )
      ;;
    off)
      bus_env=(
        JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=0
        JEV_BUS_ADAPTER="$REPO_ROOT/jev/scripts/bus_boundary.py"
        JEV_BUS_PYTHON=python3
        JEV_BUS_STAGE_DEDUP="$STAGE_COMMAND"
      )
      ;;
    none) bus_env=() ;;
  esac

  local status=0
  (
    cd "$workspace"
    env -i PATH="$PATH" HOME="$WORKDIR/home" CODEX_HOME="$home" TERM=dumb NO_COLOR=1 \
      "${bus_env[@]}" \
      timeout "$TIMEOUT" "$CODEX_BIN" exec --skip-git-repo-check --json \
        -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
        "read the memory files" </dev/null > "$WORKDIR/exec-$mode-1.log" 2>&1
  ) || status=$?
  if [[ "$status" != 0 ]]; then
    printf 'error: the %s run exited %s; see %s\n' "$mode" "$status" \
      "$WORKDIR/exec-$mode-1.log" >&2
    kill -TERM "$mock_pid" 2>/dev/null || true
    return 1
  fi

  # The second user turn is what pushes the duplicate pair out of the current
  # user turn and out of the RECENT tail, which is the only shape the pinned
  # policy can prove. Without it no projection is possible at all.
  status=0
  (
    cd "$workspace"
    env -i PATH="$PATH" HOME="$WORKDIR/home" CODEX_HOME="$home" TERM=dumb NO_COLOR=1 \
      "${bus_env[@]}" \
      timeout "$TIMEOUT" "$CODEX_BIN" exec resume --last --skip-git-repo-check --json \
        -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
        "wrap up the review" </dev/null > "$WORKDIR/exec-$mode-2.log" 2>&1
  ) || status=$?
  kill -TERM "$mock_pid" 2>/dev/null || true
  wait "$mock_pid" 2>/dev/null || true
  if [[ "$status" != 0 ]]; then
    printf 'error: the %s resume run exited %s; see %s\n' "$mode" "$status" \
      "$WORKDIR/exec-$mode-2.log" >&2
    return 1
  fi

  # Snapshot the rollout so the next mode starts from an empty session store;
  # without this `resume --last` would continue the previous mode's session.
  mv "$home/sessions" "$WORKDIR/sessions-$mode"
}

run_mode on
run_mode off
run_mode none

set +e
python3 "$SCRIPT_DIR/check_read_tool_projection.py" \
  --on-dir "$WORKDIR/requests-on" \
  --off-dir "$WORKDIR/requests-off" \
  --none-dir "$WORKDIR/requests-none" \
  --on-sessions "$WORKDIR/sessions-on" \
  --off-sessions "$WORKDIR/sessions-off" \
  --repo-root "$REPO_ROOT" \
  --stage-command "$STAGE_COMMAND"
status=$?
set -e

if [[ "$status" != 0 ]]; then
  printf '\nscratch directory kept for inspection: %s\n' "$WORKDIR" >&2
elif [[ "$KEEP" == 1 ]]; then
  printf 'scratch directory: %s\n' "$WORKDIR"
else
  python3 - "$WORKDIR" <<'PY'
import shutil
import sys

shutil.rmtree(sys.argv[1], ignore_errors=True)
PY
fi
exit "$status"
