# Host-driven plaintext smoke

These fixtures drive a **real Codex host process** through a parent/child
collaboration turn against a **loopback Responses-API mock**, then assert that
the collaboration messages travelled as plaintext. Nothing here contacts a
provider: the only reachable endpoint is the mock, and no credential is read.

They exist because the pinned patch's own unit tests prove the tool schema, and
the isolated profile proves the offline wiring, but neither produces an actual
parent-to-child turn. This harness closes that gap at the
`real-host-binary` tier and is the reproducible form of the results recorded in
[`../evidence/plaintext-pinned-build.md`](../evidence/plaintext-pinned-build.md).

## Files

| File | Purpose |
| --- | --- |
| `run-plaintext-smoke.sh` | End-to-end harness: starts the mock, runs `codex exec`, then runs the checker. |
| `mock_responses_server.py` | Minimal streaming Responses API mock that records every request body. |
| `check_plaintext.py` | Asserts the recorded request bodies and the persisted rollout are plaintext. |
| `self_test.py` | Runs the mock and checker against synthetic bodies, including the negative controls. |
| `collect_evidence.py` | Collapses per-binary verdicts into one machine-readable summary. |

## Run it

```sh
# against a built binary (defaults to $CARGO_TARGET_DIR/release/codex)
jev/smoke/run-plaintext-smoke.sh --codex /path/to/codex --keep

# the fixture's own tests; no Codex binary is needed
python3 jev/smoke/self_test.py
```

Exit codes: `0` assertions passed, `1` an assertion failed, `2` usage error. A
failed run keeps its scratch directory so the recorded bodies can be inspected.

`run-plaintext-smoke.sh` writes a fresh scratch directory by default; set
`TMPDIR` to a filesystem with room if `/tmp` is small or full.

## How it is wired

- The fixture runs inside the repository-level Python gate:
  `.github/scripts/test_jev_smoke.py` executes `self_test.py`, so a checker that
  stops discriminating fails CI rather than passing quietly.
- `self_test.py` asserts that the checker *rejects* an encrypted schema, a
  missing child turn, and a call that drops the `collaboration` namespace. A
  checker that cannot fail is not evidence.

## Requirements

Standard-library Python 3. The end-to-end harness additionally needs a built
`codex` binary and an ability to bind a loopback port; the mock never binds a
routable address. The self-test needs only loopback.

## Evidence tiers

| Tier | What it shows |
| --- | --- |
| `offline-fixture` | The mock answers deterministically, with no clock, randomness, or external network. |
| `real-host-binary` | A pinned Codex build actually reaches the mock and spawns a child agent. |
| live provider | Never used here. Remote inference stays disabled and unbudgeted. |

The fixture enables `multi_agent_v2` in its own throwaway `CODEX_HOME` (the
`isolated-offline` profile deliberately enables no Codex-native feature flag),
so it is the one place a real parent/child turn is exercised.
