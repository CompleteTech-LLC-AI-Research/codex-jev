# Canonical capture and event correlation

Phase #4 (#13) implements contract C1 for the host: the original bytes of every
captured event, their SHA-256, and their origin are stored **before** any
component projects context into an outgoing request. `jev/scripts/event_envelope.py`
owns the envelope schema and the correlation rules; `jev/scripts/capture_hook.py`
is the lifecycle-hook adapter that the isolated profile installs ahead of every
component hook.

## The envelope

The manifest declares the schema, and the host reads it from there instead of
duplicating it. `event_envelope.load_event_contract()` fails closed
(`EnvelopeError`) when the version, the field list, the kind list, or a
projection stage no longer matches the host's schema, so schema drift is a
test/validation failure rather than a silent format change.

| Field | Meaning |
| --- | --- |
| `event_id` | Deterministic identity of the observation, derived from the correlation identity and the content digest. |
| `parent_event_id` | The event this one provably belongs to, resolved when the record is appended; `null` when the parent was not captured. |
| `session_id`, `turn_id`, `tool_call_id` | Correlation identifiers taken from the host payload. |
| `component` | `codex-jev` for host-captured events; components emit their own value. |
| `stage` | `null` for raw capture; `100` (dedup) and `200` (fabric view) are projection stages owned by phase #5. |
| `kind` | Canonical kind, from the manifest's `kinds` list. |
| `origin_workspace` | Resolved working directory the event came from. |
| `capture_id` | SHA-256 of the canonical content bytes. |
| `occurred_at_ms` | Host clock, milliseconds since the epoch. |
| `redaction` | The policy that applies to derived views, never to canonical bytes. |

## Which host events are captured

The adapter only captures events that carry complete correlation identity. The
Codex lifecycle payloads for `SessionStart`, `SessionEnd`, `PreCompact`,
`PostCompact`, `PermissionRequest`, `Interrupt`, and `SubagentStart` either have
no `turn_id` or no captured content, so they are recorded as **skipped**
observations instead of being assigned an invented identity.

| Hook event | Kind | Content |
| --- | --- | --- |
| `UserPromptSubmit` | `user_message` | `prompt` |
| `PreToolUse` | `tool_call` | `{tool, input}` |
| `PostToolUse` | `tool_result` | `{tool, input, result}` |
| `Stop` | `assistant_message` | `last_assistant_message` |
| `SubagentStop` | `collab_message` | `{agent_id, agent_type, message}` |

`envelope_from_payload` returns `None` - never a guess - when the event name is
not captured, `session_id`/`turn_id` is missing, a tool event has no
`tool_use_id`, or the content is empty. A component's own kinds (`USER_INPUT`,
`PRE_TOOL`, `POST_TOOL`, `TURN_END`, `SUBAGENT_END`, `PRE_MODEL`) map into the
same vocabulary through `COMPONENT_KIND` so one stream can be validated as a
whole.

## Correlation

Parent links are the only relationships the host can prove, and nothing else is
inferred:

| Child kind | Parent |
| --- | --- |
| `tool_result` | The `tool_call` with the same `session_id` and `tool_call_id`. |
| `assistant_message`, `collab_message` | The `user_message` with the same `session_id` and `turn_id`. |

Kinds emitted by components are reported as `orphans`: validated, not
correlated here. A record whose parent is missing is still stored - with
`parent_event_id` left `null` - and the gap is reported rather than repaired:

| Code | Meaning |
| --- | --- |
| `W_CAPTURE_MISSING_TOOL_CALL` | A result body was captured without its call. |
| `W_CAPTURE_MISSING_PROMPT` | A message was captured without the user turn that produced it. |
| `W_CAPTURE_UNSUPPORTED_EVENT` | An observation produced no envelope; the reason is stored with it. |

## Deduplication and origin metadata

`event_id` is a SHA-256 over the correlation identity, the kind, and the content
digest, so a retried delivery of the same event is byte-identical and collapses
onto the first record. `CaptureLog.append` returns
`{"stored": false, "reason": "duplicate"}` and writes nothing; the log is
append-only JSONL, and each record keeps the envelope, the canonical content,
`content_sha256`, and - for host hooks - `origin.raw_sha256`, `origin.raw_bytes`
and the raw payload, alongside the source (`host-hook`).

Two different contents can never share one `event_id`; if that ever happened,
`validate_stream` reports `E_CAPTURE_DUPLICATE_RECORD` instead of accepting it.

## Redaction and the retrieval view

Canonical bytes are never redacted: the record keeps the original content and
its digest, and `redaction.canonical` is asserted to be `false`. The retrieval
view is the redacted projection built by a later phase, and the acceptance
criterion is checked by `retrieval_view_findings`: a planted secret, and any
credential shape (`sk-…`, `ghp_…`, `AKIA…`, `Bearer …`, private-key blocks),
that survives into the view is reported as `E_CAPTURE_SECRET_IN_VIEW`.

## The adapter

`capture_hook.py` runs as the first handler for every captured event. It reads
one hook payload as JSON on stdin, writes the envelope, and then:

1. never writes to stdout, so it can never inject context or a decision;
2. never modifies the payload or the transcript;
3. exits `0` even when it cannot capture - a capture problem must not become a
   host failure - recording the reason (`unparseable_payload`, `event_mismatch`,
   `missing_hook_event_name`, `unsupported_or_incomplete_event`) in the log.

The capture directory comes from `--capture-dir`, then `JEV_CAPTURE_DIR`, then
`$CODEX_HOME/capture`; nothing is written outside it.

## Commands

```
python3 jev/scripts/capture_hook.py --event UserPromptSubmit < payload.json
python3 jev/scripts/event_envelope.py mappings
python3 jev/scripts/event_envelope.py validate <capture-log.jsonl>
python3 jev/scripts/event_envelope.py correlate <capture-log.jsonl>
```

`validate` exits `1` on any `E_` finding; `correlate` exits `1` when a capture
gap is reported.

## Failure and disable behavior

- The contract is read from the manifest: a drifted manifest fails
  `load_event_contract` closed, and `validate_stream` reports it per envelope.
- A `hooks.json` the driver did not write (not JSON, or not an object) is never
  rewritten: bind raises `FabricError`, install writes no record, and the file
  keeps its exact bytes. `status` reports it under `capture.hooks_error`.
- Removing the adapter removes only its own groups; every component hook and
  unrelated group survives (`fabric_env.py uninstall`).
- Deleting the capture directory removes no host state: the log is a derived
  artifact inside `.jev/isolated`, and rollback moves the whole environment
  aside.

## Hook trust (host gate)

This is a host restriction, not a capture defect, and it is the one thing that
keeps a real host from running the adapter today.

The hooks engine loads `hooks.json` from the Codex config folder, and a handler
runs only when it is enabled **and** the source is trusted:

```
enabled && (bypass_hook_trust || trust_status in {Managed, Trusted})
```

`hooks.json` is a user-layer source, so its handlers start `Untrusted` and the
engine skips them. Trust is a per-handler `trusted_hash` recorded in the user
layer (`hooks.state` in `config.toml`), keyed by
`file:<config-folder>/hooks.json:<event>:<group>:<handler>`, and it must equal
the hash of the normalized handler or the status becomes `Modified` (still
skipped). The alternatives are `--dangerously-bypass-hook-trust` or
`bypass_hook_trust = true` in the configuration, both of which explicitly
declare that the hook source is already vetted.

Consequences, stated plainly:

- The binding, ordering, dedup, and correlation above are proven against the
  installed adapter (`fabric_env.py verify` → `host_capture_*` checks).
- On a real host **the adapter will not be invoked** until the operator trusts
  those handlers or launches with the bypass. Provisioning that trust is not
  part of this phase: writing a trust hash requires reproducing the host's
  normalized-handler TOML hashing, and a wrong hash silently yields `Modified`.
  It is tracked as a live-host validation item (phase #8, #25) instead of being
  approximated here.

## Evidence tiers and limits

| Tier | Source |
| --- | --- |
| `offline-fixture` | `.github/scripts/test_jev_capture.py`, driving synthetic payloads through the adapter and the store. |
| `real-host, mocked service` | The pinned binary run through `.jev/isolated/bin/codex-isolated`, whose provider is the loopback fixture service. |
| live provider | Never used here. |

Known limits:

- Only the five events above carry enough identity to be captured; the rest are
  recorded as skipped observations.
- Per the trust gate, no real session has yet produced a host-hook record. The
  checks in `verify` replay the adapter as the host would call it.
- The retrieval view and its budget belong to phase #5 (#14); this phase proves
  the canonical side and the secret-absence check they will be measured against.
