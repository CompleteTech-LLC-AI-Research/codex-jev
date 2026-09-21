# Host-driven smokes

These fixtures drive a **real Codex host process** against a **loopback
Responses-API mock**, then assert what it actually put on the wire. Nothing here
contacts a provider: the only reachable endpoint is the mock, and no credential
is read.

Two fixtures live here, each at the `real-host-binary` tier:

- **Plaintext collaboration** — a parent/child collaboration turn, asserting the
  collaboration messages travelled as plaintext. It exists because the pinned
  patch's own unit tests prove the tool schema and the isolated profile proves
  the offline wiring, but neither produces an actual parent-to-child turn. Its
  results are recorded in
  [`../evidence/plaintext-pinned-build.md`](../evidence/plaintext-pinned-build.md).
- **Projection and exact reset** — a resumed session whose transcript carries a
  duplicate read pair, asserting the bus boundary shrinks the outgoing request,
  resets it exactly when switched off, and persists nothing. Its results are
  recorded in
  [`../evidence/projection-real-host.md`](../evidence/projection-real-host.md).

## Files

### Plaintext collaboration

| File | Purpose |
| --- | --- |
| `run-plaintext-smoke.sh` | End-to-end harness: starts the mock, runs `codex exec`, then runs the checker. |
| `mock_responses_server.py` | Minimal streaming Responses API mock that records every request body. |
| `check_plaintext.py` | Asserts the recorded request bodies and the persisted rollout are plaintext. |
| `self_test.py` | Runs the mock and checker against synthetic bodies, including the negative controls. |
| `collect_evidence.py` | Collapses per-binary verdicts into one machine-readable summary. |

### Projection and exact reset

| File | Purpose |
| --- | --- |
| `run-projection-reset-smoke.sh` | End-to-end harness: seeds one session, runs `pristine`, `off`, and `on` cases against the mock, then runs the checker. |
| `seed_projection_rollout.py` | Writes the resumable rollout that carries the eligible duplicate-read pair, and reports its body digest. |
| `mock_projection_server.py` | Loopback Responses-API mock that records every request body and its encoding. |
| `gen_projection_receipts.py` | Computes the pinned component's own proof for the seeded pair and writes its receipt store. |
| `check_projection_reset.py` | Asserts the reduction, the exact reset, the unchanged item count, and that no rollout is projected or receipted. |
| `self_test_projection.py` | Runs the checker against a faithful fixture and six mutations; no Codex binary is needed. |

## Run them

```sh
# against a built binary (defaults to $CARGO_TARGET_DIR/release/codex)
jev/smoke/run-plaintext-smoke.sh --codex /path/to/codex --keep
jev/smoke/run-projection-reset-smoke.sh --codex /path/to/codex --keep

# the fixtures' own tests; no Codex binary is needed
python3 jev/smoke/self_test.py
python3 jev/smoke/self_test_projection.py
```

Exit codes: `0` assertions passed, `1` an assertion failed, `2` usage error. A
failed run keeps its scratch directory so the recorded bodies can be inspected.

Both harnesses write a fresh scratch directory by default; set `TMPDIR` to a
filesystem with room if `/tmp` is small or full. A reused `--workdir` is safe:
each case deletes its own outputs before it starts, so a run can never answer
with a previous run's recorded requests or a stale mock port.

## How it is wired

- The fixture runs inside the repository-level Python gate:
  `.github/scripts/test_jev_smoke.py` executes `self_test.py` and
  `self_test_projection.py`, so a checker that stops discriminating fails CI
  rather than passing quietly.
- `self_test.py` asserts that the checker *rejects* an encrypted schema, a
  missing child turn, and a call that drops the `collaboration` namespace. A
  checker that cannot fail is not evidence.
- `self_test_projection.py` asserts the projection checker *rejects* six
  mutations, including a vanished reduction, an item-count change, a projected
  rollout, and a control that is not byte-identical.

## Requirements

Standard-library Python 3. The end-to-end harness additionally needs a built
`codex` binary and an ability to bind a loopback port; the mock never binds a
routable address. The self-test needs only loopback.

## Evidence tiers

| Tier | What it shows |
| --- | --- |
| `offline-fixture` | The mock answers deterministically, with no clock, randomness, or external network. |
| `real-host-binary` | A pinned Codex build actually reaches the mock: the plaintext fixture spawns a child agent, and the projection fixture sends a request the boundary can project, with the recorded bytes as the evidence. |
| live provider | Never used here. Remote inference stays disabled and unbudgeted. |

The plaintext fixture enables `multi_agent_v2` in its own throwaway `CODEX_HOME`
(the `isolated-offline` profile deliberately enables no Codex-native feature
flag), so it is the one place a real parent/child turn is exercised. The
projection fixture needs no feature flag, but it does need a seeded rollout:
this revision exposes no `read` tool, so a host cannot yet produce an eligible
duplicate pair on its own.
