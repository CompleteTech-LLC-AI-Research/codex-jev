# Shared event identifiers

Every component in the JEV Codex stack emits bounded, correlated records so one
trace can follow a turn through capture, projection, screening, approval,
authorization, and execution. The point is end-to-end correlation with proof of
origin, not a log aggregator.

OmniRoute is excluded. No event kind below depends on it.

## Envelope: `jev.event.v1`

```json
{
  "schema": "jev.event.v1",
  "event_id": "01J...ULID",
  "kind": "sentinel.pre_tool",
  "occurred_at": "2026-09-21T01:58:30Z",
  "component": "jev-sentinel",
  "host": "codex",
  "session_id": "abc123",
  "turn_id": "turn-7",
  "tool_call_id": "call_x",
  "workspace": "/abs/path/to/workspace",
  "correlation_id": "01J...root",
  "causation_id": "01J...parent",
  "severity": "info",
  "payload_digest": "sha256:...",
  "data": {}
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | Always `jev.event.v1`. |
| `event_id` | yes | Unique, monotonic (ULID/UUIDv7 recommended). |
| `kind` | yes | One of the kinds below. |
| `occurred_at` | yes | RFC 3339 UTC. |
| `component` | yes | Emitting component name from the manifest pins. |
| `host` | yes | `codex` for this integration. |
| `session_id` | yes | Host session identity. |
| `turn_id` | yes | Host turn identity. |
| `tool_call_id` | conditional | Present for pre/post-tool and execution events. |
| `workspace` | yes | Absolute workspace root; distinguishes worktrees. |
| `correlation_id` | yes | Root event id for the trace. |
| `causation_id` | conditional | The event that caused this one, when applicable. |
| `severity` | yes | `info` \| `warn` \| `error`. |
| `payload_digest` | yes | `sha256:` of the canonical payload, so integrity can be checked without storing raw content. |
| `data` | yes | Bounded, redacted payload. Never carries raw secrets. |

## Kinds

| Kind | Emitter | Meaning |
| --- | --- | --- |
| `codex.session.start` | codex-jev | A host session begins; establishes the correlation root. |
| `fabric.capture.canonical` | jev-context-fabric | Canonical evidence recorded **before** any projection, with content hash and origin metadata. |
| `fabric.retrieval.hydrate` | jev-context-fabric | Source-backed excerpt retrieved and hydrated within budget. |
| `bus.projection.plan` | codex-jev | A preview of the combined chain; never mutates the array. |
| `bus.projection.apply` | codex-jev | The chain transformed the outgoing request. |
| `bus.projection.reset` | codex-jev | The outgoing view was reset to canonical. |
| `collab.message.plaintext` | codex-jev | A new inter-agent message is stored/forwarded as plaintext. |
| `sentinel.prompt` | jev-sentinel | Prompt boundary observation. |
| `sentinel.pre_tool` | jev-sentinel | Pre-tool screening; may return REVIEW/BLOCK/QUARANTINE. |
| `sentinel.post_tool` | jev-sentinel | Post-tool observation feeding capture. |
| `sentinel.veto` | jev-sentinel | A terminal veto for a bound action. |
| `approval.preflight.request` | jev-codex-approval | Eligible synchronous review attempt begins a preflight. |
| `approval.preflight.result` | jev-codex-approval | Preflight returned a valid result, deferred, or failed. |
| `host.authorization.decision` | codex-jev | The host's authoritative authorization outcome. |
| `host.execution.result` | codex-jev | The exact action executed or was prevented. |

## Correlation rules

- A trace's `correlation_id` equals the `codex.session.start` event id.
- `causation_id` links a downstream event to the exact upstream event that
  produced it (e.g. `approval.preflight.result` -> `approval.preflight.request`).
- Tool events share one `tool_call_id` across pre-tool, authorization, execution,
  and post-tool.
- Incident records reference source events by `event_id`; they never duplicate
  raw secret-bearing payloads.

## Privacy rules

- `data` carries bounded, normalized fields. Raw commands, raw file contents, and
  secrets stay out.
- `payload_digest` allows integrity verification without storing the payload.
- A capture gap is recorded as an event with an explicit missing-payload marker,
  never silently dropped.
