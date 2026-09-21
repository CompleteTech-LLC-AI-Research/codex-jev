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

`content/` is content-addressed and append-only: a file is written once, keyed by
the SHA-256 of the bytes it holds, and never rewritten. A capture is a function of
the rollouts it was given, so narrowing the rollout set leaves the bytes of the
events the store no longer lists in place. Those orphans are never deleted -
deleting them would destroy the only stored copy of evidence whose originating
rollout is no longer named - and are reported instead: `status` returns
`content_files_orphaned` and `orphaned_content_files`, and `verify` returns
`orphaned_content_files`. Orphan reporting is not one of the eleven checks, so it
never changes `verify`'s `ok`.

A file that an event refers to but that is *absent* is the opposite case, and is
not an orphan: it fails `stored_content_matches_the_recorded_hash` and
`retrieval_view_derives_from_canonical_content`, and `verify` reports those
results rather than aborting.

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

`verify` reports an empty store honestly instead of hiding it behind one message:

- no capture has ever run: `nothing captured yet at <store>; run capture first`;
- the store index records events but `events.jsonl` holds none: `the store index
  records <n> event(s) but events.jsonl holds none: the store is incomplete, so
  nothing can be re-derived; re-run capture`. A truncated store is never
  described as a transcript that carried no evidence. `status` reports the same
  condition as `store_incomplete`;
- a store file is unreadable - a line of `events.jsonl` is not valid JSON - the
  read names the file and line: `<file> line <n> is not valid JSON (<reason>):
  the store is corrupt or incomplete, so nothing can be re-derived; re-run
  capture`. A corrupt store is never a Python traceback;
- a capture ran, the transcript yielded no evidence, and no gap was recorded:
  `no evidence was captured: the store holds no events and recorded no gap`;
- a capture ran, the transcript yielded no events, and a gap was recorded:
  `verify` runs all eleven checks, reports the gap through `no_capture_gaps:
  false` and the recorded gap in `gaps`, and explains the outcome in
  `empty_capture_note`.

## Coverage reporting

`capture` records what it actually read, so a narrowed invocation is visible
rather than assumed. When a sessions directory was searched, the summary carries
`sessions_dir: {directory, rollouts_matched}`; when the pattern matched nothing
but `--rollout` files were also given, those files are still captured and the
summary adds a note saying so. An invocation that names no transcript at all
fails with `no rollout-*.jsonl under <dir>: the rollout-*.jsonl pattern matched
nothing and no --rollout was given`, naming the directory searched and the
pattern used instead of reporting a missing flag.

## Failure and disable behavior

Capture fails closed. A missing isolated environment, a missing rollout, or an
event kind the manifest does not declare raises and writes nothing. Gaps are
never hidden: a missing or repeated ordinal, an unparsable or non-object line,
and an orphaned tool result are each recorded, and any gap fails `verify`.
`rollout_paths` itself reports an empty match as an empty list, so a caller that
already has rollouts is never refused by an empty directory. Capture never
deletes store content: the canonical bytes are written once and the orphans of a
narrowed capture set are reported, as described above.
Removing `capture.canonical_evidence` from a profile, or deleting the isolated
environment, disables capture with no effect on the host transcript.
