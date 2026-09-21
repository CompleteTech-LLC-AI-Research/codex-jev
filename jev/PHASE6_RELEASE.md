# Phase 6/6: validate and release the combined stack

Phase 6/6 asks for one proof that the integrated lifecycle is **validated and
released**: an offline correlated trace that exercises every stage with the
authority boundaries preserved, reproducible evidence that keeps offline,
real-host, and live-provider coverage distinct, and a fresh isolated setup that
reproduces the validated configuration and rolls back - with release readiness
following that evidence rather than issue completion.

The three sub-issues supply the parts: [`END_TO_END.md`](END_TO_END.md) (6.1,
the composed regression), [`VALIDATION.md`](VALIDATION.md) (6.2, the tier and
performance record), and [`RELEASE.md`](RELEASE.md) (6.3, the packaging,
upgrade, and rollback workflow). This record is the **composition** of the
three, produced by one harness:

```sh
python3 jev/scripts/phase6_release.py run --root . --json /tmp/phase6.json
```

The harness composes the shipped modules (`e2e_regression`, `perf_validation`,
`release_readiness`) rather than re-implementing them, and adds only the
composition and its honesty rules. Everything it runs is offline: checked-in
fixtures, local subprocesses, and scratch outside the repository. No provider is
contacted and no credential is read.

## Criterion 1 - a correlated offline trace with preserved authority

The composed regression runs the declared lifecycle over deterministic inputs
and reports **55/55** assertions across the eight stages, in the promised order:

| Stage               | Scenario tier     | Assertions |
| ------------------- | ----------------- | ---------- |
| `capture`           | `rollout-fixture` | 4          |
| `retrieval`         | `rollout-fixture` | 5          |
| `screening`         | `rollout-fixture` | 6          |
| `projection`        | `bus-stage-stub`  | 15         |
| `collab`            | `rollout-fixture` | 4          |
| `sentinel_and_veto` | `component-stub`  | 8          |
| `approval`          | `offline-fixture` | 7          |
| `execution`         | `offline-fixture` | 4          |
| (fixture labelling) | cross-cutting     | 2          |

The stage partition is asserted **lossless**: the stage assertions plus the two
cross-cutting fixture-labelling assertions equal the trace's own check count, so
a new stage cannot be added without a home. The trace digest is
`b727323e13b749a70dbdd0bac29bbc0d2031bfa2582e83fa2c03aac7c93785bf`.

Authority is checked by exact assertion id, not by a summary bit:

| Invariant                                      | Proved by                                                          |
| ---------------------------------------------- | ------------------------------------------------------------------ |
| stages run in the declared order               | `execution.the_composed_lifecycle_runs_in_the_declared_order`      |
| the canonical request is never mutated         | `projection.the_canonical_request_is_never_mutated`                |
| an unapproved removal is reverted, not shipped | `projection.an_unapproved_removal_is_reverted_rather_than_shipped` |
| quarantine is journaled before it is honored   | `screening.quarantine_is_journaled_before_it_is_honored`           |
| clearing a latch needs explicit confirmation   | `veto.clearing_a_latch_requires_explicit_confirmation`             |
| a later allowance cannot clear a latched veto  | `approval.a_later_allowance_cannot_clear_a_latched_veto`           |
| the approval gate never flips a switch         | `approval.the_gate_reads_readiness_and_never_flips_a_switch`       |
| no assertion claims a host/provider run        | `execution.no_assertion_claims_a_host_or_provider_run`             |
| no check claims a measured token count         | `execution.no_check_claims_a_measured_token_count`                 |

Fixtures are distinguished from live evidence by construction: every assertion
carries a tier from `{rollout-fixture, offline-fixture, bus-stage-stub,
component-stub}`, and the trace's `real_host` and `live_provider` label lists are
empty. This is offline regression evidence, never a statement about model
accuracy, safety, or speed.

## Criterion 2 - coverage that keeps the three tiers apart

| Tier                                                                        | Status      | Where                                                                                  |
| --------------------------------------------------------------------------- | ----------- | -------------------------------------------------------------------------------------- |
| `offline-fixture` / `bus-stage-stub` / `component-stub` / `rollout-fixture` | **Run**     | `scripts/phase6_release.py`, `scripts/e2e_regression.py`, `scripts/perf_validation.py` |
| `real-host-binary`                                                          | **Run**     | `smoke/` results in [`evidence/platform-matrix.json`](evidence/platform-matrix.json)   |
| live provider                                                               | **Not run** | refused by the gate; paid inference needs explicit consent and a budget                |

The composition derives coverage from evidence and refuses an overclaim: a
live-provider label, a non-zero `provider_requests`, a non-zero budget, a
measured token claim, or a live-claiming platform record each becomes a named
overclaim that fails the tier review. On the recorded revision the measurement
reports `provider_requests = 0`, `budget_spent_usd = 0.0`, and the tier lists for
`real_host`/`live_provider` are empty, so no live test can run implicitly - and
none runs in an existing user session, because every host run happens in a
throwaway isolated `CODEX_HOME`.

The offline measurement is baseline-vs-integrated on the wire: 5507 -> 4215
bytes (removed 1292, ratio 0.765), 8 of 8 cases pass, 3 refusals named, 5
fallbacks byte-identical, `tokens_measured` is `null` and the token figure is a
byte-derived estimate. Its deterministic digest is
`552f446ad5376437289881923bbf9a18c538c7b71d912a2bdbd822cc91d0a21f`.

## Criterion 3 - a fresh isolated setup reproduces and rolls back

The isolated round trip creates the safe profile (`isolated-offline`) in a
throwaway environment, moves state, and rolls back: both runs report state
`moved`, the source absent, the plan preserved, the binary matching the plan, no
optional features enabled, and remote inference off. Re-running the gate on a
fresh checkout reproduces the same record.

Release readiness then **follows the evidence**, not issue completion: the gate
list has 10 entries, `failed = 0`, and `not_run = [platform.matrix]`, so
`release_ready` is **false** with `blocking = macos-aarch64, platform.matrix,
windows-x86_64`. The release candidate is built from the same tree
(`candidate_bundle_digest =
53fcab9efc76c4b67f199aa3a232472dcc4ca88a69e639d4f5ec83d26e9a5188`) and excludes
the declared synthetic fixtures and any credential-shaped file.

`phase_validated` is reported **separately** from `release_ready` so the two are
never confused: this host validates the lifecycle while release readiness stays
false because two supported platforms have no run. That is the intended outcome,
not a suppressed failure.

## Recorded result

On revision `96fd0bfcf2589c8acd1e592246b5eb570610fcb2`:

```
phase_validated=true trace=true tiers=true rollback=true readiness=true release_ready=false revision=96fd0bfcf2589c8acd1e592246b5eb570610fcb2 blocking=macos-aarch64,platform.matrix,windows-x86_64
```

## Known limitations

- `macos-aarch64` and `windows-x86_64` are supported by the pin and **not
  validated**; `release_ready` stays `false` for them, by design.
- The recorded real-host runs use a **debug** binary, not a release artifact.
- No `live-provider` tier was run; the token figure is an estimate, never a
  measurement.
- Stage 200 (the Fabric prose view) has no component bus-stage entry point, so
  the projection stage is driven through the host-side `fabric_views` code.
- Repository-wide CI is red for pre-existing, unrelated reasons; the lane this
  harness relies on is `repo-checks / build-test`.
