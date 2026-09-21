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
- Host implementation: `jev/scripts/event_envelope.py` (schema, dedup,
  correlation, gaps) and `jev/scripts/capture_hook.py` (the lifecycle-hook
  adapter); see [`CANONICAL_CAPTURE.md`](CANONICAL_CAPTURE.md).

## C2 — One native adapter owns the jev-bus transformation boundary

The request-construction site in the host calls exactly one bus owner, in order:

| Stage | Order | Owner | Failure behavior |
| --- | --- | --- | --- |
| duplicate-read dedup | 100 | `jev-prune-kit` | Return the stage input unchanged. |
| approved Fabric view | 200 | `jev-context-fabric` | Return the stage input unchanged. |

Unsupported message shapes pass through untouched. The canonical transcript is
never mutated; only the outgoing request payload is replaced.

## C3 — Duplicate-read receipts

A projection may replace an older read body only when the request arguments and
the result body match a later retained copy, the current user turn and the
recent tail are protected, and the source and witness hashes validate against
native identities. Everything else — stale receipts, changed arguments, missing
witnesses, concurrent results — keeps the original content. Projection is
idempotent and never removes unique evidence.

## C4 — Approved views are reversible

An approved prose-view plan is bound to the post-dedup snapshot and is rejected
if that snapshot changed. Preview, apply, and reset use the existing package
approval semantics. Native compaction keeps working; byte counts and measured
tokens are reported separately and never conflated.

## C5 — Sentinel veto precedence

Sentinel observes prompt, pre-tool, and post-tool boundaries and starts in local
shadow mode. It reports exactly which tools and event paths are covered;
installation alone is never reported as activation. A configured veto prevents
the exact action before execution, cannot be cleared by a later approval, and
latches for the subsequent action. Host permissions and sandbox enforcement
remain in force, and unsupported output replacement or bypass surfaces are
documented rather than assumed away.

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
order is unique and recorded. Disabling the integration means building the
pinned base without patches and running the baseline profile: the original
behavior is reproduced rather than approximated.

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
