# Evidence: the phase-6 release verdict is composed from evidence, not from closed issues

Tracks [#8](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/8),
phase 6 of [#2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2).
Its three sub-issues each already ship their own tier and their own tests:
[`#24`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/24) the
composed end-to-end harness, [`#25`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/25)
the offline/real-host/live coverage record, and
[`#26`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/26) the
release gates and the candidate artifact. Companion to
[`VALIDATION.md`](../VALIDATION.md), [`RELEASE.md`](../RELEASE.md), and
[`END_TO_END.md`](../END_TO_END.md).

What none of those three owns is the *phase* claim: that they compose into one
release verdict, and that the verdict is derived from evidence rather than from
the sub-issues being closed. That is what
[`.github/scripts/test_jev_release_phase.py`](../../.github/scripts/test_jev_release_phase.py)
checks, and what this document records.

Nothing here is a statement about model accuracy, safety, or speed. It records
what was executed, on which revision, and what was **not** executed.

## Revisions

| Item | Value |
| --- | --- |
| Verified revision of this repository | `4bac04bdc03e24bca1f9ce130ad77e680da98d95` (`main`) |
| Host pin the verdict was measured against | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| Toolchain | `rustc 1.95.0`, `cargo 1.95.0` (from `codex-rs/rust-toolchain.toml`), Python `>=3.11` |
| Gate implementation | [`jev/scripts/release_readiness.py`](../scripts/release_readiness.py) |
| Composed trace | [`jev/scripts/e2e_regression.py`](../scripts/e2e_regression.py) |
| Tier | composed claim runs offline (`offline-fixture`); it reads one `recorded` input, the `real-host-binary` platform matrix |

## What is executed

1. `release_readiness.evaluate_gates(...)` derives all ten gates from the
   checkout it is pointed at, including the isolated create/rollback round trip.
2. `e2e_regression.run(...)` produces the composed trace over the whole
   lifecycle.
3. The phase test asserts the *joined* properties: that the verdict is bound to
   this revision, that the gate vocabulary and the trace vocabulary are one
   vocabulary, that an asserted gate is never a pass, that readiness is exactly
   the conjunction of the gates, that the isolated round trip really created two
   fresh environments and rolled both back, and that the live tier is not run
   anywhere.

```
python3 .github/scripts/test_jev_release_phase.py            # 14 tests, ok
python3 jev/scripts/release_readiness.py gates --json /tmp/readiness.json
python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'   # 411, ok
python3 -m unittest discover -s jev/tests -t jev/tests -p 'test_*.py' # 183, ok
```

## Results at `4bac04bdc0`

The verdict is **`release_ready: false`** with nine gates passing and one
`not-run`:

```
release_ready=false gates=10 failed=0 not_run=1 revision=4bac04bdc03e24bca1f9ce130ad77e680da98d95
  blocking=macos-aarch64,platform.matrix,windows-x86_64
```

| Gate | Status | Evidence | Detail |
| --- | --- | --- | --- |
| `manifest.pins` | pass | verified-here | manifest, pins, and profiles validate with no error code |
| `host.patch.applied` | pass | verified-here | every manifest patch is applied to this checkout |
| `host.patch.rollback-detected` | pass | verified-here | `--patch-state absent` reports 2 error(s) on the integrated tree, so it can detect a failed rollback |
| `host.adapter.applied` | pass | verified-here | the declared native approval adapter is installed and wired |
| `host.adapter.rollback-detected` | pass | verified-here | `--native-adapter absent` reports 12 error(s) on the integrated tree, so it can detect an incomplete removal |
| `approval.enforcement.disabled` | pass | verified-here | every enforcement switch still defaults to `false` |
| `remote_inference.disabled` | pass | verified-here | `consent=false`, `budget=null`, default `false`, and the isolated profile keeps it off |
| `isolated.roundtrip` | pass | verified-here | two fresh environments resolved to the same plan digest and rolled back to a preserved record; the ambient home fingerprint is unchanged |
| `candidate.exclusions` | pass | verified-here | 129 files scanned with no refusal and no credential path |
| `platform.matrix` | **not-run** | recorded | no current recorded real-host run exists for `macos-aarch64`, `windows-x86_64` |

This is the point of criterion 3. `#24`, `#25`, and `#26` are all **closed**,
and the verdict is still `false` because two supported platforms have no current
recorded run. Closing an issue cannot move a gate: a gate whose evidence is only
an assertion is rewritten to `fail` by `summarize_gates`, which the phase test
proves with a synthetic pair (`claimed` -> `fail`, `recorded` -> `pass`).

The composed trace at the same revision is `ok` with **55 checks, 0 failed**,
in the declared lifecycle order (capture, retrieval, screening, projection,
collab, sentinel_and_veto, approval, execution). Its tiers are
`rollout-fixture`, `offline-fixture`, `bus-stage-stub`, and `component-stub` —
no `real-host` and no `live-provider` assertion, which the phase test asserts
directly (`tier_labels.real_host == []`, `tier_labels.live_provider == []`).

The isolated round trip resolved the validated configuration to the declared
`isolated-offline` profile (digest
`3e138ea57e4f7e936155c58ae0369880710c1bf61725f5fc8e04b02dfd9e2462`, matching
the checked-in profile file) with all four declared profiles digested, and both
environments rolled back to a preserved record with the ambient home untouched
and `remote_inference_enabled` false.

## Mutation controls

Each control mutates `release_readiness.py`, runs the phase test, and restores
the file byte-identically (sha256 verified before and after):

| Mutation | Expected | Outcome |
| --- | --- | --- |
| a `claimed` pass is accepted as a pass | test fails | KILLED — `test_an_asserted_gate_is_never_a_pass` |
| the skipped round trip is still credited | test fails | KILLED — `test_a_skipped_roundtrip_is_not_run_and_blocks_readiness` (mutation: `else:` no longer emits the gate, the pre-#98 behaviour) |
| the platform evidence is credited as `live-provider` | test fails | KILLED — `test_no_gate_claims_live_provider_coverage` |
| readiness ignores the `not-run` gates | test fails | KILLED — `test_readiness_is_the_exact_conjunction_of_the_gates` |

No survivor. The module was restored byte-identical (`sha256`
`48578edc9aaa72b73b23cd0c5c3f06cafa088bc3217ab7c1a9c41091bf7c531f`, the
post-#98 file; the pre-#98 file this record was first taken against hashed
`c4c3765cc58777f3360ff7f94891bc99b9b442890bd1983d0e315748d1df90e6`).

## What this does not show

- **No live-provider coverage.** The live tier appears in no gate and in no
  trace assertion. [`VALIDATION.md`](../VALIDATION.md) records it as *Not run*,
  blocked on explicit consent and a positive budget; the phase test asserts that
  row still says so. No paid inference was used anywhere in this work.
- **`release_ready` is false at this revision, by design.** The claim is that the
  verdict is evidence-driven, not that the artifact is releasable. Two platforms
  still need a recorded real-host run before it can be `true`.
- **The platform rows are `recorded`, not re-run here.** The linux-x86_64 row
  was recorded by `#26` from a checked-in run with binary sha256
  `6d51fdc9278d1a2fbc1bb01e024ae12d5d9a3b233d7229a7f6e792eaf35c0c3b`; this
  claim reads it, checks its tier and binding, and does not re-execute it.
- **Both gate-binding follow-ups are now fixed.** [`#97`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/97)
  was fixed by [`#99`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/pull/99)
  (merge `30f04eecba`): a record whose revision does not resolve in this clone is
  refused as `unverifiable` and named in the gate's `blocking`, instead of being
  credited. [`#98`](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/98)
  is fixed here: `gates --skip-roundtrip` now emits `isolated.roundtrip` as
  `not-run` rather than dropping it, so the round trip cannot leave the
  conjunction and a `release_ready: true` document is no longer reachable with it
  unproven — matching the flag's own `--help` ("it is then not-run"). The phase
  test moved from the weaker property (never *credited*) to the stronger one
  (`not_run` + `blocking` + `release_ready` false), and the mutation table above
  records the control that pins it.

## After the follow-ups

With `#97` and `#98` fixed, the phase-6 claim is re-taken at the `#98` change.
Editing `jev/scripts/release_readiness.py` moves the pinned-inputs digest, so the
`linux-x86_64` record was **re-run** on this revision (revision `29aefffe`, the
`#98` commit) with the same debug binary
`6d51fdc9278d1a2fbc1bb01e024ae12d5d9a3b233d7229a7f6e792eaf35c0c3b` — both host
smokes exit 0, and the projection-reset verdict keeps 35 input items and reduces
the serialized input `9705 -> 9130` bytes with a byte-identical switch-off
control (the absolute totals include the scratch root three times, so no
absolute byte total is path-independent - the 575-byte reduction is, as are the
35-item count and the byte-identical switch-off control, while the ratio holds
only at the recorded precision; [`VALIDATION.md`](../VALIDATION.md) and
[`projection-real-host.md`](projection-real-host.md) quote the same measurement
at other roots). The verdict is still `release_ready: false`, now blocking only on
`macos-aarch64` and `windows-x86_64` (linux-x86_64 is `verified`), because those
two supported platforms still have no recorded real-host run:

```
release_ready=false gates=10 failed=0 not_run=1 revision=29aefffee497ac673dc82e81fb95e194ef2b01dd
  blocking=macos-aarch64,platform.matrix,windows-x86_64
```

The skipped-round-trip document is the complement, and it is *not* a pass:
`gates --skip-roundtrip` reports `not_run=2` with `isolated.roundtrip` named in
both `not_run` and `blocking`.

## Reproduce

```
cd <checkout at 4bac04bdc0>
python3 .github/scripts/test_jev_release_phase.py
python3 jev/scripts/release_readiness.py gates --scratch /tmp/rr --json /tmp/readiness.json
```

The mutation controls are run by editing `jev/scripts/release_readiness.py` as
the table above describes; the file must be restored byte-identical afterwards.

## Re-record for #136 (after this claim)

The rows above are the claim as it was taken at the `#98` revision
(`29aefffe`/`4bac04bdc0`). The `linux-x86_64` matrix has since been re-recorded
for the `#136` mechanism fix at `e6fd719b21`, where `record_platform` began
pairing each record with the digest its own revision carries. That re-record was
taken from a **clean** worktree with a freshly built debug binary, sha256
`f363d4e594a092be38dc5ba60ef49d85dc5b943d67d115fdf3340afd507aaf66`, because the
earlier `6d51fdc9278d1a2fbc1bb01e024ae12d5d9a3b233d7229a7f6e792eaf35c0c3b` binary
no longer exists on the recording host and could not be reproduced. The two hosts
are the same source revision's build; only the digest of the recorded artifact
differs. The current record and its `revision_inputs_digest` agree, both host
smokes exit 0, and `gates` still reports `failed=0 not_run=1`.
