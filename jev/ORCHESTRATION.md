# JEV orchestration ledger

Durable record for epic [#2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2).
It holds only what belongs in a repository: issue and dependency state, pinned
revisions, decisions, contracts, PR links, tested commits, and open blockers.
Credentials, private captures, and raw sensitive logs are never recorded here.

Local working state (worktrees, agent scratch, raw captures) stays outside this
repository; this file is the committable projection of it.

## Dependency order

```
#3 (phase 1) -> #4 (phase 2) -> #5 (phase 3) -> #6 (phase 4) -> #7 (phase 5) -> #8 (phase 6)
 #9  #10  #11    #12 #13 #14    #15 #16 #17    #18 #19 #20    #21 #22 #23    #24 #25 #26
```

Phases are strictly ordered: a later phase does not start while its predecessor
is incomplete, even when a concurrency slot is free.

## Status

| Issue | Scope | Owner | Branch | PR | State |
| --- | --- | --- | --- | --- | --- |
| #9 | Compatibility manifest and integration contracts | lead (integration) | `jev/1.1-manifest` | #28 | in review |
| #10 | Plaintext collaboration in the pinned build | lead (native/Rust) | `jev/1.2-plaintext` | #33 | in review |
| #11 | Isolated build and integration profile | lead (integration) | `jev/1.3-profile` | pending | blocked by #10 |
| #12–#14 | Fabric binding, capture, retrieval | unassigned | — | — | blocked by phase 1 |
| #15–#17 | Native bus adapter, receipts, views | unassigned | — | — | blocked by phase 2 |
| #18–#20 | Sentinel hooks, veto precedence, screening | unassigned | — | — | blocked by phase 3 |
| #21–#23 | Approval preflight, binding, shadow comparison | unassigned | — | — | blocked by phase 4 |
| #24–#26 | Regression, live-host validation, release package | unassigned | — | — | blocked by phase 5 |

## Pinned revisions

Recorded in `compatibility-manifest.json` and validated by
`jev/scripts/verify-manifest.py`:

| Component | Revision |
| --- | --- |
| codex-jev host base | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| codex-plaintext-collab | `073b3a99e4fa0eebe5417fd096f2e37abd7a7527` |
| jev-context-fabric | `5079099211c0d39a6ead347633a99d675a210c64` |
| jev-prune-kit | `2ecc8ff4e0976991c7abc09287d8f2f736d3164c` |
| jev-sentinel | `4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a` |
| jev-codex-approval | `0b931ee24a907c9dc47dc1828a94a6afcd7775b6` |

`omniroute-codex-docker` is excluded and is rejected by the validator if it is
ever added as a component.

## Decisions

| Decision | Rationale |
| --- | --- |
| The integration layer lives in `jev/` in this fork. | `AGENTS.md` reserves `docs/` for upstream product documentation; the integration needs a fork-specific home. |
| The manifest is JSON, and every script is standard-library Python. | The manifest and validator must run on any supported platform without pip or npm installation, and JSON parsing is part of every Python runtime. |
| Patches are build-time and pinned by digest and base commit. | The plaintext collaboration component is a source patch; recording digest, order, and base makes the integrated build reproducible and the disable path exact. |
| A behavior patch and its host test alignment are recorded as two patch entries. | Patch `0001` stays byte-identical to the component repository, so its provenance and digest are checkable, while the host-owned test expectations in patch `0002` are reviewed here. |
| Validation fails closed with stable error codes. | Unsupported combinations must fail explicitly instead of silently building an unvalidated configuration. |
| Remote inference is gated by credentials consent *and* a positive budget, not by a feature switch alone. | Optional remote inference stays disabled unless separately authorized and budgeted. |
| Shared interfaces have exactly one declared owner. | One writable owner per shared interface keeps the transformation boundary unambiguous. |

## Open blockers

| Blocker | Affected issues | Status |
| --- | --- | --- |
| Live-provider evidence needs explicit consent and a positive budget. | #10, #17, #22, #23, #25 | Open; offline and real-host-mocked-service tiers still run. |
| Real parent/child smoke with a live model requires the same consent. | #10 | Open; the focused transport tests run offline. |
