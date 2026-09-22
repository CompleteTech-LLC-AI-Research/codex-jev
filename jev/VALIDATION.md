# Validation: offline, real-host, and live-provider coverage

Phase 6.2 asks for reproducible evidence that distinguishes **offline**,
**real-host**, and **live-provider** coverage, and for a baseline-vs-integrated
performance measurement that reports correctness, payload bytes, latency,
fallback rates, failures, and service usage without dropping unsuccessful cases.

This record keeps those three tiers separate. Nothing here is a statement about
model accuracy, safety, or speed.

| Tier                                                    | Status      | Where                                                                                                                |
| ------------------------------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------- |
| `offline-fixture` / `bus-stage-stub` / `component-stub` | **Run**     | [`scripts/e2e_regression.py`](scripts/e2e_regression.py), [`scripts/perf_validation.py`](scripts/perf_validation.py) |
| `real-host-binary`                                      | **Run**     | [`smoke/`](smoke/README.md); results below                                                                           |
| live provider                                           | **Not run** | Blocked on explicit consent and a positive budget                                                                    |

## Revisions

| Item                                 | Value                                                                                                                     |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Verified revision of this repository | `177f57110a19a11a8fbf28399db6cfc3d54021c9` (merge of #89)                                                                 |
| Binary exercised (real-host)         | `/home/agent/jev/work/lead/target/debug/codex`, sha256 `6d51fdc9278d1a2fbc1bb01e024ae12d5d9a3b233d7229a7f6e792eaf35c0c3b` |
| Toolchain                            | Rust `1.95.0` (`codex-rs/rust-toolchain.toml`)                                                                            |
| Host patch base                      | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180`                                                                                |
| `codex-plaintext-collab`             | `7bf9202513a59362171b6687563580c9b02ec203`                                                                                |
| `jev-context-fabric`                 | `5079099211c0d39a6ead347633a99d675a210c64`                                                                                |
| `jev-prune-kit`                      | `2ecc8ff4e0976991c7abc09287d8f2f736d3164c`                                                                                |
| `jev-sentinel`                       | `4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a`                                                                                |
| `jev-codex-approval`                 | `0b931ee24a907c9dc47dc1828a94a6afcd7775b6`                                                                                |

The binary is a **debug** build; it is not the release artifact. `verify-manifest.py
--patch-state applied` reports `ok` on this tree.

## Offline: composed behavior and integration performance

`scripts/e2e_regression.py` drives the composed lifecycle over checked-in
fixtures and reports **55/55** assertions (see [`END_TO_END.md`](END_TO_END.md)).

`scripts/perf_validation.py` measures what the integration changes on the wire —
baseline (untouched) versus integrated (post-boundary) — with a byte count as the
measurement and a byte-derived token estimate reported **separately** from it:

```sh
python3 jev/scripts/perf_validation.py run --repetitions 5 --json /tmp/perf.json
```

| Case                                  | Tier              | Result                                       |
| ------------------------------------- | ----------------- | -------------------------------------------- |
| `integrated_dedup_and_view`           | `bus-stage-stub`  | 5507 → 4215 bytes; items 27 → 25             |
| `dedup_only_replaces_the_body`        | `bus-stage-stub`  | 5507 → 4555 bytes; item count unchanged      |
| `disabled_switches_leave_bytes_alone` | `offline-fixture` | byte-identical (nothing invoked)             |
| `cancelled_turn_removes_nothing`      | `offline-fixture` | byte-identical to the post-dedup input       |
| `protected_tail_is_never_replaced`    | `bus-stage-stub`  | byte-identical                               |
| `stale_view_is_refused`               | `offline-fixture` | byte-identical; refused `stale_view`         |
| `unapproved_removal_is_reverted`      | `offline-fixture` | byte-identical; refused `unapproved_removal` |
| `view_application_requires_approval`  | `offline-fixture` | refused `view_not_approved`                  |

- **Payload bytes:** baseline 5507, integrated 4215, removed 1292 (ratio 0.765).
- **Tokens:** `tokens_measured` is `null`; `tokens_estimated` is a byte-derived
  estimate (`before 1376`, `after 1053`), never a measurement.
- **Latency:** the boundary call is sampled (`--repetitions`) and reported as
  min/median/max with `source: observed`. This is local boundary wall-clock, not
  model latency, and it is excluded from the deterministic digest.
- **Fallback rate:** 5 of 8 cases fall back and are byte-identical; each is named.
- **Failures:** 3 refusals are counted and named by code; none is dropped, and
  `failed` is `0`.
- **Service usage:** `provider_requests` is `0` and `budget_spent_usd` is `0.0`.

Two runs produce the same `deterministic_digest` (the latency samples are the
only field that varies).

## Real-host: a launched binary against a loopback mock

Both host harnesses run a **real Codex binary** against a **loopback
Responses-API mock** inside a throwaway `CODEX_HOME`. No provider is contacted
and no credential is read.

```sh
jev/smoke/run-plaintext-smoke.sh --codex /path/to/codex
jev/smoke/run-projection-reset-smoke.sh --codex /path/to/codex
```

| Harness                    | Result                                                                                                                                                                                                     |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Plaintext collaboration    | `ok: true`; 3 recorded requests; turns `parent_initial`, `child`, `parent_after_spawn`; a `collaboration.spawn_agent` call is present and the persisted rollout saw the plaintext call.                    |
| Projection and exact reset | `ok: true`; serialized `input` bytes pristine/off `9765` vs on `9190` (575-byte reduction, ratio 0.941); every persisted rollout carries **no** marker and **no** receipt, so the transcript is untouched. |

This is the only tier that exercises a genuine parent→child turn and a real
outgoing request; it is not a model-performance measurement.

The absolute byte totals above are not intrinsic to the smoke: the host embeds
the scratch root three times in the request preamble, so a `--workdir` whose
path is longer by N characters shifts both totals by exactly `3*N`. This record
used a 47-character root; the same binary, seed, and harness report
`9705 / 9130` at a 27-character root. The path-independent claims are the
575-byte reduction, the ratio, the unchanged 35-item count, and the
byte-identical switch-off control, which is what
[`check_projection_reset.py`](smoke/check_projection_reset.py) asserts.
[`evidence/projection-read-tool-host-run.md`](evidence/projection-read-tool-host-run.md)
states the same property from its own three-run spread.

## Live provider: not run

No live inference was run, here or implicitly. The remote-inference switch keeps
its declared `false` default, and the manifest refuses a profile that enables
remote inference without an explicit budget (`E_REMOTE_INFERENCE_UNAUTHORIZED`).

**Blocker:** paid inference is outside this authorization and requires explicit
consent and a positive budget. Until that is provided, the live-provider tier is
deliberately **empty** in every document this phase produces (`tier_labels.live_provider`
is `[]` in both the composed trace and the performance document), and the token
figure is a byte-derived estimate rather than a counted value.

## What is not proven

- No model accuracy, safety, or speed claim is made, and no measured token count
  is reported.
- Only `linux-x86_64` was exercised here; `macos-aarch64` and `windows-x86_64`
  are the other supported platforms and are **not** validated by this run.
- The projection harness resumes a seeded rollout; this revision exposes no
  `read` tool, so no run yet shows a host _discovering_ its own eligible
  duplicate pair.
- Stage 200 (the Fabric prose view) has no component bus-stage entry point, so the
  performance harness applies the shipped host-side `fabric_views` code rather
  than a component subprocess.
