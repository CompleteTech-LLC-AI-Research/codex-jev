# Event identifiers and correlation

Every component emits the `jev.event.v1` envelope (`events.v1.json`). The envelope
exists so that one lifecycle can be reconstructed from records written by six
different processes, and so a retried capture or projection is recognisable instead
of silently multiplying rows.

## Envelope

| Field | Meaning |
| --- | --- |
| `event_id` | Deterministic identity, derived as below. |
| `stage` | One of the eight stages in `events.v1.json`. |
| `source` | The component that emitted the event. |
| `session_id` | Host session identity. A child session has its own value; the correlation to its parent is carried by `cause_event_id`. |
| `turn_id` | Host turn identity inside the session. |
| `sequence` | Monotonic counter per `(session_id, stage)`; disambiguates repeated events in one turn. |
| `tool_call_id` | Present for tool-scoped events (dedup, pre-tool, post-tool, execution). |
| `cause_event_id` | The event that caused this one; this is how a projected payload points back to the capture it pruned. |
| `content_sha256` | SHA-256 of the canonical bytes the event describes, when the event describes content. |
| `content_bytes` | Byte length of those canonical bytes. Bytes are never reported as tokens. |
| `decision` | Present when the stage produced a decision; values are listed in `events.v1.json`. |
| `redaction` | Redaction state of any content referenced by the event. |

## Identity derivation

```
event_id = sha256_hex(NUL.join([session_id, turn_id, tool_call_id, stage, sequence, content_sha256]))
dedup_key = "session_id|turn_id|tool_call_id|stage"
```

An empty field is the empty string, never a placeholder such as `null`. Two
identical derivations are the same event: a retried capture is idempotent by
construction. Two different derivations are different events, so a genuine repeat
with changed content is retained rather than collapsed.

## Rules

1. Capture events are emitted before any projection event for the same content.
2. A projection event carries `cause_event_id` pointing at the capture it used; a
   projection that cannot name its cause is not emitted.
3. Incident and approval records are metadata-only: they carry identities, hashes,
   and decisions, and never raw secret-bearing payloads.
4. `redaction` never changes what is stored canonically; it records what a retrieval
   or incident view withheld.
5. Session, turn, and tool-call identifiers come from the host. A component may not
   invent an identity for content produced by the host; when the host supplies none,
   the field is empty and the missing identity is recorded as a capture gap.
