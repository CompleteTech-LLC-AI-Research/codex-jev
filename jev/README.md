# JEV integration layer

This directory is the fork-specific home of the JEV integration for this Codex
checkout. It is owned by the integration host; the supporting components stay in
their own repositories and are consumed here at the exact revisions recorded in
[`compatibility-manifest.json`](compatibility-manifest.json).

Tracking epic: [CompleteTech-LLC-AI-Research/codex-jev#2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2).

One host-side native module lives outside this directory:
`codex-rs/core/src/jev_bus.rs` is the boundary the host itself invokes, and
`jev/patches/0002-jev-bus-boundary.patch` is the recorded source change that
installs its call site. Everything else the integration owns is here.

## What is in here

| Path | Purpose |
| --- | --- |
| `compatibility-manifest.json` | Machine-readable pins, patch order, interface versions, feature switches, events, credentials, and ownership. |
| `profiles/` | Integration profiles. Each profile declares the feature switches for one supported configuration. |
| `fixtures/` | Deterministic, labelled offline Responses-API fixtures. Nothing here is live evidence. |
| `scripts/isolated_env.py` | Create, inspect, and roll back the isolated environment under `.jev/isolated`. |
| `scripts/offline_fixtures.py` | Loopback-only fixture service that stands in for a provider. |
| `scripts/launch_isolated.py` | Run the pinned binary against the isolated home and the fixtures. |
| `scripts/fabric_env.py` | Bind the pinned context fabric's runtime, MCP entry, and hooks to the isolated home. |
| `scripts/canonical_capture.py` | Read the host's own rollout JSONL into the canonical capture store: envelope events, dedup receipts, gaps, and a redacted retrieval view. |
| `scripts/retrieval.py` | Return budgeted, source-backed excerpts and hydrate exact references, marked untrusted and never authoritative. |
| `scripts/bus_boundary.py` | The native request adapter and jev-bus boundary: normalize the host's `input` shapes and invoke the single bus owner (dedup stage 100, Fabric view stage 200). |
| `scripts/jev_bus.py` | The `jev-bus.v1` contract, vendored byte-identically to the pinned component copies. |
| `scripts/bus_stage_fixture.py` | A labelled `offline-fixture` jev-bus stage that stands in for a component, so the boundary can be exercised end to end where the owning packages are not installed. |
| `scripts/dedup_receipts.py` | Host-side enforcement of the duplicate-read receipt contract (C3): keep only dedup replacements a receipt proves, revert every other edit. |
| `scripts/fabric_views.py` | Approved, reversible Fabric prose views (C4): preview/apply/reset, bound to the post-dedup snapshot, with bytes and tokens reported separately. |
| `scripts/sentinel_boundary.py` | The Sentinel hook boundary (C5): normalize and bound the prompt/pre-tool/post-tool events, run the pinned evaluator, record correlated incidents, and report effective coverage and activation. |
| `scripts/sentinel_veto.py` | Host veto precedence (C5): map `REVIEW`/`BLOCK`/`QUARANTINE` onto the veto each stage supports, latch a session so a later approval or `DEFER` cannot clear it, serialize concurrent evaluations, and fail closed on every failure path. |
| `scripts/jev_sentinel_adapter.py` | The Codex hook translation vendored from the pinned `jev-sentinel`: `EVENTS`, `normalize`, `render`, the decisions, and the input bound. |
| `scripts/approval_shadow.py` | The approval shadow comparator and enforcement gate (C6): correlate typed judgments with the host's final decisions and observed timing, refuse raw content, account for every failure and deferral, and freeze a scenario-family calibration/holdout split. |
| `scripts/verify-manifest.py --native-adapter` | Assert that the declared native approval adapter is installed and wired where the manifest says it is, or gone again after a rollback. |
| `scripts/build_provenance.py` | Build the pinned CLI with the repository recipe and record provenance. |
| `scripts/verify-manifest.py` | Fail-closed validator for the manifest, a profile, the patch state, and the checkout pin. |
| `scripts/jev_manifest.py` | The validation rules, importable from tests. |
| `tests/` | Focused tests for every validation rule. |
| `patches/` | Ordered, digest-pinned patches applied to the pinned host source. |
| `smoke/` | Host-driven plaintext smoke: a real host runs a parent/child turn against a loopback mock. |
| `evidence/` | Recorded runs: what was executed, on which revision, and what was not. |

## Scope

- Integration host: this repository.
- Supporting components: `codex-plaintext-collab`, `jev-context-fabric`,
  `jev-prune-kit`, `jev-sentinel`, `jev-codex-approval`.
- **OmniRoute is excluded.** `omniroute-codex-docker` is listed in
  `integration.excluded_repositories`, and the validator rejects any manifest
  that tries to integrate it.

## Validate this checkout

```sh
python3 jev/scripts/verify-manifest.py --patch-state applied --json
python3 jev/scripts/verify-manifest.py --profile jev/profiles/integrated-offline.json
python3 -m unittest discover -s jev/tests -t jev/tests
python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'
```

Exit codes: `0` valid, `1` validation failed, `2` usage or unreadable input.
Every failure carries a stable code such as `E_PATCH_BASE` or
`E_REMOTE_INFERENCE_UNAUTHORIZED` so an unsupported combination fails
explicitly instead of silently building.

## Read next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — where each component sits in the request lifecycle.
- [`CONTRACTS.md`](CONTRACTS.md) — the cross-component contracts and the invariants they enforce.
- [`ISOLATED_ENV.md`](ISOLATED_ENV.md) — build the pinned host and run the isolated profile offline.
- [`FABRIC_BINDING.md`](FABRIC_BINDING.md) — bind the context fabric's runtime and MCP entry to the isolated home.
- [`CAPTURE.md`](CAPTURE.md) — turn the host's own transcript into canonical events, dedup receipts, gaps, and a redacted retrieval view.
- [`RETRIEVAL.md`](RETRIEVAL.md) — budgeted retrieval and hydration with provenance, workspace scoping, and remote enrichment refused.
- [`BUS_BOUNDARY.md`](BUS_BOUNDARY.md) — the single request-construction boundary, the supported vs opaque shapes, and the bus invocation invariants.
- [`DEDUP_RECEIPTS.md`](DEDUP_RECEIPTS.md) — what the dedup stage may replace, the receipt the host proves, and the edits it reverts.
- [`FABRIC_VIEWS.md`](FABRIC_VIEWS.md) — the approved prose view, its snapshot binding, the reversible controls, and the byte/token split.
- [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) — the Sentinel hook wiring, effective coverage, the activation proof, the incident envelope, and the bypass surfaces.
- [`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md) — the decision lattice, the per-session latch, the supported veto per stage, concurrent-action handling, the fail-closed paths, and the unavoidable races.
- [`APPROVAL_PREFLIGHT.md`](APPROVAL_PREFLIGHT.md) — the native approval preflight, its environment contract, eligibility and deferral, the answer binding and freshness rules, and the guarded host blobs.
- [`RETRIEVAL_SCREENING.md`](RETRIEVAL_SCREENING.md) — screening retrieved context before injection, the two switches, the withholding rules, and memory-write authorization.
- [`INCIDENT_OPERATIONS.md`](INCIDENT_OPERATIONS.md) — bounded, metadata-only incident operations: validation, the read-only disable plan, correlation, and the policy view.
- [`APPROVAL_SHADOW.md`](APPROVAL_SHADOW.md) — the shadow comparison by review id, the raw-content refusal, the every-failure accounting, the declared enforcement criteria, and why the gate never flips a switch.
- [`ROLLBACK.md`](ROLLBACK.md) — disable the integration and remove the isolated environment.
