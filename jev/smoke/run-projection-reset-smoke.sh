#!/usr/bin/env bash
#
# Drive a *real* Codex host binary through the jev-bus boundary and record what
# it actually sent, for the three states the projection contract distinguishes:
#
#   pristine  no JEV_* environment at all: nothing can project.
#   off       the boundary is configured but the dedup switch is explicitly 0.
#   on        the boundary is configured, the switch is 1, and an approved
#             receipt for the seeded duplicate-read pair is in the store.
#
# Each case resumes an identically seeded session against the same loopback
# mock, so the only difference between the recorded request bodies is the
# boundary. The checker then asserts the reduction, the exact reset, and that
# neither run persisted a projected body.
#
# This is a `real-host-binary` tier run: it starts a launched `codex` process
# and reads the bytes it put on the wire. It is not a token measurement and it
# contacts no live provider.
#
# Usage:
#   jev/smoke/run-projection-reset-smoke.sh [options]
#
# Options:
#   --codex PATH    codex binary to exercise (default: $CARGO_TARGET_DIR/debug/codex)
#   --kit DIR       pinned jev-prune-kit checkout
#   --workdir DIR   scratch directory (default: a fresh mktemp -d)
#   --profile NAME  profile identity used for the receipt store
#   --keep          keep the scratch directory and print it
#   --timeout SEC   wall-clock limit per host run (default: 120)
#
# Exit codes: 0 = assertions passed, 1 = an assertion failed, 2 = usage error.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CODEX_BIN="${CODEX_BIN:-${CARGO_TARGET_DIR:-$REPO_ROOT/codex-rs/target}/debug/codex}"
KIT_DIR="${JEV_PRUNE_KIT:-/home/agent/jev/checkouts/jev-prune-kit}"
WORKDIR=""
PROFILE="integrated-offline"
KEEP=0
TIMEOUT=120
SESSION="01a0c22b-0000-7000-8000-000000000070"
PROMPT="Please summarise the status of the seeded fixture project."
SOURCE_CALL="call_jev_proj_source"
WITNESS_CALL="call_jev_proj_witness"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex) CODEX_BIN="${2:-}"; shift 2 ;;
    --kit) KIT_DIR="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -x "$CODEX_BIN" ]] || { printf 'error: codex binary not found: %s\n' "$CODEX_BIN" >&2; exit 2; }
[[ -d "$KIT_DIR" ]] || { printf 'error: pinned kit not found: %s\n' "$KIT_DIR" >&2; exit 2; }
command -v python3 >/dev/null || { printf 'error: python3 is required\n' >&2; exit 2; }

WORKDIR="${WORKDIR:-$(mktemp -d -t jev-projection-smoke-XXXXXX)}"
mkdir -p "$WORKDIR"

# The host embeds its CODEX_HOME and cwd inside the instructions it sends, so
# both are shared across the three cases and only the seeded rollout is reset
# between runs. Otherwise the control and the switch-off run would differ by a
# path that has nothing to do with the boundary.
CODEX_HOME_ROOT="$WORKDIR/codex-home"
WORKSPACE="$WORKDIR/workspace"
mkdir -p "$CODEX_HOME_ROOT" "$WORKSPACE" "$WORKDIR/home"

mock_pid=""
case_dir=""

stop_mock() {
  if [[ -n "$mock_pid" ]] && kill -0 "$mock_pid" 2>/dev/null; then
    kill -TERM "$mock_pid" 2>/dev/null || true
    wait "$mock_pid" 2>/dev/null || true
  fi
  mock_pid=""
}

cleanup() {
  stop_mock
  if [[ "$KEEP" -eq 0 ]]; then
    python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$WORKDIR"
  fi
}
trap cleanup EXIT

# run_case <name> <pristine|off|on> [receipt-source-request]
run_case() {
  local name="$1" mode="$2" receipt_source="${3:-}"
  case_dir="$WORKDIR/$name"
  local requests="$case_dir/requests"
  local codex_home="$CODEX_HOME_ROOT"
  local workspace="$WORKSPACE"
  # A reused --workdir must not be able to answer for this run: a stale
  # `mock.port` would let the readiness loop below break before the mock binds,
  # and stale recorded requests would be read as if this run had produced them.
  python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$case_dir"
  mkdir -p "$requests"

  # Reset the seeded transcript so every case resumes exactly the same session.
  python3 "$SCRIPT_DIR/seed_projection_rollout.py" \
    --codex-home "$codex_home" --workspace "$workspace" --session "$SESSION" \
    --provider jev_projection_mock --resume-prompt "$PROMPT" \
    --manifest "$case_dir/seed.json" >/dev/null

  python3 "$SCRIPT_DIR/mock_projection_server.py" \
    --requests-dir "$requests" --port-file "$case_dir/mock.port" \
    >"$case_dir/mock.log" 2>&1 &
  mock_pid=$!
  for _ in $(seq 1 100); do
    [[ -s "$case_dir/mock.port" ]] && break
    sleep 0.1
  done
  [[ -s "$case_dir/mock.port" ]] || { echo "error: mock did not report a port" >&2; cat "$case_dir/mock.log" >&2; exit 2; }
  local port
  port="$(cat "$case_dir/mock.port")"

  cat >"$codex_home/config.toml" <<EOF
# Fixture configuration for jev/smoke/run-projection-reset-smoke.sh. The only
# reachable endpoint is the loopback mock; no live provider is contacted.
model = "jev-projection-model"
model_provider = "jev_projection_mock"
approval_policy = "never"

[model_providers.jev_projection_mock]
name = "JEV projection mock"
base_url = "http://127.0.0.1:$port/v1"
wire_api = "responses"
request_max_retries = 0
EOF

  local -a jev_env=()
  if [[ "$mode" != "pristine" ]]; then
    local switch=0
    [[ "$mode" == "on" ]] && switch=1
    local state_dir="$case_dir/state"
    mkdir -p "$state_dir"
    if [[ "$mode" == "on" ]]; then
      python3 "$SCRIPT_DIR/gen_projection_receipts.py" \
        --request "$receipt_source" --session "$SESSION" --profile "$PROFILE" \
        --state-dir "$state_dir" --repo "$REPO_ROOT" --kit "$KIT_DIR" \
        --source-call "$SOURCE_CALL" --witness-call "$WITNESS_CALL" --json \
        >"$case_dir/receipts.json"
    fi
    jev_env+=(
      "JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=$switch"
      "JEV_BUS_ADAPTER=$REPO_ROOT/jev/scripts/bus_boundary.py"
      "JEV_BUS_PYTHON=python3"
      "JEV_BUS_WORKSPACE=$workspace"
      "JEV_BUS_STAGE_DEDUP=python3 $KIT_DIR/runner.py --bus-stage --state-dir $state_dir --profile $PROFILE --format openai"
    )
  fi

  local status=0
  set +e
  (
    cd "$workspace"
    env -i \
      PATH="$PATH" HOME="$WORKDIR/home" CODEX_HOME="$codex_home" TERM=dumb NO_COLOR=1 \
      "${jev_env[@]}" \
      timeout "$TIMEOUT" "$CODEX_BIN" exec resume "$SESSION" --all \
        --skip-git-repo-check --json \
        -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
        "$PROMPT" </dev/null >"$case_dir/host.jsonl" 2>&1
  )
  status=$?
  set -e
  stop_mock
  cp "$(python3 -c 'import glob,sys; print(glob.glob(sys.argv[1]+"/sessions/**/rollout-*.jsonl", recursive=True)[0])' "$codex_home")" \
    "$case_dir/rollout.jsonl"
  printf '%s\n' "$status" >"$case_dir/exit-status"

  # The host discards the adapter's report, so re-run the same boundary over the
  # recorded control request to capture the receipt the switch-on chain emits.
  if [[ "$mode" == "on" ]]; then
    JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=1 python3 "$REPO_ROOT/jev/scripts/bus_boundary.py" apply \
      --request "$receipt_source" --json --session "$SESSION" --turn evidence --workspace "$workspace" \
      --stage "dedup=python3 $KIT_DIR/runner.py --bus-stage --state-dir $state_dir --profile $PROFILE --format openai" \
      >"$case_dir/chain.json" 2>"$case_dir/chain.err" || true
  fi
  echo "== case $name ($mode): exit status $status =="
  tail -n 3 "$case_dir/host.jsonl" || true
}

run_case pristine pristine
run_case off off
run_case on on "$WORKDIR/pristine/requests/request-00.json"

checker_status=0
python3 "$SCRIPT_DIR/check_projection_reset.py" \
  --workdir "$WORKDIR" \
  --pristine "$WORKDIR/pristine" \
  --off "$WORKDIR/off" \
  --on "$WORKDIR/on" \
  --source-call "$SOURCE_CALL" --witness-call "$WITNESS_CALL" \
  --json >"$WORKDIR/verdict.json" || checker_status=$?
cat "$WORKDIR/verdict.json"

if [[ "$checker_status" -ne 0 && "$KEEP" -eq 0 ]]; then
  KEEP=1
  echo "assertions failed; keeping scratch directory: $WORKDIR" >&2
fi
if [[ "$KEEP" -eq 1 ]]; then
  echo "scratch directory: $WORKDIR"
fi
exit "$checker_status"
