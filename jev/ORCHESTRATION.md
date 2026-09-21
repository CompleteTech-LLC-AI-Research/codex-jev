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

| Issue | Scope | PR | State |
| --- | --- | --- | --- |
| #9 | Compatibility manifest and integration contracts | #28, #31 | merged; issue closed |
| #10 | Plaintext collaboration in the pinned build | #32, #37 | merged; issue closed |
| #11 | Isolated build and integration profile | #35 | merged; issue closed |
| #12 | Fabric runtime and MCP bound to the isolated workspace | #38 | merged; issue closed |
| #13–#14 | Canonical capture, budgeted retrieval | — | open; blocked by #12 |
| #15–#17 | Native bus adapter, receipts, views | — | open; blocked by phase 2 |
| #18–#20 | Sentinel hooks, veto precedence, screening | — | open; blocked by phase 3 |
| #21–#23 | Approval preflight, binding, shadow comparison | — | open; blocked by phase 4 |
| #24–#26 | Regression, live-host validation, release package | — | open; blocked by phase 5 |

State above is the GitHub state of each issue and PR, not a local plan.

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
| Validation fails closed with stable error codes. | Unsupported combinations must fail explicitly instead of silently building an unvalidated configuration. |
| Remote inference is gated by credentials consent *and* a positive budget, not by a feature switch alone. | Optional remote inference stays disabled unless separately authorized and budgeted. |
| Shared interfaces have exactly one declared owner. | One writable owner per shared interface keeps the transformation boundary unambiguous. |
| Concurrent duplicate work is consolidated into one canonical change per issue. | Three sessions opened an implementation of #9 at once (#27, #28, #29); merging duplicates would land incompatible trees, so the strongest artifact was adopted, its defects fixed, and the duplicates closed with a cross-reference. |

## Evidence

| Artifact | Covers |
| --- | --- |
| [`evidence/plaintext-pinned-build.md`](evidence/plaintext-pinned-build.md) | The host-driven plaintext smoke (real host, loopback mock) and the isolated-profile checks, with the unpatched pinned base as a negative control. |
| [`ISOLATED_ENV.md`](ISOLATED_ENV.md) | How to build the pinned host and run the isolated profile offline. |

The plaintext smoke is the real parent/child turn that #10's acceptance criteria
ask for; the focused transport tests alone could not show a child agent being
spawned, so both are recorded. The live-provider tier remains untouched.

## Open blockers

| Blocker | Affected issues | Status |
| --- | --- | --- |
| Live-provider evidence needs explicit consent and a positive budget. | #10, #17, #22, #23, #25 | Open; offline and real-host-mocked-service tiers still run. |
| A live-model parent/child smoke requires the same consent. | #10 | Open for the live tier only; a real parent/child turn against a loopback mock now runs in `jev/smoke/` and is recorded in `jev/evidence/`. |
| Every session authenticates to GitHub as one account, so "author ≠ reviewer" cannot be met with a second identity. | all | Open; reviews are recorded as self-review comments backed by reproducible automated checks. |
| The private key for the GitHub-verified commits in this repository is not on this machine. | all | Open; commits are pushed unsigned and GitHub reports them unverified. |
