# Approved, reversible Fabric prose views

This document records the host half of contract `C4` in
[`CONTRACTS.md`](CONTRACTS.md): an approved prose-view plan is bound to the
post-dedup snapshot, preview/apply/reset are host-owned with semantics
equivalent to the package's own, and byte counts and token figures are reported
separately. The component's own stored approval is **not** consulted by the
carrier: an operator who approves through `jev-context-fabric`'s own CLI changes
nothing at the boundary, because a component's claim is never taken on its own
word (`C3`). It backs issue
[#17](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/17) and the
stage-200 half of contract `C2`.

## What an approved view is

Stage 200 (`jev-context-fabric.view`, priority 200) may drop **standalone
assistant prose** from the outgoing request to save context. It is the second
half of the projection boundary described in
[`BUS_BOUNDARY.md`](BUS_BOUNDARY.md); stage 100
([`DEDUP_RECEIPTS.md`](DEDUP_RECEIPTS.md)) runs first, so the view is applied to
the post-dedup array.

The host owns the control in `jev/scripts/fabric_views.py`. Its semantics mirror
the `jev-context-fabric` model (`paging.plan` / `apply` / `reset`), but they are
the host's, for the host's own wire `ResponseItem` array:

| Step | Behavior |
| --- | --- |
| `plan` | Previews which assistant-prose messages are eligible and the bytes they would save. **Never** mutates the request. |
| `apply` | Turns a plan into an approved view **bound to the exact snapshot** it was previewed against. Approval is an explicit act; a plan is never applied implicitly. |
| `reset` | Discards the view. Nothing else is stored, so the original bytes return exactly. |
| `filter` | Drops the approved messages; refuses (removes nothing) when the array no longer matches the bound snapshot, or when the turn was cancelled. |

## What is eligible

Only a `message` whose role is `assistant` and whose content is **text-only** may
leave the view. A message that carries a tool call, reasoning, or any non-text
part is not prose and is never eligible, so tool and compaction structure is
preserved. A message inside the current user turn or the recent tail is
protected, and a message that states a constraint (`must`, `never`,
`requirement`, `unresolved`, `blocker`, `security`, `permission`, `decision`,
`do not`, `don't`) is never eligible.

## What the host refuses

| Refusal | Meaning |
| --- | --- |
| `view_not_approved` | `apply` was called without explicit approval. |
| `not_a_plan` / `empty_plan` | the plan is malformed, or has no eligible message. |
| `stale_plan` / `stale_view` | the array changed since the plan was previewed / the view was approved. Nothing is removed. |
| `not_a_view` | the object is not an approved view. |

A removal without an approved view is **reverted** — the removed item is restored
from the original array and the boundary records `unapproved_removal` — so a
stage can never shrink the outgoing request on its own authority.

## Metrics

Savings are reported by `fabric_views.metrics` and surfaced as
`report["view_metrics"]`:

- `bytes_before`, `bytes_after`, `bytes_removed` — **measured** from the
  serialized array;
- `tokens_measured` — a real token count only when the host supplies a counter,
  otherwise `null`;
- `tokens_estimated` — a byte-derived figure, **explicitly labelled** as an
  estimate and never conflated with a measurement;
- `native_compaction_called` — always `false`: the view never invokes compaction
  and never removes reasoning/compaction items, so native compaction keeps
  working.

## Evidence

- **`offline-fixture`** — `jev/tests/test_fabric_views.py` and the required-CI
  suite `.github/scripts/test_jev_views.py` drive the control directly and
  through the boundary with a deterministic stage double.
- **`real-host`** — the script-level transport run recorded on the #17 PR shows
  the boundary applying an approved view over real subprocess stage doubles,
  reporting measured bytes and a separate token estimate, while the canonical
  request stays byte-identical.

Live-provider evidence is never produced here: remote inference stays disabled
and unbudgeted.
