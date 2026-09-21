# jev-bus on Codex: the single native transformation boundary

`jev-context-fabric` and `jev-prune-kit` already share the jev-bus v1 contract for
carrier hosts (`pi`, `opencode`, `hermes`). Codex exposes no outgoing-request
transform hook, so on this host the boundary is native: the fork implements the
carrier, and the two Python packages run as ordered stages behind it.

The stage protocol itself is unchanged. This document records only what Codex adds,
because a second owner of this boundary would break invariant 3 in
`architecture.md`.

## Boundary

The native adapter sits at final request construction, after history is assembled
and before the payload leaves the process. It is the only writer of outgoing
message content.

```
assembled history ──▶ [native carrier]
                        ├── stage 100  jev-prune.dedup   (claims: tool-result:read)
                        ├── stage 200  jev-context.view  (claims: assistant-prose,
                        │                                 message-remove, system-append)
                        └── passthrough if the chain cannot be resolved
                     ──▶ outgoing payload
```

## Wire protocol

One JSON object in, one JSON object out, over stdio, matching jev-bus v1:

```json
{ "schema": "jev-bus.stage.v1", "op": "transform",
  "host": "codex", "api": "native",
  "session": "...", "workspace": "...", "goal": "...",
  "accepts_system_append": true,
  "messages": [], "original_messages": [], "notes": [] }
```

```json
{ "ok": true, "messages": [], "system_append": "",
  "notes": [{ "stage": "...", "action": "...", "count": 0, "bytes": 0, "detail": "..." }] }
```

`original_messages` is the pristine pre-chain array. The carrier passes the same
`original_messages` to every stage, so a stage that keys messages by position stays
valid after an upstream stage edits the array.

## Ordering

Priority, not install order, decides: 100 before 200, ties broken by stage name.
Dedup substitutes bodies without changing the array length, while the prose view
removes messages; running dedup first is what keeps the view's positional keys
valid. Enabling the view without dedup is refused by the manifest validator rather
than silently reordered.

## Bounds and failure behaviour

| Condition | Behaviour |
| --- | --- |
| Stage exits non-zero, times out, or answers with an unrecognised shape | The stage contributes nothing and the chain continues with that stage's own input. |
| The chain cannot be resolved (registry missing, malformed, claim conflict) | Passthrough with a `chain-refused` note. |
| Total chain deadline exceeded | Remaining stages are skipped; the current message array is sent. |
| The native adapter itself cannot run | The unmodified assembled history is sent. |

A stage may decline. It may never break the host's turn, and it may never change
authorization: a token budget, an approval, or a recalled excerpt never removes a
review requirement.

## What this document does not claim

Nothing here has been validated against a live provider. The offline tests exercise
the carrier and stage mechanics against fixtures; the real-host and live-provider
evidence is tracked separately and is not implied by a green test run.
