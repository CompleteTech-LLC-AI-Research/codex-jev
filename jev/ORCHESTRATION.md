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
| #5 | Native Codex context projection through jev-bus (phase 3) | #47, #63, #64, #75, #79 | complete; issue closed |
| #6 | Sentinel boundary checks and veto precedence (phase 4) | #60, #68, #80, #84, #91, #92 | complete; issue closed. Phase claim composed in #91 (`real-component`) and exercised through a launched host binary in #92 (`real-host-binary`) |
| #7 | Native JEV approval preflight with Guardian fallback (phase 5) | #72, #74, #81 | complete; issue closed; follow-up #85 filed |
| #8 | Validate and release the combined JEV Codex stack (phase 6) | #24, #25, #26, #101 | in progress; #24, #25, and #26 merged and closed; the composed phase claim is in review as #101. The verdict it records is `release_ready: false` with `platform.matrix` `not-run`, because `macos-aarch64` and `windows-x86_64` have no current recorded run - readiness follows the evidence, not the closed sub-issues |
| #9 | Compatibility manifest and integration contracts | #28, #31 | merged; issue closed |
| #10 | Plaintext collaboration in the pinned build | #32, #37 | merged; issue closed |
| #11 | Isolated build and integration profile | #35 | merged; issue closed |
| #12 | Fabric runtime and MCP bound to the isolated workspace | #38 | merged; issue closed |
| #13 | Canonical capture and event correlation | #40 | merged; issue closed |
| #14 | Budgeted retrieval and hydration | #42 | merged; issue closed |
| #15 | Native request adapter and bus boundary | #47 | merged; issue closed |
| #16 | Duplicate-read proof receipts | #50 | merged; issue closed |
| #17 | Approved Fabric views and reversible controls | #53 | merged; issue closed |
| #18 | Wire Sentinel hooks and effective coverage reporting | #60 | merged; issue closed |
| #19 | Sentinel veto precedence and the subsequent-action latch | #68 | merged; issue closed |
| #20 | Retrieval screening and incident operations | #80 | merged; issue closed |
| #21 | Port and compile the pinned native approval adapter | #72 | merged; issue closed |
| #22 | Verify action binding, freshness, and fallback | #74 | merged; issue closed |
| #23 | Shadow comparison and controlled enforcement configuration | #81 | merged; issue closed; duplicate #83 closed, follow-up #85 filed |
| #24 | Composed end-to-end regression harness | #89 | merged; issue closed |
| #25 | Isolated offline and performance validation record | #90 | merged; issue closed |
| #26 | Package compatibility, upgrade, and rollback workflow | #95 | merged; issue closed; follow-ups #97 and #98 filed |
| #41 | Verify the fabric checkout revision against the pinned component | #44 | merged; issue closed |
| #49 | Canonical capture CLI: report a zero-event capture gap | #64 | merged; issue closed |
| #52 | Host proof is weaker than the component's own receipt validator | #65 | merged; issue closed by independent verification of the merge `f9ba672eeb` (the body closed the gaps; the issue was closed separately once that was verified) |
| #55 | Invoke the bus boundary from the host request path | #63 | merged; issue closed |
| #57 | Reconcile C4's package-approval semantics with the host-owned view control | #62 | merged; issue closed; filed from #17 |
| #58 | Reconcile the host's item-count fallback with the approved-view removal | #73 | merged; issue closed; filed from #55 |
| #61 | Invoke the Sentinel carrier from the host hook path | #84 | merged; issue closed |
| #66 | repo-checks is red on `main`: root `README.md` asciicheck | #71 | merged; issue closed |
| #69 | repo-checks is red on `main`: `just fmt-check` needs `ruff format` and a vendored-file exclusion | #77 | merged; issue closed |
| #70 | Run the real host binary to show projected outgoing content and exact reset | #75 | merged; issue closed |
| #78 | The host exposes an eligible read tool: prove the pair can arise during a turn | #79 | merged; issue closed |
| #82 | repo-checks is red on `main`: prettier wants the README entry-point table realigned | #77 | merged; issue closed |
| #85 | Bind the approval gate to the frozen holdout it was measured on | #88 | merged; issue closed; filed from #23 |

State above is the GitHub state of each issue and PR, not a local plan.

## Merged commits

The reviewed commit is the head that passed review; the merge commit is what
landed on `main`. Phases 3 to 5 are complete and phase 6 is partly merged: #24
and #25 landed, and #26 is in flight.

| Issue | PR | Reviewed commit | Merge commit |
| --- | --- | --- | --- |
| #12 | #38 | `b720e9c2f6` | `017b472be5` |
| #13 | #40 | `30e8588e19` | `7826a887ac` |
| #14 | #42 | `1a355cfe66` | `afd973e17e` |
| #41 | #44 | `3bfc68389c` | `9ba51473e0` |
| #15 | #47 | `916ec10cae` | `f444b1066a` |
| #16 | #50 | `d5276641dc` | `a7949e368e` |
| #17 | #53 | `2bfb977a23` | `013ba38df8` |
| #18 | #60 | `45cfc93210` | `f9076fb7bc` |
| #49 | #64 | `842d99d216` | `186198d3fe` |
| #55 | #63 | `0c10851a79` | `dad9e5ad1e` |
| #52 | #65 | `0ad456b527` | `f9ba672eeb` |
| #21 | #72 | `1e03adbaa` | `8ca45b6a7a` |
| #66 | #71 | `aab06717a6` | `997b33da76` |
| #70 | #75 | `89615fdaab` | `220c6e5023` |
| #19 | #68 | `6a0ec2a841` | `660bab6c89` |
| #22 | #74 | `aeffd4bcfc` | `f18c00627f` |
| #78 | #79 | `6648f37cf3` | `2a3082ab76` |
| #20 | #80 | `254ec9ddee` | `e54571283e` |
| #69 | #77 | `902076f724` | `a844c9645f` |
| #82 | #77 | `902076f724` | `a844c9645f` |
| #23 | #81 | `9e5ad0682b` | `aef58a0c20` |
| #61 | #84 | `3fd858ef45` | `d2882fb594` |
| #57 | #62 | `7e09aeb14c` | `e95eae7595` |
| #58 | #73 | `3b82f8b82f` | `00e6b3b434` |
| #24 | #89 | `720adab060` | `177f57110a` |
| #25 | #90 | `af36a77fd9` | `392123ebe5` |
| #85 | #88 | `e6e228cc97` | `d6f53c5e46` |
| #6 | #91 | `259aacd12d` | `3ddf7345a9` |
| #6 | #92 | `5a739b70e6` | `ed60a5b0e4` |
| #26 | #95 | `82a6225e0c` | `c312d4aaea` |

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

Two ordered patches turn the pinned base into the integrated host. Each records
its own digest, unique `order`, and targets, and the tree's state is validated
with `jev/scripts/verify-manifest.py --patch-state applied`.

| Order | Patch | Component | Targets |
| --- | --- | --- | --- |
| 10 | `0001-plaintext-collab` | codex-plaintext-collab | The collaboration router, its spec-plan tests, and the seven request-history snapshots whose tool fingerprint changes. |
| 20 | `0002-jev-bus-boundary` | codex-jev | The native host boundary module and its tests, plus the two call-site lines in `codex-rs/core/src/client.rs` and `codex-rs/core/src/lib.rs`. |

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
| A declared call site is not an invocation. | The merged #15 declared `jev_bus_call_site` as a host-owned interface and documented `bus_boundary.py` as "the host's carrier", but `codex-rs` contained no `jev` reference, so a launched host projected nothing and #17's "reduced outgoing content" could not be demonstrated by any run. #55 records the gap; the call site is now real, invoked once per outgoing payload, and returns the caller's array unchanged on every error. |
| A native adapter's call site is an ordered patch. | `ARCHITECTURE.md` admits only ordered patches and native adapters as host-source changes. The native adapter is the module `codex-rs/core/src/jev_bus.rs`; the edit that installs it in `client.rs` and `lib.rs` is recorded as patch `0002-jev-bus-boundary` (order 20) against the pinned base, with its own digest and disable path, so a pinned build can reproduce or omit the call site exactly. |
| The host's length rule and an approved view removal must agree. | #55 requires the host to fail closed on a changed item count, and #17 lets an approved view drop prose, so the host as merged at #55 reduced bytes but never items. The two rules were individually right and jointly inconsistent; #58 reconciles them rather than weakening the fallback silently, and that reconciliation is merged as #73 (`00e6b3b434`). |
| A receipt records a projection that reached the wire. | Re-deriving #55 on the merged #54 showed a *declining* stage (`ok: false`, no array) being read as a structural change, so it earned an `applied` slot and a `projection_receipt` although the bus kept its input — the opposite of #54's own rule, and it turned the required CI step red on rebase. #59 (merged) fixes `bus_boundary.observe()` so only an array the bus itself accepted counts; this branch keeps no code change there and instead pins the same rule from the host side in `test_jev_bus_host.py`, so the two cannot drift apart unnoticed. |
| A stage's replacement is accepted only where the host can re-derive the proof. | The dedup stage decides which reads are duplicates, but the host re-derives the receipt (exact arguments and body on a later retained copy, unique identities, outside the protected turn and tail) and reverts every unproven edit, so a marker's claim is never taken on its own word. |
| The outgoing array may only shrink by an item the host has approved. | A stage that removes an input item is reverted (`unapproved_removal`) unless an approved prose view, bound to the exact post-dedup snapshot, authorizes it; an approved view is refused whole when that snapshot changed or the turn was cancelled, so removal is never a stage's own authority and never touches tool, reasoning, or compaction items. |
| Sentinel enforcement is gated by a declared switch, not by the policy's `mode`. | The phase starts in local shadow mode (C5): a finding is recorded and never vetoes until `sentinel.enforcement` is on, and an enforcing policy under an off switch still returns nothing to the host, so enforcement can never be switched on by editing a policy file alone. The shadow and enforcement switches are the only mechanism the phase reads (`JEV_SWITCH_SENTINEL_*`). |
| The host vendors only Sentinel's serialization contract, never its detection. | The host must bound and correlate a payload before it forwards it and must know which response keys Codex supports, so `jev/scripts/jev_sentinel_adapter.py` carries `EVENTS`/`normalize`/`render`/`MAX_INPUT` from the pinned component and is proven equal to it on 26 goldens. Every verdict still comes from running the component's own `launch.py check`, so rules, thresholds, and the audit store are not duplicated. |
| Installation is not activation, and only a probe can show activation. | Codex requires per-hook trust approval that no file on disk records, so a present `hooks.json`, a resolvable launcher, and a matching revision are necessary but never sufficient. `coverage --probe` runs the *wired command itself* and requires an audit row whose `content_sha256` and `session_ref` match a per-run-unique canary session, so neither a stale row nor a launcher that merely echoes `{}` can be mistaken for activation. |
| A payload the host cannot bound is refused, never truncated. | A normalized event above the component's own input limit (`MAX_INPUT`) or content above the policy's `max_content_bytes` is not forwarded; the host records a fail-closed `REVIEW` incident (`backend="host_boundary"`) instead, because a shortened prompt would be assessed as if complete. |
| Bypass surfaces are reported, not assumed away. | `coverage.bypass_surfaces` names each observed way a finding is skipped or an action left ungated — `native_disable_all_hooks`, `hook_not_wired`, `launcher_unreachable`, `tools_outside_matcher`/`matcher_opaque`, `post_tool_replacement_unsupported`, `ingress_scope_is_prompt_only`, `local_rules_only`, `host_trust_unverified`, `component_revision_mismatch`, `integration_switch_off` — so the uncovered space is explicit. Only a canary through the wired command can show a hook is active, because a shadow response is `{}`. |
| The manifest pin names the pre-patch base; merged revisions are recorded in this ledger. | The `codex-jev` manifest `revision` is the tree the ordered patches apply to, so it must equal `host.base_commit`. A merged revision already contains patch `0002`, so pinning it would make `apply-patches.py` fail and invalidate every profile. Merged revisions therefore live in the *Merged commits* table, and the applied tree is proved by `verify-manifest.py --patch-state applied`. |
| A ported native adapter is declared, not described. | `jev-codex-approval` ships its Codex adapter as source that its authors never compiled. The port is recorded in the manifest as an installed file, the module declaration and call site it creates, and the two guarded host blobs it was applied to, so `verify-manifest.py --native-adapter` can prove the port is present and wired - and prove it is gone after a rollback - instead of relying on a document. |
| An eligible preflight may replace one synchronous review attempt, and nothing else. | Concurrency, escalation, retries, mandatory review, non-eligible action classes, incomplete context, and any change of policy text or authorization version all return `None` and run the unchanged Guardian path. Enforcement stays off, and the host re-checks the low-risk boundary itself rather than trusting the engine's own policy. |
| A port may not accept a weaker answer binding than the component it ports. | `jev-codex-approval`'s own transport refuses an answer whose `request_id`, `snapshot_hash`, `policy_hash` or `question_hash` does not match what it sent (`daemon_response_binding_mismatch`), but the first port checked only the request id, so it would have accepted a decision computed for another action, policy, or question set under a reused review id. #22 binds all four, records the approved question set as a manifest pin the host cannot recompute, and pins the canonical form with digests the component computes so the two implementations cannot drift apart unobserved. |
| Enforcement promotion is a declared, gated state, not an inferred one. | Shadow mode is `approval.preflight` on with `approval.enforcement` off, so a candidate answer is recorded and never applied. The gate reports readiness and writes no switch, and the manifest's `evaluation` block is the reviewed home of the criteria, the consent credential, the opt-in categories, and the Guardian-only return - so "enforcement stays disabled until the criteria are met" is validated (`E_EVALUATION_*`) and computed rather than promised. |
| A veto already latched for a session is never downgraded. | The host orders the decisions `DEFER < REVIEW < BLOCK < QUARANTINE` and takes `max(this event, the session latch)`, so a later approval-shaped event, a later `DEFER`, or a weaker finding cannot clear a veto. The component's finding is still the only detection: the latch only decides which veto keeps the session, and the strongest concurrent finding survives. |
| A latched veto gates the action that follows it, and only the pre-tool stage can prevent the exact action before execution. | After a veto the session stays vetoed, so the next `PreToolUse` is denied even though its own finding defers. Codex has no output-replacement field, so a `PostToolUse` veto is feedback plus that latch — a result that already executed cannot be un-executed, and the host never fabricates a replacement. |
| Concurrent evaluations of one session are serialized, and the guarantee is no stale read and no lost raise. | A per-session `threading.Lock` plus an `flock(2)` file lock serialize the read-modify-write of the latch. Arrival order is not observable to the host, so an event evaluated before the veto exists legitimately inherits nothing; what is enforced is that no call reads a pre-veto ledger and no raise is dropped. |
| Every failure path fails closed and never turns into an allow. | A timeout, a malformed response, a malformed verdict, a non-zero exit, an unboundable payload, a malformed native payload, a corrupt policy, and a cancellation all produce the component's own `failure_result` shape (`REVIEW`, `route=security_review`, `backend=unavailable`) and latch the session. A corrupt or missing policy is treated as *enforcing*, so editing a policy file can never silently open the gate; a cancellation is latched before the interrupt propagates. |
| The host latch and the component's taint are separate stores, and clearing one does not clear the other. | The component taints its own session in its audit store; the host keeps an append-only latch ledger keyed by the component's own `session_ref`. Clearing the host latch leaves the component's taint vetoing on its own findings, which is the demo that the host has not reimplemented detection. Bounded replay of the ledger tail is lossless: the state is "the strongest escalation since the last clear". |
| A host owns what it can prove, and only that; a component finding is not a fact. | Retrieved context is screened by the pinned component's real `context` stage and a proposed write by its real `memory` stage, so no host code classifies content. The host nevertheless withholds **unconditionally** on the facts it already holds (`not_canonical_evidence`, `unproven_origin`, `cross_workspace`, `redaction_regression`, `duplicate_of_accepted`, `over_budget`, `over_bound`, an uncleared #19 session veto), because no switch can make a fact the host proved safe to inject; a *component* finding withholds only when `screening.enforcement` is on and the screening policy's exact `withhold_on` set names the decision, and otherwise injects and records the shadow signal `would_withhold`. |
| Two screening switches, and switching off is not a fourth decision. | `screening.retrieval` decides whether the component is consulted; `screening.enforcement` decides whether a finding withholds and is declared only behind retrieval, because there is nothing to enforce without an assessment. Off, every row carries `assessed: false` rather than a `DEFER` the component never returned, and a genuine component `DEFER` is recorded as `component_defer`. Both default to false, so the isolated profile starts dark and no fixture can be mistaken for a screening result. |
| A withheld candidate is still evidence. | Withholding is never erasure: the row is a metadata pointer into the canonical store, and `verify_withheld` re-proves each row against the captured content it names and reports any row it cannot resolve, so a withheld excerpt can be audited without being re-injected. A memory write is refused when its target is not named in the host's own policy whatever the switches say, which keeps authorization a host rule rather than a component opinion. |
| Incident operations are bounded, read-only, and report by identifier, never by content. | Every row is validated against the manifest's declared envelope fields plus the documented host fields; a digest must be a digest, a metadata string must stay inside its byte bound, and no string may still match the capture layer's credential rules. A failing row is reported by identifier and failing check only and never printed, so a careless writer cannot launder content into a report; reads cap at the component's own `outbox --limit` ceiling and a saturated scan says so. `disable` prints the exact commands (`executes: false`) and the module never writes, so asking what disabling would do can never itself disable anything; a correlation names the key that matched and leaves a non-match unmatched; and the policy view is confined to the isolated profile root so an operator is never shown a policy a launch would not use. |
| A later phase resolves its own switches through one rule. | `sentinel_boundary.feature_switches_for` is the single rule - an unset `JEV_SWITCH_*` means the manifest's declared default, never "off" - and `feature_switches` is now that rule applied to Sentinel's pair. Screening declares its two switches in the manifest behind `screening.retrieval` and `sentinel.enforcement`, so the same resolution and the same fail-closed default cover every phase rather than each reimplementing it. |
| Enforcement is a declared contract the gate computes, and the gate never flips a switch. | `jev-codex-approval` carries an `evaluation` record - the shadow and enforcement switches, the report kind, the opt-in action classes, the consent credential, the seven Guardian-only return conditions, and the promotion criteria - and the manifest validator refuses a record that is missing (`E_EVALUATION_MISSING`), malformed (`_SCHEMA`), mis-ordered or unowned (`_SWITCH`), outside the two replaceable action classes (`_CATEGORY`), consented by default (`_CONSENT`), or short of a Guardian-only return (`_STATE`). The gate then evaluates a report against those declared criteria and reports `permitted` with `enforcement_enabled` always `false`, so "enforcement stays disabled until declared evaluation criteria are met" is a computed statement and the only thing that can set a switch is the operator. |
| A duplicate #23 implementation is consolidated into the merged change. | Two sessions implemented #23 at once: #81 (`approval_shadow.py`, `APPROVAL_SHADOW.md`, the manifest `evaluation` record, and the validator gate) and #83 (`shadow_comparison.py`, with digest-pinned calibration/holdout splits, a redaction audit over records and attached documents, a self-verifying report, and a declared-record precondition on the gate). Both added `.github/scripts/test_jev_shadow.py`, so only one could land. #81 merged first (`aef58a0c20`) and is the canonical artifact; #83 was closed with a cross-reference and its branch is kept as prior art rather than landed as a second shadow comparison. The one clause #81 leaves as procedure - the gate never binds a report to the frozen holdout it was measured on - is filed as #85 against the merged module, so the phase keeps one implementation and the residual gap keeps its own issue. |
| The wired command is the host carrier itself, and its two failure halves are deliberately opposite. | `hooks.json` now runs `sentinel_boundary.py hook`, so the boundary the host invokes is the one it can read back, its stdout is the native hook response, and the component's `launch.py check` stays the only verdict source (`veto.handle` -> `observe` -> `check`). A payload the carrier cannot bound or parse, or a component it cannot resolve, is **fail-closed** in the component's own failure shape (`spawned: false`) and records **no** incident, so an unwired host cannot mint host evidence; the process itself is **fail-open** (always `json.dumps(response)`, exit 0), so a broken Sentinel cannot wedge the host's hook path. Installation merges rather than replaces (`hooks.json` keeps the Fabric entries and every unrelated key), is gated on the declared switches (`E_SWITCH_OFF` when both are off), pins `--state-dir` so the host journal is read back from the directory the carrier writes, and `--remove` needs neither a switch nor a resolvable component because uninstalling must always be possible. A carrier-wired stage is only credited as covered when the probe finds the host's correlated incident (`session_ref` and `content_sha256`) in the wired state dir, not merely the component's audit row. |
| A conflict is classified before it is resolved. | The #61 rebase hit #77, which reformatted the four files #61 edits. `git diff -w` does not separate formatting from behaviour, because the formatter's line splits survive a whitespace-insensitive diff, so the conflict was classified by formatting the pre-#77 base with the same `ruff` and comparing the result byte-for-byte with post-#77 `main`: byte-identical means formatting-only, and the resolution is "keep this branch's content and re-run `ruff format`". A resolution that cannot be shown to be formatting-only is a behaviour change and needs its own review rather than a solve. |
| Evidence tiers are a ladder, and a closed phase can still gain a higher rung. | #6 was closed by #91 at the `real-component` tier, which calls the carrier's Python API directly. #92 adds the `real-host-binary` rung for the same criteria: a launched `codex` process runs the wired command, so the host's own hook discovery, trust path and `PreToolUse` deny handling are in the loop rather than assumed. Landing it after the phase closed is an increase in what is proven, not a re-claim, and #92's body and evidence doc say so explicitly. The rung below never substitutes for the one above it, and neither is presented as live-provider evidence. |
| A release verdict is a conjunction of evidence, and a phase claim may be that the verdict is `false`. | Phase 6's three sub-issues are merged and closed, and the composed verdict at `4bac04bdc0` is still `release_ready: false` with nine gates passing and `platform.matrix` `not-run` for the two platforms that have no current recorded run. That false verdict **is** the phase's acceptance - "release readiness follows actual evidence rather than issue completion alone" - so the phase test asserts the composition and the *derivation* rather than the greenness: readiness is exactly `not failed and not run`, a `claimed` gate is rewritten to `fail`, readiness would hold if every gate carried evidence, and an unready verdict always names what is missing. A phase test that could only pass when every gate is green would be satisfied by closing issues, which is the failure mode the criterion names. |

## Evidence

| Artifact | Covers |
| --- | --- |
| [`evidence/plaintext-pinned-build.md`](evidence/plaintext-pinned-build.md) | The host-driven plaintext smoke (real host, loopback mock) and the isolated-profile checks, with the unpatched pinned base as a negative control. |
| [`ISOLATED_ENV.md`](ISOLATED_ENV.md) | How to build the pinned host and run the isolated profile offline. |
| [`FABRIC_BINDING.md`](FABRIC_BINDING.md) | How the pinned fabric is bound to the isolated home, what `verify` proves, the checkout-revision rule, and the evidence tiers. |
| [`CAPTURE.md`](CAPTURE.md) | Canonical capture and event correlation: store layout, correlation, gaps, and evidence tiers (#13). |
| [`RETRIEVAL.md`](RETRIEVAL.md) | Budgeted retrieval and hydration: budgets, provenance-not-authority, refusals, and remote-enrichment refusal (#14). |
| [`BUS_BOUNDARY.md`](BUS_BOUNDARY.md) | The single request-construction boundary and its file/line anchors, the host call site and the environment contract it resolves, the supported vs opaque shapes, the invocation and fallback invariants, and the fixture, transport, host-invocation, and `real-host-binary` runs. |
| [`evidence/projection-real-host.md`](evidence/projection-real-host.md) | The `real-host-binary` run behind #70: a launched `codex` resumes a seeded session, the recorded `input` shrinks by 575 bytes with the item count unchanged, switch-off is byte-identical to the unswitched control, no rollout is projected, the receipt is emitted only when the switch is on, and the seeded-transcript limit is stated. |
| [`evidence/projection-read-tool-host-run.md`](evidence/projection-read-tool-host-run.md) | The run behind #78 that closes the seeded-transcript limit: the host itself executes two identical `memories` `read` calls during the turn, so the eligible pair is produced inside the turn and then projected - switch-off is byte-identical to the unswitched control, the item count is unchanged, exactly one body is projected, the receipt names a witness that still holds its body, and no rollout is projected. |
| [`DEDUP_RECEIPTS.md`](DEDUP_RECEIPTS.md) | Duplicate-read proof receipts: the receipt the host proves, the reasons it reverts, and the enforcement invariants (#16). |
| [`FABRIC_VIEWS.md`](FABRIC_VIEWS.md) | Approved, reversible Fabric prose views: the snapshot binding, preview/apply/reset, the eligibility rules, and the byte/token split (#17). |
| [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) | The Sentinel hook boundary: the three events, the two switches, the payload bound and refusal, effective coverage, the activation probe, the incident envelope, and the bypass surfaces (#18); the host carrier wiring that makes the host itself the invoker - `hook` as the wired command under the component's own stdin bound, fail-closed unboundable payloads and unresolvable components against a fail-open process, merge-not-replace `install-hooks` behind the declared switches with `--state-dir` pinned, and the activation probe crediting a stage only when the host's correlated incident is present (#61); and the phase claim composed on one running session - three correlated canary incidents across the stages, installation written by the real installer but still not activation, enforcement denying the exact action at both the tool and prompt boundaries while shadow records and returns `{}`, a latched veto gating the next action with its incident naming the latched cause, and quarantined context withheld by the supported search path, journaled, and re-proved with no excerpt bytes in the plan or the journal (#6). |
| [`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md) | Veto precedence and the subsequent-action latch: the decision lattice, the stage mapping, the per-session latch and its operator controls, concurrent-action handling, every fail-closed path, and the unavoidable races (#19). |
| [`APPROVAL_PREFLIGHT.md`](APPROVAL_PREFLIGHT.md) | The ported native approval adapter: the guarded host blobs, the environment and switch contract, eligibility and deferral, the accepted answer's binding to this exact request and policy, the freshness re-derivation, and the offline-fixture plus static host compile evidence (#21, #22). |
| [`APPROVAL_SHADOW.md`](APPROVAL_SHADOW.md) | The shadow comparison: correlation by review id, the raw-content and shape refusals, the every-failure/deferral accounting, the declared enforcement criteria and their manifest home, the frozen scenario-family calibration/holdout, the consent and opt-in boundaries, the Guardian-only return, and why the gate never flips a switch (#23). |
| [`RETRIEVAL_SCREENING.md`](RETRIEVAL_SCREENING.md) | Screening retrieved context before injection and authorizing memory writes: the division of labour, the two independent switches, the unconditionally-withholding host facts and the exact-set `withhold_on` rule, the shadow signal, `verify_withheld`, and the real-component tier (#20). |
| [`INCIDENT_OPERATIONS.md`](INCIDENT_OPERATIONS.md) | Bounded, read-only, metadata-only incident operations: the validated field set and the credential-rule refusal, the identifier-only failure report, the bounded scan and its saturation signal, correlation by content or identity, the non-executing disable plan, and the isolated-root policy view (#20). |
| [`END_TO_END.md`](END_TO_END.md) | The composed end-to-end regression harness: one request lifecycle (capture → retrieval → screening → projection → collaboration → Sentinel/veto → approval → execution) consuming the previous step's artifact rather than re-deriving it, its negative set (stage failure, corrupt context, veto, cancellation, stale authorization, disabled components), the wire and side-effect assertions, and the tier labels - all offline and hermetic (#24). |
| [`VALIDATION.md`](VALIDATION.md) | The validation record: the `offline-fixture`/`bus-stage-stub`/`component-stub` tiers, the `real-host-binary` run, the live tier left explicitly **not run** for want of consent and budget, the revisions exercised, and the baseline-versus-integrated performance measurement reporting correctness, payload bytes, latency, fallback rates, failures, and service usage without dropping unsuccessful cases (#25). |
| [`evidence/sentinel-hook-real-host.md`](evidence/sentinel-hook-real-host.md) | The `real-host-binary` rung for #6: a launched host runs the wired carrier and is gated by it - shadow stays observational with three correlated incidents, enforce prevents the exact action before execution and the host quotes the component's reason, unwired records nothing under the same switches, and the coverage report says `activated: false` while three stages are wired, `true` only after a probe the host corroborates, and `false` again after `--remove` (#6). |
| [`evidence/release-phase-claim.md`](evidence/release-phase-claim.md) | The composed phase-6 verdict: the ten gates and their evidence kinds at `4bac04bdc0`, why nine passing gates and one `not-run` platform gate make `release_ready` false while all three sub-issues are closed, the isolated round trip that reproduced the validated profile and rolled two fresh environments back with the ambient home untouched, the four mutation controls (all killed), and the two open binding weaknesses #97 and #98 stated rather than hidden (#8). |

The plaintext smoke is the real parent/child turn that #10's acceptance criteria
ask for; the focused transport tests alone could not show a child agent being
spawned, so both are recorded. The projection smoke is the launched host that
#5's third acceptance item asks for, and it states its own limit: it resumes a
seeded transcript rather than watching a host find its own duplicate pair. The
live-provider tier remains untouched.

## Open blockers

| Blocker | Affected issues | Status |
| --- | --- | --- |
| Live-provider evidence needs explicit consent and a positive budget. | #10, #17, #22, #23, #25 | Open; offline and real-host-mocked-service tiers still run. |
| A live-model parent/child smoke requires the same consent. | #10 | Open for the live tier only; a real parent/child turn against a loopback mock now runs in `jev/smoke/` and is recorded in `jev/evidence/`. |
| Every session authenticates to GitHub as one account, so "author ≠ reviewer" cannot be met with a second identity. | all | Open; reviews are recorded as self-review comments backed by reproducible automated checks. |
| The private key for the GitHub-verified commits in this repository is not on this machine. | all | Open; commits are pushed unsigned and GitHub reports them unverified. |
| `repo-checks` `just fmt-check` was red on `main`: `ruff format --check .` reformatted twelve files. | all | Closed by #77 (`a844c9645f`), filed as #69: the eleven ordinary files are now formatted, and `jev/scripts/jev_bus.py` is excluded in `ruff.toml` instead of reformatted so the digest test on the vendored component copy still holds. The `build-test` lane reaches its clean-worktree check again. |
| The merged #23 gate never binds a report to the frozen holdout it was measured on. | #23, #85 | Closed by #88 (`d6f53c5e46`), filed as #85: `gate --split` now binds the report to the frozen split - a report measured outside the declared holdout is refused with `E_HOLDOUT_REQUIRED`, a split edited after freezing is refused as `split_drift` with enforcement disabled, a split missing its binding fields is `E_HOLDOUT_SPLIT`, an unmeasured holdout family yields `permitted: false` with `holdout_incomplete`, and a caller that passes no `--split` keeps the pre-#85 surface but now reports `holdout.checked: false` with the reason. |
| Codespell is red on `main` on three files under `codex-rs/`. | all | Open; inherited, not JEV. The findings are in upstream files carried in by the `openai:main` merge (`tui/src/markdown_render/math_tests.rs`, `tui/src/markdown_render/math/render.rs`, `exec-server/src/no_follow/unix.rs`), so the fix is an ignore entry or an upstream fix, not an integration change. |
| The host path reduces bytes but never items, and passes no view to the carrier. | #55, #17 | Closed by #73 (`00e6b3b434`), filed as #58: `JEV_BUS_VIEW` now names the approved view the launcher forwards, and the bus boundary accepts a shorter array only when the adapter's own report marks exactly the positions the array lost - each unique and in range, matching the length delta - every removed item is standalone assistant prose, and the surviving array is the incoming array minus those positions in order. Anything else (an addition, a reorder, an unaccounted, duplicate or out-of-range position, a removal with no configured view, or a removal of an item carrying a tool call, reasoning, an image or audio) is refused (`item-count`) and the caller's array is returned unchanged; an unapproved removal is reverted and named `unapproved_removal`, while the legitimate dual-application case (dedup and the view dropping the same item) still applies. The host still never decides which prose is approvable - `fabric_views.py` owns that. What is not yet shown is a `real-host-binary` run in which an approved view actually shrinks the outgoing array; the #70 record used no view. |
| The projection boundary had no `real-host-binary` run. | #5, #55, #17 | Closed on a seeded transcript; filed as #70 and recorded in [`evidence/projection-real-host.md`](evidence/projection-real-host.md). A launched host now shows the reduced `input` and the exact reset, but it resumes a rollout whose duplicate pair is already present: this revision exposes no `read`/`read_file`/`file_read` tool, so no run yet shows a host *discovering* its own eligible pair, and there is still no live-provider or token measurement. |
