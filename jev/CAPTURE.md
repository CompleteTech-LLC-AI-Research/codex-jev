# Canonical capture and event correlation

Phase #4 (#13) makes the host's own session transcript the canonical evidence
for the integration. Codex already writes an append-only rollout JSONL under
`$CODEX_HOME/sessions/<year>/<month>/<day>/rollout-*.jsonl` for every session.
`jev/scripts/canonical_capture.py` reads that file - and only that file - and
emits the integration envelope defined by `events.envelope_fields` in
[`compatibility-manifest.json`](compatibility-manifest.json). Capture runs
before any projection, never rewrites the transcript it reads, and writes only
under the isolated environment directory.

This is contract [C1](CONTRACTS.md): capture stores the original bytes, their
SHA-256, and their origin before any outgoing view is transformed.

## What is captured

Two native surfaces carry the same activity, and both are read so nothing is
lost when the host reports an event on only one of them:

| Surface | Record | Classification |
| --- | --- | --- |
| `response_item` | `message` role `user` with `content_item_kinds: ["user.text"]` | `user_message` |
| `response_item` | `message` role `assistant` | `assistant_message` |
| `response_item` | `function_call` / `custom_tool_call` | `tool_call`, or `collab_message` when `namespace == "collaboration"` or the tool is one of `spawn_agent`, `send_message`, `followup_task` |
| `response_item` | `function_call_output` / `custom_tool_call_output` | `tool_result` |
| `event_msg` | `item_completed` with a typed item | the same kinds, from `item_completed` |

A user-role message that carries no `user.text` item is injected context, not
operator speech, and is recorded as a skip. The two surfaces overlap, so the
same session, turn, kind, and content seen twice collapses to one event and
leaves an explicit dedup receipt rather than a second record. A collaboration
call is classified as `collab_message`, but its output still arrives as a tool
result, so call pairing treats both `tool_call` and `collab_message` as calls.

## The store

Everything is written under `<env-dir>/capture/`:

| Path | Contents |
| --- | --- |
| `content/<capture_id>.txt` | The exact canonical bytes, content-addressed by their SHA-256. |
| `events.jsonl` | One envelope event per supported activity, with origin and correlation. |
| `retrieval.jsonl` | The redacted retrieval view, marked untrusted and possibly stale. |
| `duplicates.jsonl` | Dedup receipts: what collapsed, on which surfaces, and onto which event. |
| `gaps.jsonl` | Reported gaps: missing ordinal, repeated ordinal, unparsable or non-object record, orphan tool result. |
| `skipped.jsonl` | Records that are not evidence, with the reason each was skipped. |
| `index.json` | The capture summary, including every source's SHA-256 and tier. |

The retrieval view replaces credential-shaped spans but keeps the canonical
content intact, because canonical content never leaves the isolated store.
Every retrieval record names the rules that matched and carries provenance back
to the source line, and every record states that recalled content is evidence
and never authorization.

## Tiers

A rollout under `jev/tests/` is labelled `rollout-fixture`; anything else is
labelled `real-host-rollout`. The tier travels in the summary and on every
event, so a fixture can never pass as live evidence.

## Commands

```sh
python3 jev/scripts/canonical_capture.py capture --sessions-dir .jev/isolated/home/sessions
python3 jev/scripts/canonical_capture.py status
python3 jev/scripts/canonical_capture.py verify
python3 jev/scripts/canonical_capture.py trace --tool-call-id call-...
python3 jev/scripts/canonical_capture.py retrieval --kind tool_result
```

`capture` reads one or more `--rollout` files, or every rollout under
`--sessions-dir`. `verify` re-derives every claim from the store and the source
rollouts and returns exit code `1` unless all eleven checks hold:

- every event traces to its origin record, by line number and SHA-256;
- stored content matches the recorded hash;
- the envelope carries every declared field;
- every kind is declared by the manifest;
- correlation ids are complete;
- the retrieval view holds no detectable credential;
- the retrieval view is marked untrusted and possibly stale;
- the retrieval view derives from the canonical content;
- repeats do not multiply records;
- tool calls and results are paired;
- no capture gaps.

## Failure and disable behavior

Capture fails closed. A missing isolated environment, a missing rollout, or an
event kind the manifest does not declare raises and writes nothing. Gaps are
never hidden: a missing or repeated ordinal, an unparsable or non-object line,
and an orphaned tool result are each recorded, and any gap fails `verify`.
Removing `capture.canonical_evidence` from a profile, or deleting the isolated
environment, disables capture with no effect on the host transcript.
