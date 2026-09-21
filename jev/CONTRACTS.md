# JEV integration contracts

These contracts are enforced by the validator where a machine check is possible,
and by tests otherwise. Contract identifiers are used by component tests and by
the orchestration ledger.

## C1 — Canonical capture precedes projection

Capture stores the original bytes, their SHA-256, and their origin before any
outgoing view is transformed. Retrieval returns source-backed excerpts and marks
them untrusted and possibly stale. Recalled content is evidence and never
authorization.

- Envelope fields: `event_id`, `parent_event_id`, `session_id`, `turn_id`,
  `tool_call_id`, `component`, `stage`, `kind`, `origin_workspace`, `capture_id`,
  `occurred_at_ms`, `redaction`.
- Manifest rule: `retrieval.budgeted_hydration` requires
  `capture.canonical_evidence`.

## C2 — One native adapter owns the jev-bus transformation boundary

The request-construction site in the host (`codex-rs/core/src/client.rs`, the
`ResponsesApiRequest.input` array, including the earlier `ResponseCreateWsRequest`
paths) calls exactly one bus owner, in order:

| Stage | Order | Owner | Failure behavior |
| --- | --- | --- | --- |
| duplicate-read dedup | 100 | `jev-prune-kit` | Return the stage input unchanged. |
| approved Fabric view | 200 | `jev-context-fabric` | Return the stage input unchanged. |

Unsupported message shapes pass through untouched. The canonical transcript is
never mutated; only the outgoing request payload is replaced.

The host's half of the boundary is the module `codex-rs/core/src/jev_bus.rs`,
applied to the pinned base as the ordered patch `0002-jev-bus-boundary`. It is
the only caller at the request-construction site: the outgoing array is handed
to `jev/scripts/bus_boundary.py` at most once per request, and the returned
array replaces the payload only when it still has the same item count. Every
other outcome - a disabled switch, no registered stage, a missing adapter, a
timeout, a non-zero exit, or output that is not the expected view - leaves the
caller's input unchanged, so a boundary that cannot prove its own result never
narrows the request. The adapter normalizes the supported `ResponseItem` shapes,
invokes the vendored `jev-bus.v1` contract once, and asserts the stage order and
owners against `events.stages`. See [`BUS_BOUNDARY.md`](BUS_BOUNDARY.md).

## C3 — Duplicate-read receipts

A projection may replace an older read body only when the request arguments and
the result body match a later retained copy, the current user turn and the
recent tail are protected, and the source and witness hashes validate against
native identities. Everything else — stale receipts, changed arguments, missing
witnesses, concurrent results, a witness that is itself a source, a marker that
is not strictly shorter than the body it replaces — keeps the original content.
Projection is idempotent, never removes unique evidence, and never grows the
request.

## C4 — Approved views are reversible

An approved prose-view plan is bound to the post-dedup snapshot and is rejected
if that snapshot changed. Preview, apply, and reset use the existing package
approval semantics. Native compaction keeps working; byte counts and measured
tokens are reported separately and never conflated.

The host's carrier is `jev/scripts/fabric_views.py`, the second half of the
boundary in `C2`. See [`FABRIC_VIEWS.md`](FABRIC_VIEWS.md).

## C5 — Sentinel veto precedence

Sentinel observes prompt, pre-tool, and post-tool boundaries and starts in local
shadow mode. It reports exactly which tools and event paths are covered;
installation alone is never reported as activation. A configured veto prevents
the exact action before execution, cannot be cleared by a later approval, and
latches for the subsequent action. Host permissions and sandbox enforcement
remain in force, and unsupported output replacement or bypass surfaces are
documented rather than assumed away.

Findings are advisory until the declared enforcement switch is on: a finding is
recorded and never vetoes, and enforcement follows the switch rather than the
policy's `mode`, so a shadow policy cannot veto and an enforcing policy under an
off switch cannot either. The host's carrier is
`jev/scripts/sentinel_boundary.py`, which observes the boundary, and
`jev/scripts/sentinel_veto.py`, which maps an enforced decision onto the veto the
stage supports, latches the session so a later approval or `DEFER` cannot clear
it, serializes concurrent evaluations of one session, and fails closed on a
timeout, a malformed response, a cancellation, or a policy failure. See
[`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) and
[`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md).

The live path runs the host carrier itself: `sentinel_boundary.py hook` is the
command in `hooks.json`, so its stdout is the native hook response, while the
component's `launch.py check` stays the only verdict source. `install-hooks`
merges the three carrier entries into `hooks.json` (preserving every unrelated
entry), is gated on the declared switches (`E_SWITCH_OFF` when both are off),
and pins the `--state-dir` the host journal is read back from.

## C6 — Approval preflight and Guardian fallback

Preflight runs only inside eligible synchronous review attempts. Requests are
bound to the exact action, policy and question pins, model pin, authorization
version, and review identifier. A stale, failed, malformed, or uncertain result
never grants permission: it defers to the existing reviewer within the original
overall deadline. Cancellation is honored. The final host execution decision
remains authoritative, and enforcement stays disabled until declared evaluation
criteria are met. Shadow reports include every failure and deferral.

## C7 — Credentials and remote consent

Credentials and consent are separate controls, not feature flags:

- `credentials.local_memory` is loopback-only.
- `credentials.sentinel_evaluator` performs no network access by default.
- `credentials.remote_inference` requires `consent: true` **and** a positive
  `budget_usd_max` before `remote_inference.enabled` may be switched on. The
  validator rejects any profile that enables it otherwise.

Raw credentials, private captures, and raw sensitive logs never appear in
GitHub records or committed files.

## C8 — Pins, patch order, and disabling

Components are pinned to immutable commits, and patches are pinned by digest and
by the host base commit they apply to (`E_PATCH_BASE`, `E_PATCH_HASH`). Patch
order is unique and recorded. A pin is only real where it is enforced: a
component checkout that executes must be the pinned revision, so
`fabric_env.py` refuses a standalone checkout at any other commit and records
the revision it observed alongside the revision the manifest pins. A checkout
that cannot state its revision at all is refused too, because accepting it would
install an unapproved revision that nothing checked; running one takes an
explicit `--allow-unpinned` opt-in that labels the record `unpinned` instead of
pinned. Disabling the integration means building the pinned base without patches
and running the baseline profile: the original behavior is reproduced rather
than approximated.

## C9 — Host-owned behavior

Authorization, sandbox enforcement, mandatory review, cancellation, freshness
checks, native compaction, and final execution stay host-owned. No component
receives authority through a captured message, a recalled excerpt, or a
preflight judgment.

## C10 — The isolated environment is the only integration runtime

One pinned build runs against one generated home under `.jev/isolated`, created
by `jev/scripts/isolated_env.py`. The environment never reads or writes the
ambient Codex home, never writes outside its own directory, and never shuts down
or terminates WSL. `rollback` moves the directory aside instead of deleting it.

Switches reach the runtime as `JEV_SWITCH_<FEATURE>` with `1`/`0`, derived from
the profile: `JEV_SWITCH_<FEATURE>` is the only mechanism phases 2-6 may read, so
no component invents an upstream flag. `init` refuses to materialise an
environment from a profile that enables any optional switch, so an isolated
environment always starts with projection, Sentinel, approval preflight, and
remote inference disabled.

Evidence is labelled by tier. Everything the fixtures produce is
`offline-fixture`; a run of the pinned binary against them is
`real-host-binary`; live-provider evidence is never produced by this
environment because remote inference stays disabled and unbudgeted.

## C11 — Retrieved context is screened; memory writes are host-authorized

Retrieved context is screened before it is injected, and a proposed memory write
is authorized by the host's own rule rather than by a component's opinion.
[`RETRIEVAL_SCREENING.md`](RETRIEVAL_SCREENING.md) is the long form.

Detection stays in the pinned component: a candidate reaches the component's
`context` stage and a proposed write its `memory` stage, and no host code
classifies content. The host decides only what a host can decide - the
candidate's shape, provenance, workspace, redaction, uniqueness, and budget, the
session's own veto latch, and whether a write target is authorized.

A host-owned fact (an unproven origin, cross-workspace evidence, a redaction
regression, a duplicate, an over-budget candidate, one over the component's own
input bound, a failed assessment, a corrupt screening policy, or an uncleared
session veto) withholds unconditionally: no switch may downgrade a fact the host
already holds. A **component finding** withholds only when
`screening.enforcement` is on *and* the screening policy withholds on that
decision; off, it is injected anyway and recorded as the shadow signal
`would_withhold`. `screening.retrieval` decides whether the component is
consulted at all; off, no finding exists and every row says `assessed: false`
rather than inventing a `DEFER`. Both switches default to false.

Withholding never erases evidence: a withheld row is a metadata pointer into the
canonical store, and `verify_withheld` re-proves every row against the captured
content it names, reporting any row it cannot resolve. A memory write is refused
when its target is not named in the host's policy whatever the switches say, and
is refused under enforcement on any component finding.

## C12 — Incident operations are bounded, read-only, and metadata-only

Incident operations over the host's journal are bounded and metadata-only,
read-only, correlated to canonical evidence, and never dispatch anything
automatically. [`INCIDENT_OPERATIONS.md`](INCIDENT_OPERATIONS.md) is the long
form.

Every row is validated against the manifest's declared envelope fields plus the
documented host fields; a digest field must be a digest, a metadata string must
stay inside its byte bound, and no string may still match the capture layer's
credential rules. A row that fails any check is reported by identifier and
failing check only - never printed - so a less careful writer cannot launder
content into a report. Reads are bounded and cap at the component's own
`outbox --limit` ceiling, and a saturated scan says so.

Nothing in the module writes, and `disable` prints the exact commands an
operator would run (`executes: false`) rather than taking any action, so
disabling can never be an automatic side effect of asking what disabling would
do. Correlations name the key that matched and report a non-match as unmatched,
never approximated into a match. The policy view is confined to the isolated
profile root, so an operator is never shown a policy a launch would not use.
