# Evidence: a launched host projects the outgoing request and resets exactly

Tracks [#70](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/70),
which is the third acceptance item of
[#5](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/5): a real
Codex run demonstrates reduced outgoing content and an exact reset. Companion to
the boundary description in [`BUS_BOUNDARY.md`](../BUS_BOUNDARY.md) and to the
transport runs recorded there.

Nothing here is a statement about model accuracy, safety, or performance. It
records what was actually executed, on which revision, and what was not
executed.

## Revisions

| Item | Value |
| --- | --- |
| Verified revision of this repository | `8ca45b6a7a2829ce61d23ea30ad2f64f5820187f` (`main`) |
| Binary exercised | `/home/agent/jev/work/lead/target/debug/codex`, sha256 `8da2941611b27c2963820573508d31170f8f2f8738476b17b5cce73e2101d1d6` |
| Toolchain | `rustc 1.95.0 (59807616e 2026-04-14)`, `cargo 1.95.0 (f2d3ce0bd 2026-03-21)`, from `codex-rs/rust-toolchain.toml` |
| Pinned component | `jev-prune-kit` at `2ecc8ff4e0976991c7abc09287d8f2f736d3164c` |
| Adapter under test | [`jev/scripts/bus_boundary.py`](../scripts/bus_boundary.py) (`apply`, resolved by the host) |
| Tier | `real-host-binary` |

The binary is a **debug** build, not the release artifact. It is built directly
from the verified revision: `main` already carries both ordered patches, so
`jev/scripts/verify-manifest.py --patch-state applied` reports `ok` on this tree
and no patch has to be applied to build it.

## What the run executes

`jev/smoke/run-projection-reset-smoke.sh` starts a **real Codex host process**
against a **loopback Responses-API mock**
([`jev/smoke/mock_projection_server.py`](../smoke/mock_projection_server.py))
inside an isolated `CODEX_HOME`. No provider is contacted and no credential is
read: the only reachable endpoint is the mock, and the generated config declares
a local `model_provider` with `request_max_retries = 0`.

All three cases share one `CODEX_HOME` and one workspace, so the only difference
between their recorded request bodies is the boundary itself:

1. [`seed_projection_rollout.py`](../smoke/seed_projection_rollout.py) writes a
   resumable session rollout whose transcript already carries an eligible
   duplicate-read pair — two `read` calls with identical arguments and an
   identical 655-byte result (`sha256 bb785d1d…`) at input indices 3 and 6,
   followed by filler turns that put the pair outside the current turn and
   outside the protected tail;
2. `codex exec resume <session> --all` appends the new user turn and sends the
   outgoing request, which the mock records byte for byte;
3. `check_projection_reset.py` reads the recorded bodies, the persisted
   rollouts, and (for the switch-on case) a standalone re-run of the same
   boundary, and prints a machine-readable verdict.

An explicit session id is required: `--last` resolves through the state database
and starts a new thread instead of loading the hand-written rollout.

## Results

| Case | `JEV_*` environment | Serialized `input` bytes | Input items | Omission marker | `projection_receipt` in the recorded body |
| --- | --- | --- | --- | --- | --- |
| `pristine` | none | 9753 | 35 | 0 | 0 |
| `off` | boundary configured, `JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=0` | 9753 | 35 | 0 | 0 |
| `on` | same, switch `=1`, one approved receipt in the store | 9178 | 35 | 1 | 0 |

`check_projection_reset.py` asserts, and this run satisfies, 13 checks:

| # | Assertion | Observed |
| --- | --- | --- |
| 1–3 | Every host run exits 0 | `pristine`, `off`, `on` all `0` |
| 4 | The item count is unchanged | `35 / 35 / 35` |
| 5 | Switch-on is strictly smaller | `9178 < 9753`, **575 bytes saved** |
| 6 | Switch-off is an exact reset | the `input` array is byte-identical to the unswitched control; only the host-minted `client_metadata.turn_id` differs, outside the boundary |
| 7 | Switch-on changes exactly the duplicate | `input[3].output` is the marker naming `call_jev_proj_witness`, everything else is unchanged |
| 8 | The marker appears only when switched on | `0 / 0 / 1` |
| 9 | No case persists a `projection_receipt` in a recorded body | `0 / 0 / 0` |
| 10 | The receipt exists only in the switched-on chain | applied `['jev-prune.dedup']`, one `projection_receipt`, `accepted: [3]`, `reverted: []` |
| 11–13 | No rollout is projected or receipted | all three rollouts keep the original read body (`sha256 bb785d1d…`, marker `0`, receipt `0`) |

The chain (a standalone re-run of the same adapter, stage, and store over the
recorded control request, since the host discards the adapter's own report)
records one `projection_receipt` at `stage: 100`, component `jev-prune-kit`,
`kind: "projection_receipt"`, and the dedup module's own note: one duplicate
substituted, **568 bytes** removed, policy `exact-read-repeat-v1`, model
`jev-1.13.0`, format `openai`. The 568 bytes the component reports and the 575
bytes the wire saved differ because the marker that replaces the body is itself
part of the outgoing request.

## Reproducibility

Two consecutive runs into the same scratch directory produce the same verdict
(`ok: true`, `9753 / 9753 / 9178`, 13/13). The fixture deletes each case's
outputs before it starts for that reason: while building this evidence a reused
`--workdir` was found to answer for the previous run, because a leftover
`mock.port` let the readiness loop break before the new mock bound and the
checker then read the earlier run's recorded requests. The fix is in this
change, and the failure it prevents (`host-exit` red while the byte comparisons
stayed green) is why the checker asserts the host exit status at all.

A *different* scratch root does move the absolute totals: the root leaks into
the request preamble three times, so a root longer by N characters shifts both
totals by exactly `3*N` while leaving the 575-byte reduction, the 35-item count,
and the exact reset unchanged. The `9753 / 9178` in the results table is this
run's own root (`/home/agent/jev/work/lead/verify/proj-final`); the
`9705 / 9130` in [`release-phase-claim.md`](release-phase-claim.md) and the
`9765 / 9190` in [`../VALIDATION.md`](../VALIDATION.md) are the same measurement
at shorter roots, not a drifting measurement.

## What this does not show

- **No live provider and no token metric.** The reduction is serialized request
  bytes on a fixture transcript served by a loopback mock. No token count is
  claimed here or anywhere else in `jev/`.
- **The duplicate pair is seeded, not discovered.** This revision exposes no
  `read`, `read_file`, or `file_read` tool, so a pair cannot enter the eligible
  set during a turn; the run proves the boundary on a transcript the host
  loaded, not that a host finds its own duplicate. That limit is stated in
  [`BUS_BOUNDARY.md`](../BUS_BOUNDARY.md).
- **Bytes only, never items.** The item count is unchanged in every case,
  because the merged host refuses a changed count (#55) and passes no approved
  view to the carrier. Reconciling the two rules is
  [#58](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/58).
- **One debug build on one platform.** The sha above is this machine's debug
  binary; no release artifact was rebuilt for this evidence.

## Reproduce

```sh
cd /home/agent/jev/work/lead/codex-jev
cargo build -p codex-cli --bin codex        # in codex-rs/, with the target dir and
                                            # OPENSSL_* environment recorded in ISOLATED_ENV.md
./jev/smoke/run-projection-reset-smoke.sh \
  --workdir /home/agent/jev/work/lead/verify/proj-final \
  --codex /home/agent/jev/work/lead/target/debug/codex --keep --timeout 120

# the fixture's own negative control; no Codex binary is needed
python3 jev/smoke/self_test_projection.py
```

Exit codes: `0` assertions passed, `1` an assertion failed, `2` usage error. A
failed run keeps its scratch directory so the recorded bodies can be inspected.
