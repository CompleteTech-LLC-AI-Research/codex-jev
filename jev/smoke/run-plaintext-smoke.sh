#!/usr/bin/env bash
#
# Drive the built Codex host through a real parent/child collaboration turn
# against a local mock Responses API, then assert the collaboration messages
# travelled as plaintext.
#
# Usage:
#   jev/smoke/run-plaintext-smoke.sh [options]
#
# Options:
#   --codex PATH     codex binary to exercise (default: $CARGO_TARGET_DIR/release/codex)
#   --workdir DIR    scratch directory (default: a fresh mktemp -d)
#   --keep           keep the scratch directory and print it
#   --timeout SEC    wall-clock limit for the host run (default: 180)
#   --rendezvous SEC how long the parent's post-spawn turn waits for the child
#                    turn before giving up (default: 20; 0 disables the wait)
#
# Exit codes: 0 = assertions passed, 1 = an assertion failed, 2 = usage error.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CODEX_BIN="${CODEX_BIN:-${CARGO_TARGET_DIR:-$REPO_ROOT/codex-rs/target}/release/codex}"
WORKDIR=""
KEEP=0
TIMEOUT=180
RENDEZVOUS=20

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex) CODEX_BIN="${2:-}"; shift 2 ;;
    --workdir) WORKDIR="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --rendezvous) RENDEZVOUS="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -x "$CODEX_BIN" ]] || { printf 'error: codex binary not found: %s\n' "$CODEX_BIN" >&2; exit 2; }

WORKDIR="${WORKDIR:-$(mktemp -d -t jev-plaintext-smoke-XXXXXX)}"
mkdir -p "$WORKDIR"
codex_home="$WORKDIR/codex-home"
requests_dir="$WORKDIR/requests"
sessions_dir="$codex_home/sessions"
workspace="$WORKDIR/workspace"
mkdir -p "$codex_home" "$requests_dir" "$workspace"

mock_log="$WORKDIR/mock.log"
host_log="$WORKDIR/codex-exec.log"
mock_pid=""

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

# The mock binds an ephemeral port and reports it on stdout.
python3 "$REPO_ROOT/jev/smoke/mock_responses_server.py" \
  --requests-dir "$requests_dir" --port-file "$WORKDIR/mock.port" \
  --child-rendezvous-seconds "$RENDEZVOUS" \
  >"$mock_log" 2>&1 &
mock_pid=$!

for _ in $(seq 1 100); do
  [[ -s "$WORKDIR/mock.port" ]] && break
  sleep 0.1
done
[[ -s "$WORKDIR/mock.port" ]] || { echo "error: mock server did not report a port" >&2; cat "$mock_log" >&2; exit 2; }
mock_port="$(cat "$WORKDIR/mock.port")"

cat >"$codex_home/config.toml" <<EOF
# Fixture configuration for jev/smoke/run-plaintext-smoke.sh. No live provider is
# contacted: the only reachable endpoint is the loopback mock.
model = "jev-smoke-model"
model_provider = "jev_smoke_mock"
approval_policy = "never"

[model_providers.jev_smoke_mock]
name = "JEV plaintext smoke mock"
base_url = "http://127.0.0.1:$mock_port/v1"
wire_api = "responses"
request_max_retries = 0

[features]
multi_agent_v2 = true
EOF

set +e
(
  cd "$workspace"
  env -i \
    PATH="$PATH" \
    HOME="$WORKDIR/home" \
    CODEX_HOME="$codex_home" \
    TERM=dumb \
    NO_COLOR=1 \
    timeout "$TIMEOUT" "$CODEX_BIN" exec \
      --skip-git-repo-check \
      --json \
      -c 'sandbox_mode="read-only"' \
      -c 'approval_policy="never"' \
      "please delegate the fixture task to a sub-agent" \
      </dev/null \
      >"$host_log" 2>&1
) 
host_status=$?
set -e

# Stop the mock first: it writes turns.json while shutting down.
stop_mock

echo "== codex exec exit status: $host_status =="
tail -n 20 "$host_log" || true

python3 "$REPO_ROOT/jev/smoke/check_plaintext.py" \
  --requests-dir "$requests_dir" \
  --session-dir "$sessions_dir" \
  --json >"$WORKDIR/verdict.json" || checker_status=$?
checker_status="${checker_status:-0}"
cat "$WORKDIR/verdict.json"

if [[ "$checker_status" -ne 0 && "$KEEP" -eq 0 ]]; then
  KEEP=1
  echo "assertions failed; keeping scratch directory: $WORKDIR" >&2
fi
if [[ "$KEEP" -eq 1 ]]; then
  echo "scratch directory: $WORKDIR"
fi
exit "$checker_status"
