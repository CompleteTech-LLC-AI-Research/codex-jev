#!/usr/bin/env python3
"""Canonical capture envelopes and event correlation (contract C1).

The integration manifest declares the envelope fields, the canonical event
kinds, and the projection stages; this repository owns that schema and every
other component emits envelopes against it. This module implements the schema
for the host:

* ``envelope_from_payload`` turns one documented Codex lifecycle-hook payload
  into an envelope-v1 record. The host is the only place that sees ``turn_id``
  and ``tool_use_id``, so the correlation identifiers are derived here and not
  guessed from the captured content.
* ``CaptureLog`` keeps canonical bytes and their SHA-256, collapses a repeated
  delivery of the same event onto the first record, and resolves
  ``parent_event_id`` against the records already stored.
* ``validate_stream`` reports duplicates, unsupported events, and capture gaps
  (a result body without its call, an assistant turn without its prompt) as
  findings instead of inventing event kinds the manifest does not define.
* ``retrieval_view_findings`` checks the acceptance criterion that a secret
  present in the canonical bytes never reaches the retrieval view.

Subcommands:

``mappings``   print the hook-event and component-kind mappings;
``validate``   validate an envelope stream (JSONL or a capture log);
``correlate``  report parent links, duplicates, and capture gaps.

Exit codes: 0 = ok, 1 = validation failure, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ENVELOPE_VERSION = 1
CAPTURE_RECORD_VERSION = 1
COMPONENT = "codex-jev"

EXPECTED_FIELDS = (
    "event_id",
    "parent_event_id",
    "session_id",
    "turn_id",
    "tool_call_id",
    "component",
    "stage",
    "kind",
    "origin_workspace",
    "capture_id",
    "occurred_at_ms",
    "redaction",
)
EXPECTED_KINDS = (
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "collab_message",
    "sentinel_incident",
    "approval_judgment",
    "projection_receipt",
)
EXPECTED_STAGES = {100: "dedup", 200: "fabric_view"}

# Kinds this layer captures from the host itself. The remaining kinds are
# emitted by components in later phases and validated, not produced, here.
CAPTURED_KINDS = (
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "collab_message",
)

# Documented Codex lifecycle hooks -> canonical kinds. A hook that is not a
# captured event (session start/end, compaction, interrupts, permission
# requests) returns ``None`` and is recorded as a skipped observation, so a
# capture gap is visible instead of being silently dropped.
HOOK_EVENT_KIND = {
    "UserPromptSubmit": "user_message",
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "Stop": "assistant_message",
    "SubagentStop": "collab_message",
}

# The context fabric records its own kinds in the same capture, so the host
# maps them into the canonical vocabulary before correlating.
COMPONENT_KIND = {
    "USER_INPUT": "user_message",
    "PRE_MODEL": "user_message",
    "PRE_TOOL": "tool_call",
    "POST_TOOL": "tool_result",
    "TURN_END": "assistant_message",
    "SUBAGENT_END": "collab_message",
}

TOOL_KINDS = ("tool_call", "tool_result")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_./+=-]{12,}"),
)


class EnvelopeError(Exception):
    """The envelope contract is unavailable or inconsistent with the manifest."""


def repository_root():
    return Path(__file__).resolve().parents[2]


def load_event_contract(root=None):
    """Read the envelope contract from the manifest and fail closed on drift."""
    root = Path(root) if root else repository_root()
    manifest_path = root / "jev" / "compatibility-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    events = manifest.get("events")
    if not isinstance(events, dict):
        raise EnvelopeError(f"{manifest_path} declares no events contract")
    if events.get("envelope_version") != ENVELOPE_VERSION:
        raise EnvelopeError(
            f"envelope version {events.get('envelope_version')!r} is not "
            f"{ENVELOPE_VERSION}"
        )
    fields = tuple(events.get("envelope_fields", ()))
    if fields != EXPECTED_FIELDS:
        raise EnvelopeError(
            "manifest envelope fields drifted from the host schema: "
            f"{fields!r} != {EXPECTED_FIELDS!r}"
        )
    kinds = tuple(events.get("kinds", ()))
    if kinds != EXPECTED_KINDS:
        raise EnvelopeError(
            f"manifest kinds drifted from the host schema: {kinds!r} != "
            f"{EXPECTED_KINDS!r}"
        )
    stages = {
        int(stage["order"]): str(stage["id"])
        for stage in events.get("stages", ())
        if isinstance(stage, dict) and "order" in stage and "id" in stage
    }
    if stages != EXPECTED_STAGES:
        raise EnvelopeError(
            f"manifest stages drifted from the host schema: {stages!r} != "
            f"{EXPECTED_STAGES!r}"
        )
    return {
        "envelope_version": ENVELOPE_VERSION,
        "envelope_fields": fields,
        "kinds": kinds,
        "stages": stages,
        "correlation": events.get("correlation"),
    }


def canonical(value):
    """The canonical JSON encoding shared with the components."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    import hashlib

    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def event_content(payload):
    """The canonical content this host event contributes as evidence.

    Shaped to match what the host hands the components for the same hook, so a
    later join between the two captures compares like with like.
    """
    hook_event_name = payload.get("hook_event_name")
    if hook_event_name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        return prompt if isinstance(prompt, str) and prompt else None
    if hook_event_name == "PreToolUse":
        return canonical(
            {
                "tool": payload.get("tool_name"),
                "input": payload.get("tool_input"),
            }
        )
    if hook_event_name == "PostToolUse":
        return canonical(
            {
                "tool": payload.get("tool_name"),
                "input": payload.get("tool_input"),
                "result": payload.get("tool_response"),
            }
        )
    if hook_event_name == "Stop":
        message = payload.get("last_assistant_message")
        return message if isinstance(message, str) and message else None
    if hook_event_name == "SubagentStop":
        return canonical(
            {
                "agent_id": payload.get("agent_id"),
                "agent_type": payload.get("agent_type"),
                "message": payload.get("last_assistant_message"),
            }
        )
    return None


def event_id_for(session_id, turn_id, tool_call_id, kind, capture_id):
    """Deterministic identity: a retried delivery collapses onto one record."""
    return digest(
        canonical(
            [
                session_id or "",
                turn_id or "",
                tool_call_id or "",
                kind,
                capture_id,
            ]
        )
    )


def envelope_from_payload(payload, occurred_at_ms=None):
    """Build an envelope-v1 record from one host lifecycle-hook payload.

    Returns ``None`` for an unsupported or incomplete observation. Missing
    correlation identity is never repaired by guessing: an event without a
    session, or a tool event without its ``tool_use_id``, is not captured.
    """
    if not isinstance(payload, dict):
        return None
    hook_event_name = payload.get("hook_event_name")
    kind = HOOK_EVENT_KIND.get(hook_event_name)
    if kind is None:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    turn_id = (
        payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
    )
    if not turn_id:
        return None
    tool_call_id = None
    if kind in TOOL_KINDS:
        candidate = payload.get("tool_use_id")
        if not isinstance(candidate, str) or not candidate:
            return None
        tool_call_id = candidate
    content = event_content(payload)
    if content is None:
        return None
    capture_id = digest(content)
    workspace = payload.get("cwd")
    return {
        "event_id": event_id_for(session_id, turn_id, tool_call_id, kind, capture_id),
        "parent_event_id": None,
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "component": COMPONENT,
        "stage": None,
        "kind": kind,
        "origin_workspace": str(Path(workspace).resolve()) if workspace else None,
        "capture_id": capture_id,
        "occurred_at_ms": int(
            occurred_at_ms if occurred_at_ms is not None else time.time() * 1000
        ),
        "redaction": {
            "policy": "jev-context-fabric/redact",
            "canonical": False,
            "retrieval_view": True,
            "canonical_sha256": capture_id,
        },
    }


def validate_envelope(envelope, contract=None):
    """Return the findings for one envelope; an empty list means conforming."""
    contract = contract or load_event_contract()
    findings = []
    if not isinstance(envelope, dict):
        return ["E_ENVELOPE_SCHEMA: envelope must be a JSON object"]
    unknown = sorted(set(envelope) - set(contract["envelope_fields"]))
    if unknown:
        findings.append(
            f"E_ENVELOPE_SCHEMA: unsupported envelope fields: {', '.join(unknown)}"
        )
    missing = [field for field in contract["envelope_fields"] if field not in envelope]
    if missing:
        findings.append(
            f"E_ENVELOPE_SCHEMA: missing envelope fields: {', '.join(missing)}"
        )
        return findings
    if envelope["kind"] not in contract["kinds"]:
        findings.append(f"E_ENVELOPE_KIND: unknown kind {envelope['kind']!r}")
    if envelope["stage"] is not None:
        if envelope["stage"] not in contract["stages"]:
            findings.append(
                f"E_ENVELOPE_STAGE: unknown projection stage {envelope['stage']!r}"
            )
    if not isinstance(envelope["session_id"], str) or not envelope["session_id"]:
        findings.append("E_ENVELOPE_IDENTITY: session_id must be a nonempty string")
    for field in ("turn_id", "tool_call_id", "event_id", "capture_id"):
        value = envelope[field]
        if value is not None and not isinstance(value, str):
            findings.append(f"E_ENVELOPE_IDENTITY: {field} must be a string or null")
    if envelope["parent_event_id"] is not None and (
        not isinstance(envelope["parent_event_id"], str)
    ):
        findings.append("E_ENVELOPE_IDENTITY: parent_event_id must be a string or null")
    if envelope["kind"] in TOOL_KINDS and not envelope["tool_call_id"]:
        findings.append(
            f"E_ENVELOPE_CORRELATION: {envelope['kind']} needs a tool_call_id"
        )
    if envelope["kind"] in CAPTURED_KINDS and not envelope["turn_id"]:
        findings.append(f"E_ENVELOPE_CORRELATION: {envelope['kind']} needs a turn_id")
    if not isinstance(envelope["occurred_at_ms"], int):
        findings.append(
            "E_ENVELOPE_SCHEMA: occurred_at_ms must be an integer of milliseconds"
        )
    redaction = envelope["redaction"]
    if not isinstance(redaction, dict) or not redaction.get("policy"):
        findings.append("E_ENVELOPE_REDACTION: redaction must name a policy")
    elif redaction.get("canonical") is not False:
        findings.append(
            "E_ENVELOPE_REDACTION: redaction must never apply to canonical bytes"
        )
    return findings


def _records_of(document):
    """Accept a capture log, a JSONL stream, or a list of records."""
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        if isinstance(document.get("records"), list):
            return document["records"]
        if "envelope" in document:
            return [document]
        raise EnvelopeError("document has no 'records' or 'envelope' key")
    raise EnvelopeError("document must be a JSON object or array")


def load_stream(path):
    """Read a capture stream: either one JSON document or a JSONL log.

    A capture log is append-only JSONL, so it is not a single JSON value; both
    shapes are accepted here and normalized to a list of records.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise EnvelopeError(f"{path} holds no capture records") from error
        try:
            return [json.loads(line) for line in lines]
        except json.JSONDecodeError as line_error:
            raise EnvelopeError(
                f"{path} is neither a JSON document nor a JSONL capture log: "
                f"{line_error}"
            ) from line_error
    return _records_of(document)


def _envelopes(records):
    envelopes = []
    for record in records:
        if not isinstance(record, dict):
            continue
        envelope = record.get("envelope") if "envelope" in record else record
        if isinstance(envelope, dict) and envelope.get("kind"):
            envelopes.append(envelope)
    return envelopes


def correlate(envelopes):
    """Report parent links and capture gaps for one session's envelopes.

    Parent resolution is exactly the correlation the host can prove: a result
    body belongs to its own tool call, and an assistant or collaboration
    message belongs to the user turn that produced it. Anything else stays
    unlinked and is reported, never inferred.
    """
    calls = {}
    prompts = {}
    report = {"parents": {}, "gaps": [], "orphans": []}
    for envelope in envelopes:
        kind = envelope.get("kind")
        if kind == "tool_call":
            calls[(envelope.get("session_id"), envelope.get("tool_call_id"))] = envelope
            continue
        if kind == "user_message":
            prompts[(envelope.get("session_id"), envelope.get("turn_id"))] = envelope
            continue
        if kind == "tool_result":
            parent = calls.get(
                (envelope.get("session_id"), envelope.get("tool_call_id"))
            )
            if parent is None:
                report["gaps"].append(
                    {
                        "code": "W_CAPTURE_MISSING_TOOL_CALL",
                        "event_id": envelope.get("event_id"),
                        "tool_call_id": envelope.get("tool_call_id"),
                        "detail": "a tool result was captured without its tool call",
                    }
                )
            else:
                report["parents"][envelope["event_id"]] = parent["event_id"]
            continue
        if kind in ("assistant_message", "collab_message"):
            parent = prompts.get((envelope.get("session_id"), envelope.get("turn_id")))
            if parent is None:
                report["gaps"].append(
                    {
                        "code": "W_CAPTURE_MISSING_PROMPT",
                        "event_id": envelope.get("event_id"),
                        "turn_id": envelope.get("turn_id"),
                        "detail": (
                            "a message was captured without the user turn that "
                            "produced it"
                        ),
                    }
                )
            else:
                report["parents"][envelope["event_id"]] = parent["event_id"]
            continue
        report["orphans"].append(
            {
                "event_id": envelope.get("event_id"),
                "kind": kind,
                "detail": "kind is emitted by a component, not correlated here",
            }
        )
    return report


def validate_stream(document, contract=None):
    """Validate a whole capture stream: schema, duplicates, gaps, correlation."""
    contract = contract or load_event_contract()
    records = _records_of(document)
    findings = []
    seen = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(f"E_CAPTURE_SCHEMA: record {index} is not an object")
            continue
        skipped = record.get("skipped")
        if isinstance(skipped, dict):
            findings.append(
                "W_CAPTURE_UNSUPPORTED_EVENT: "
                f"{skipped.get('hook_event_name')!r} "
                f"({skipped.get('reason')})"
            )
            continue
        envelope = record.get("envelope") if "envelope" in record else record
        for finding in validate_envelope(envelope, contract):
            findings.append(finding)
        if not isinstance(envelope, dict):
            continue
        previous = seen.get(envelope.get("event_id"))
        if previous is not None:
            if previous.get("capture_id") != envelope.get("capture_id"):
                findings.append(
                    "E_CAPTURE_DUPLICATE_RECORD: one event id carries two "
                    f"different capture ids ({envelope.get('event_id')})"
                )
            else:
                findings.append(
                    "W_CAPTURE_REPEATED_RECORD: "
                    f"{envelope.get('event_id')} was stored twice"
                )
        seen[envelope.get("event_id")] = envelope
    for gap in correlate(_envelopes(records))["gaps"]:
        findings.append(f"{gap['code']}: {gap['detail']} ({gap['event_id']})")
    return findings


def retrieval_view_findings(canonical_text, view_text, secrets=()):
    """Check that the retrieval view never republishes canonical secret text."""
    findings = []
    for secret in secrets:
        if secret and secret in canonical_text and secret in view_text:
            findings.append(
                "E_CAPTURE_SECRET_IN_VIEW: the retrieval view still contains a "
                f"planted secret of length {len(secret)}"
            )
    for pattern in SECRET_PATTERNS:
        match = pattern.search(view_text)
        if match:
            findings.append(
                "E_CAPTURE_SECRET_IN_VIEW: the retrieval view matches "
                f"{pattern.pattern!r} at offset {match.start()}"
            )
    return findings


class CaptureLog:
    """Append-only canonical capture store with deterministic deduplication."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.is_file():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def index(self):
        """Existing identity -> event id, so a parent can be resolved on append."""
        index = {"calls": {}, "prompts": {}}
        for envelope in _envelopes(self.read()):
            if envelope.get("kind") == "tool_call":
                key = (envelope.get("session_id"), envelope.get("tool_call_id"))
                index["calls"][key] = envelope["event_id"]
            elif envelope.get("kind") == "user_message":
                key = (envelope.get("session_id"), envelope.get("turn_id"))
                index["prompts"][key] = envelope["event_id"]
        return index

    def append(self, envelope, raw=None, content=None):
        """Store one envelope; a repeated delivery is collapsed, not multiplied."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        index = self.index()
        if envelope.get("kind") == "tool_result":
            key = (envelope.get("session_id"), envelope.get("tool_call_id"))
            parent = index["calls"].get(key)
            if parent:
                envelope["parent_event_id"] = parent
        elif envelope.get("kind") in ("assistant_message", "collab_message"):
            key = (envelope.get("session_id"), envelope.get("turn_id"))
            parent = index["prompts"].get(key)
            if parent:
                envelope["parent_event_id"] = parent
        for record in self.read():
            existing = record.get("envelope")
            if isinstance(existing, dict) and existing.get("event_id") == envelope.get(
                "event_id"
            ):
                return {"stored": False, "reason": "duplicate", "record": record}
        record = {
            "record_version": CAPTURE_RECORD_VERSION,
            "envelope": envelope,
            "content": content,
            "content_sha256": envelope.get("capture_id"),
        }
        if raw is not None:
            record["origin"] = {
                "source": "host-hook",
                "hook_event_name": envelope.get("kind"),
                "raw_sha256": digest(raw),
                "raw_bytes": len(raw),
            }
            record["raw"] = raw
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
            stream.flush()
        return {"stored": True, "reason": "appended", "record": record}

    def append_skipped(self, hook_event_name, reason):
        """Record why an observation produced no envelope, so gaps stay visible."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {
            "record_version": CAPTURE_RECORD_VERSION,
            "skipped": {
                "hook_event_name": hook_event_name,
                "reason": reason,
                "occurred_at_ms": int(time.time() * 1000),
            },
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical(record) + "\n")
            stream.flush()
        return record


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="repository root holding jev/")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("mappings", help="print the hook-event mappings")
    for name in ("validate", "correlate"):
        sub = subparsers.add_parser(name, help=f"{name} a capture stream")
        sub.add_argument("path", help="capture log or envelope JSONL file")
    return parser.parse_args(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(argv)
    except SystemExit as exit_error:
        return 2 if exit_error.code else 0
    try:
        if args.command == "mappings":
            print(
                canonical({"hook_events": HOOK_EVENT_KIND, "component": COMPONENT_KIND})
            )
            return 0
        records = load_stream(args.path)
        if args.command == "validate":
            findings = validate_stream(
                {"records": records}, load_event_contract(args.root)
            )
            for finding in findings:
                print(finding)
            errors = [item for item in findings if item.startswith("E_")]
            print(f"{len(findings)} finding(s), {len(errors)} error(s)")
            return 1 if errors else 0
        envelopes = _envelopes(records)
        report = correlate(envelopes)
        print(
            canonical(
                {
                    "envelopes": len(envelopes),
                    "parents": len(report["parents"]),
                    "gaps": report["gaps"],
                }
            )
        )
        return 1 if report["gaps"] else 0
    except EnvelopeError as error:
        print(f"event envelope contract error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot read capture stream: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
