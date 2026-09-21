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
| #13 | Canonical capture and event correlation | #40 | merged; issue closed |
| #14 | Budgeted retrieval and hydration | #42 | merged; issue closed |
| #41 | Verify the fabric checkout revision against the pinned component | #44 | merged; issue closed |
| #15 | Native request adapter and bus boundary | #47 | merged; issue closed |
| #16 | Duplicate-read proof receipts | #50 | merged; issue closed |
| #17 | Approved Fabric views and reversible controls | #53 | merged; issue closed |
| #18 | Wire Sentinel hooks and effective coverage reporting | — | in review |
| #19–#20 | Sentinel veto precedence, retrieval screening, incident operations | — | open; blocked by #18 |
| #21–#23 | Approval preflight, binding, shadow comparison | — | open; blocked by phase 4 |
| #24–#26 | Regression, live-host validation, release package | — | open; blocked by phase 5 |

State above is the GitHub state of each issue and PR, not a local plan.

## Merged commits

The reviewed commit is the head that passed review; the merge commit is what
landed on `main`. Phase 2 is complete: #12, #13, #14, and the #41 follow-up are
merged.

| Issue | PR | Reviewed commit | Merge commit |
| --- | --- | --- | --- |
| #12 | #38 | `b720e9c2f6` | `017b472be5` |
| #13 | #40 | `30e8588e19` | `7826a887ac` |
| #14 | #42 | `1a355cfe66` | `afd973e17e` |
| #41 | #44 | `3bfc68389c` | `9ba51473e0` |
| #15 | #47 | `916ec10cae` | `f444b1066a` |
| #16 | #50 | `d5276641dc` | `a7949e368e` |
| #17 | #53 | `2bfb977a23` | `013ba38df8` |

## Pinned revisions

Recorded in `compatibility-manifest.json` and validated by
`jev/scripts/verify-manifest.py`:

| Component | Revision |
| --- | --- |
| codex-jev host base | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| codex-plaintext-collab | `7bf9202513a59362171b6687563580c9b02ec203` |
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
| A duplicate phase-2 capture/retrieval implementation is not landed. | A second session built an independent capture/retrieval pair (`capture_correlation.py`/`retrieval_budget.py`) for #13–#14 while `canonical_capture.py`/`retrieval.py` (#40, #42) were merged. The merged work is the canonical artifact; the duplicate stays in its own worktree and is not pushed, so one canonical change lands per issue. |
| A declared pin is not an enforced pin. | Verifying #12 showed the binding driver read the fabric revision from the manifest and never from the `--fabric` checkout it executed, so a record could name the pin while another revision installed. #41 fixes that and records the observed checkout revision; C8 states the rule. |
| A component checkout that is not its own work-tree root is recorded as unpinned, not refused. | Required CI drives an in-repo test double whose `rev-parse HEAD` would answer for the enclosing repository; only a checkout that reports a revision other than the pin is refused. |
| The host vendors `jev-bus.v1` and owns the `codex` call site. | The bus contract is owned by `jev-prune-kit` and vendored byte-identically into every participant; the host is the only component that knows where Codex builds its request, so the adapter normalizes supported shapes there and invokes the single bus owner. |
| A stage's replacement is accepted only where the host can re-derive the proof. | The dedup stage decides which reads are duplicates, but the host re-derives the receipt (exact arguments and body on a later retained copy, unique identities, outside the protected turn and tail) and reverts every unproven edit, so a marker's claim is never taken on its own word. |
| The outgoing array may only shrink by an item the host has approved. | A stage that removes an input item is reverted (`unapproved_removal`) unless an approved prose view, bound to the exact post-dedup snapshot, authorizes it; an approved view is refused whole when that snapshot changed or the turn was cancelled, so removal is never a stage's own authority and never touches tool, reasoning, or compaction items. |
| Sentinel enforcement is gated by a declared switch, not by the policy's `mode`. | The phase starts in local shadow mode (C5): a finding is recorded and never vetoes until `sentinel.enforcement` is on, and an enforcing policy under an off switch still returns nothing to the host, so enforcement can never be switched on by editing a policy file alone. The shadow and enforcement switches are the only mechanism the phase reads (`JEV_SWITCH_SENTINEL_*`). |
| The host vendors only Sentinel's serialization contract, never its detection. | The host must bound and correlate a payload before it forwards it and must know which response keys Codex supports, so `jev/scripts/jev_sentinel_adapter.py` carries `EVENTS`/`normalize`/`render`/`MAX_INPUT` from the pinned component and is proven equal to it on 26 goldens. Every verdict still comes from running the component's own `launch.py check`, so rules, thresholds, and the audit store are not duplicated. |
| Installation is not activation, and only a probe can show activation. | Codex requires per-hook trust approval that no file on disk records, so a present `hooks.json`, a resolvable launcher, and a matching revision are necessary but never sufficient. `coverage --probe` runs the *wired command itself* and requires an audit row whose `content_sha256` and `session_ref` match a per-run-unique canary session, so neither a stale row nor a launcher that merely echoes `{}` can be mistaken for activation. |
| A payload the host cannot bound is refused, never truncated. | A normalized event above the component's own input limit (`MAX_INPUT`) or content above the policy's `max_content_bytes` is not forwarded; the host records a fail-closed `REVIEW` incident (`backend="host_boundary"`) instead, because a shortened prompt would be assessed as if complete. |
| Bypass surfaces are reported, not assumed away. | `coverage.bypass_surfaces` names each observed way a finding is skipped or an action left ungated — `native_disable_all_hooks`, `hook_not_wired`, `launcher_unreachable`, `tools_outside_matcher`/`matcher_opaque`, `post_tool_replacement_unsupported`, `ingress_scope_is_prompt_only`, `local_rules_only`, `host_trust_unverified`, `component_revision_mismatch`, `integration_switch_off` — so the uncovered space is explicit. Only a canary through the wired command can show a hook is active, because a shadow response is `{}`. |

## Evidence

| Artifact | Covers |
| --- | --- |
| [`evidence/plaintext-pinned-build.md`](evidence/plaintext-pinned-build.md) | The host-driven plaintext smoke (real host, loopback mock) and the isolated-profile checks, with the unpatched pinned base as a negative control. |
| [`ISOLATED_ENV.md`](ISOLATED_ENV.md) | How to build the pinned host and run the isolated profile offline. |
| [`FABRIC_BINDING.md`](FABRIC_BINDING.md) | How the pinned fabric is bound to the isolated home, what `verify` proves, the checkout-revision rule, and the evidence tiers. |
| [`CAPTURE.md`](CAPTURE.md) | Canonical capture and event correlation: store layout, correlation, gaps, and evidence tiers (#13). |
| [`RETRIEVAL.md`](RETRIEVAL.md) | Budgeted retrieval and hydration: budgets, provenance-not-authority, refusals, and remote-enrichment refusal (#14). |
| [`BUS_BOUNDARY.md`](BUS_BOUNDARY.md) | The single request-construction boundary and its file/line anchors, the supported vs opaque shapes, the invocation invariants, and the fixture plus transport runs. |
| [`DEDUP_RECEIPTS.md`](DEDUP_RECEIPTS.md) | Duplicate-read proof receipts: the receipt the host proves, the reasons it reverts, and the enforcement invariants (#16). |
| [`FABRIC_VIEWS.md`](FABRIC_VIEWS.md) | Approved, reversible Fabric prose views: the snapshot binding, preview/apply/reset, the eligibility rules, and the byte/token split (#17). |
| [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) | The Sentinel hook boundary: the three events, the two switches, the payload bound and refusal, effective coverage, the activation probe, the incident envelope, and the bypass surfaces (#18). |

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
