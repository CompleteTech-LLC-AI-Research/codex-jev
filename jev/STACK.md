# The combined stack record: compatibility, validation, and operations

This is the phase-6 roll-up (#8). The three phase-6 artifacts answer separate
questions - [`END_TO_END.md`](END_TO_END.md) proves the composed lifecycle runs,
[`VALIDATION.md`](VALIDATION.md) records what was measured at which tier, and
[`RELEASE.md`](RELEASE.md) packages the gates and the candidate artifact. This
record is what a reviewer reads to see that the three agree on **one revision**.

Nothing here is a new claim: the document is produced by composing the merged
harnesses, and a refusal is a failure rather than a footnote.

```sh
python3 jev/scripts/stack_validation.py run --json /tmp/stack.json
```

The command runs the composed regression (#24), the offline performance
comparison (#25), and the release gates (#26) in a scratch directory outside the
repository, and it refuses the record when a lifecycle stage has no passing
assertion, when an authority assertion is missing or failing, when a check claims
the `live-provider` tier, or when the isolated round trip does not reproduce and
roll back.

## The validated combination

Composed on host revision `4bac04bdc` with the pins below, which are the manifest
revisions and the host base commit recorded in
[`compatibility-manifest.json`](compatibility-manifest.json).

| Component                            | Kind          | Revision                                   | Interface version       |
| ------------------------------------ | ------------- | ------------------------------------------ | ----------------------- |
| `codex-jev` (this host, base commit) | host          | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` | envelopes, correlation  |
| `codex-plaintext-collab`             | patch         | `7bf9202513a59362171b6687563580c9b02ec203` | plaintext collaboration |
| `jev-context-fabric`                 | python        | `5079099211c0d39a6ead347633a99d675a210c64` | `memory_tools` v1       |
| `jev-prune-kit`                      | python        | `2ecc8ff4e0976991c7abc09287d8f2f736d3164c` | `jev_bus` v1            |
| `jev-sentinel`                       | python        | `4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a` | `sentinel_boundary` v1  |
| `jev-codex-approval`                 | python-native | `0b931ee24a907c9dc47dc1828a94a6afcd7775b6` | `approval_preflight` v1 |

What "compatible" means here, and what the gates re-derive rather than assume:

- the manifest, its patch digests, and every declared profile validate with no
  error code (`manifest.pins`);
- the ordered patches and the native approval adapter are applied at the declared
  anchors, **and** the removal direction reports the integrated tree as still
  patched, so a half-removed port cannot look clean
  (`host.patch.applied`, `host.patch.rollback-detected`, `host.adapter.applied`,
  `host.adapter.rollback-detected`);
- approval enforcement is declared but off by default, and remote inference has
  no consent, no budget, and a `false` default in the isolated profile
  (`approval.enforcement.disabled`, `remote_inference.disabled`);
- a fresh isolated environment resolves to the same plan digest twice and rolls
  back to a preserved record without touching the ambient Codex home
  (`isolated.roundtrip`).

Profiles: `baseline`, `integrated-offline` (the supported profile),
`isolated-offline` (the safe profile), and `enforcement-eval`. OmniRoute is
excluded by the manifest and the validator fails closed if it appears.

## The composed lifecycle, stage by stage

Every stage carries at least one passing assertion from the composed trace; a
missing stage refuses the record. Counts below are the composed run's own.

| Stage               | Assertions | Tiers exercised                      |
| ------------------- | ---------- | ------------------------------------ |
| `capture`           | 4          | `offline-fixture`, `rollout-fixture` |
| `retrieval`         | 5          | `offline-fixture`, `rollout-fixture` |
| `screening`         | 6          | `offline-fixture`, `rollout-fixture` |
| `projection`        | 15         | `bus-stage-stub`, `offline-fixture`  |
| `collab`            | 4          | `offline-fixture`, `rollout-fixture` |
| `sentinel_and_veto` | 8          | `component-stub`, `offline-fixture`  |
| `approval`          | 7          | `component-stub`, `offline-fixture`  |
| `execution`         | 4          | `offline-fixture`                    |

53 of the composed regression's 55 assertions are stage assertions; the other two
are the harness's own negative and bound checks. The trace digest is
`b727323e13b749a70dbdd0bac29bbc0d2031bfa2582e83fa2c03aac7c93785bf` for
55/55 passing.

## Preserved authority, by assertion

The invariants are resolved against named assertions rather than restated in
prose, so a reviewer can check the mapping against a trace dump.

| Invariant                                                                      | Assertions                                                                                                                                                                                                                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Recalled content and collaboration messages stay evidence, never authorization | `retrieval.note_marks_recalled_content_as_evidence_only`, `screening.injection_block_is_marked_as_evidence`, `collab.recalled_collaboration_is_marked_untrusted_evidence`, `retrieval.remote_enrichment_is_refused_without_consent_and_budget`          |
| A Sentinel veto latches, and a later approval cannot clear it                  | `veto.an_enforced_finding_vetoes_the_ingress_event_and_latches`, `veto.an_ordinary_tool_call_inherits_the_session_latch`, `approval.a_later_allowance_cannot_clear_a_latched_veto`                                                                      |
| Approval preflight applies only to an eligible, explicitly approved change     | `projection.view_application_requires_explicit_approval`, `projection.an_unapproved_removal_is_reverted_rather_than_shipped`, `approval.enforcement_is_off_under_the_default_environment`, `approval.the_gate_reads_readiness_and_never_flips_a_switch` |
| Stale, cancelled, and out-of-scope input is refused rather than executed       | `projection.a_stale_view_is_refused_rather_than_applied`, `projection.a_cancelled_turn_removes_nothing`, `retrieval.hydration_refuses_to_cross_workspaces`                                                                                              |

The host keeps what it never delegates: authorization, sandbox enforcement,
cancellation, compaction, and final execution. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the boundary ownership and
[`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) and
[`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md) for the veto paths.

## Tier coverage, kept apart

The census counts every assertion in the composed record by tier. A check that
claims the `live-provider` tier refuses the whole record, and the row below is
therefore structural, not editorial.

| Tier               | Assertions                                                       | What it proves                                                         | What it does not prove                                   |
| ------------------ | ---------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------- |
| `offline-fixture`  | 47                                                               | composed behavior over checked-in inputs and shipped modules           | nothing about a launched process or a model              |
| `rollout-fixture`  | 10                                                               | replay and re-proof against a checked-in rollout                       | nothing about live provider behavior                     |
| `bus-stage-stub`   | 7                                                                | the bus boundary's ordering and byte accounting                        | nothing about component policy                           |
| `component-stub`   | 8                                                                | the host's veto/latch/approval plumbing over the wired protocol        | not the component's detection semantics                  |
| `real-component`   | 0 in this run (pinned checkout resolved? run with `--component`) | the same steps against the pinned `jev-sentinel` checkout              | still no provider                                        |
| `real-host-binary` | 1 gate (recorded run) + both host smokes                         | bytes a real Codex binary sent to a loopback mock, and the exact reset | model quality, safety, or latency of a provider          |
| `live-provider`    | **not run**                                                      | nothing                                                                | everything: paid inference is outside this authorization |

`live-provider` stays `not-run` unless separately authorized with explicit consent
and a positive budget, and no existing user session is ever used for it.

## The measured numbers

From the same composed run (`perf` digest
`552f446ad5376437289881923bbf9a18c538c7b71d912a2bdbd822cc91d0a21f`):

| Metric                          | Value                                      |
| ------------------------------- | ------------------------------------------ |
| Serialized request bytes        | 5507 -> 4215 (1292 removed, ratio 0.76539) |
| Items                           | 27 -> 25                                   |
| Measured tokens                 | `null` (no token counter is wired offline) |
| Byte-derived token estimate     | 1376 -> 1053, explicitly an estimate       |
| Fallbacks / refusals / failures | 5 / 3 / 0                                  |
| Provider requests, budget spent | 0, `0.0`                                   |
| Boundary latency                | observed locally; not model latency        |

## Operations

The operating instructions are [`RELEASE.md`](RELEASE.md) sections 5 and 6 and
[`ROLLBACK.md`](ROLLBACK.md); this record names the entry points and the property
each one is checked by.

| Operation                                       | Command                                                             | Checked by                                                              |
| ----------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Verify the manifest, pins, patches, and adapter | `verify-manifest.py --patch-state applied --native-adapter applied` | `manifest.pins`, `host.*` gates                                         |
| Build the pinned host and record provenance     | `scripts/build_provenance.py build`                                 | `build_provenance.py verify`                                            |
| Create and inspect the isolated environment     | `isolated_env.py init`, `isolated_env.py status`                    | `isolated.roundtrip`                                                    |
| Select a profile and confirm the switch set     | `jev-profile.json`, `verify-manifest.py --profile <profile>`        | `approval.enforcement.disabled`, `remote_inference.disabled`            |
| Compose the validation record                   | `stack_validation.py run`                                           | this document                                                           |
| Derive release readiness                        | `release_readiness.py gates`                                        | `release_ready` below                                                   |
| Record real-host evidence for a platform        | `release_readiness.py record-platform --codex <binary>`             | `platform.matrix`                                                       |
| Build the candidate artifact                    | `release_readiness.py candidate --out <file>`                       | `candidate.exclusions`                                                  |
| Roll the environment back                       | `isolated_env.py rollback`                                          | `isolated.roundtrip`, `--patch-state absent`, `--native-adapter absent` |

Rollback order for the whole stack: unbind the optional components, roll the
isolated environment back (it **moves** to `.jev/superseded/isolated-<UTC>` and
never deletes), remove the native approval adapter, then unapply the ordered patch
and confirm the removal with `--patch-state absent`. The two removal checks
discriminate - on the integrated tree they report errors instead of passing
vacuously - and [`ROLLBACK.md`](ROLLBACK.md) names what is deliberately not
reversible (canonical evidence, component receipt stores, `jev/DEDUP_RECEIPTS.md`'s
ordering guarantees).

## Release readiness on this revision

```
combined_ready=true release_ready=false revision=4bac04bdc0 stages=8 digest=42e004f49f565ff1
release_blockers=macos-aarch64,platform.matrix,windows-x86_64
platforms: linux-x86_64=verified, macos-aarch64=not-run, windows-x86_64=not-run
gates: 10, failed 0, not-run 1
```

`combined_ready` is about the composed evidence and is true: every stage,
authority assertion, tier rule, and the isolated round trip hold. `release_ready`
is the #26 verdict and is **false**, because `macos-aarch64` and `windows-x86_64`
have no current recorded real-host run - the pin supports them, the gate refuses
to credit them. A release verdict that followed issue completion instead of
evidence would say `true` here; this one does not.

## What is not proven

- **No live-provider run.** Nothing in this record is live model accuracy,
  safety, or performance evidence, and no token count was measured.
- **`macos-aarch64` and `windows-x86_64` are unvalidated**, though they are
  supported by the pin; the platform gate stays `not-run` until a run is recorded
  there.
- **The recorded host runs used the debug binary**, not a release artifact, and
  the smokes exercise a loopback mock rather than a provider.
- **The `real-component` tier is 0 assertions in the default run.** Raising it
  means passing `--component <pinned jev-sentinel checkout>`; the tier is then
  recorded honestly rather than assumed.
- **The component-stub tier proves plumbing, not policy.** A stubbed Sentinel
  cannot show detection quality, only that the host's veto, latch, and deferral
  behavior holds over the wired protocol.
- **One GitHub account serves every lane**, so "author is not the reviewer"
  cannot be met with a distinct second identity; reviews are recorded with
  reproducible commands instead. Commits are pushed unsigned and GitHub reports
  them unverified because the signing key is not on this machine.

## Reproduce it

```sh
python3 jev/scripts/stack_validation.py run --json /tmp/stack.json   # combined record
python3 jev/scripts/e2e_regression.py run                            # 55/55, same trace digest
python3 jev/scripts/perf_validation.py run                           # same performance digest
python3 jev/scripts/release_readiness.py gates                       # same gate set
python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'   # the lane, including this record's controls
```

A changed combined digest means a stage, an authority assertion, a tier, a gate
status, or a measured number changed - the digest covers all of them, so it cannot
drift silently.
