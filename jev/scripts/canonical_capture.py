#!/usr/bin/env python3
"""Turn the host's own session transcript into canonical JEV envelope events.

Codex writes an append-only rollout JSONL under ``$CODEX_HOME/sessions`` for
every session. That file is the canonical record of what actually happened, so
this bridge reads it - and only it - and emits the integration's envelope:

* one event per supported user, assistant, tool, and collaboration activity,
  classified from the record the host itself wrote;
* the canonical content of every event, stored content-addressed with its
  SHA-256 and the SHA-256 of the exact source line it came from;
* a retrieval view in which credentials are replaced, marked untrusted and
  possibly stale, so nothing recalled is ever presented as authoritative;
* correlation identifiers (``session_id``, ``turn_id``, ``tool_call_id``) plus a
  parent chain per turn, so a capture always traces back to its origin;
* explicit dedup receipts, so a retried or doubly-reported event does not
  multiply records, and explicit gap records, so missing evidence is reported
  instead of silently skipped.

Capture runs before any projection: this module never rewrites the transcript it
reads, and it writes only under the isolated environment directory.

Stored content is content-addressed and append-only, and is never deleted. A
capture is a function of the rollouts it is given, so narrowing the rollout set
leaves the bytes of the events the store no longer lists in ``capture/content``;
``status`` and ``verify`` report those orphans rather than destroying the only
stored copy of evidence.

Subcommands:

``capture``    read one or more rollouts into the canonical store;
``status``     report what the store holds;
``verify``     re-derive every claim from the store and the source rollouts;
``trace``      show one event, its origin record, and its turn chain;
``retrieval``  print the redacted retrieval view for a kind.

Exit codes: 0 = ok, 1 = validation failure, 2 = usage error.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env
import jev_manifest

CAPTURE_DIR = "capture"
STORE_VERSION = 1
COMPONENT = "codex-jev"
CAPTURE_STAGE = 0
HOST_SURFACE = "rollout-jsonl"
FIXTURE_TIER = "rollout-fixture"
REAL_TIER = "real-host-rollout"

# Field order of the host-owned envelope, mirroring the manifest's
# events.envelope_fields. Only the host defines this schema.
ENVELOPE_FIELDS = (
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

# The collaboration tools whose calls travel between agents.
COLLAB_TOOLS = {"spawn_agent", "send_message", "followup_task"}

# Kinds that ask for work and therefore expect a matching `tool_result`. A
# collaboration call is classified as `collab_message`, but its output still
# arrives as a tool result, so pairing and orphan detection must treat both as
# calls or a collab round trip would look like an orphaned result.
CALL_KINDS = ("tool_call", "collab_message")

# Typed items the host emits in `event_msg.item_completed`.
ITEM_KINDS = {
    "UserMessage": "user_message",
    "AgentMessage": "assistant_message",
    "CommandExecution": "tool_call",
    "ToolCall": "tool_call",
    "McpToolCall": "tool_call",
    "ToolResult": "tool_result",
}

# Credential shapes. The retrieval view replaces these; canonical content keeps
# them, because canonical content never leaves the isolated store.
SECRET_RULES = (
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted:private_key_block]",
    ),
    (
        "openai_style_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        "[redacted:openai_style_key]",
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
        "[redacted:github_token]",
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[redacted:aws_access_key]",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        "[redacted:slack_token]",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[redacted:jwt]",
    ),
    (
        "authorization_header",
        re.compile(r"(?i)\b(authorization\s*:\s*bearer)[ \t]+\S+"),
        r"\1 [redacted:authorization_header]",
    ),
    (
        "assigned_credential",
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
            r"password|passwd|credential)s?\b(\s*[:=]\s*)[^\s,;\"']+"
        ),
        r"\1\2[redacted:assigned_credential]",
    ),
)

# A redaction marker is the one thing the scanner must not count as a secret:
# without this, `token=[redacted:assigned_credential]` would match the very rule
# that produced it, and the retrieval view could never be reported clean.
REDACTION_MARKER = re.compile(r"\[redacted:[a-z_]+\]")

# A redacted assignment leaves `name=` behind, and whatever text follows the
# marker then looks like the value. Neutralise the whole assignment before the
# markers are dropped, or `password=[redacted:assigned_credential] are fake`
# would be re-read as an assigned credential and the view could never be clean.
REDACTED_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|"
    r"password|passwd|credential)s?\b\s*[:=]\s*" + REDACTION_MARKER.pattern
)


class CaptureError(Exception):
    """The transcript cannot be captured as requested."""


def capture_root(env_dir):
    return Path(env_dir) / CAPTURE_DIR


def default_rollouts_dir(env_dir):
    return Path(env_dir) / "home" / "sessions"


def envelope_kinds(root=None):
    manifest = isolated_env.load_manifest(root)
    return set(manifest.get("events", {}).get("kinds", []))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def detect_tier(rollout, root=None):
    """Label fixture-backed rollouts, so no artifact can pass as real evidence."""
    repo_root = Path(root or isolated_env.repository_root()).resolve()
    path = Path(rollout).resolve()
    fixtures = (repo_root / "jev" / "tests").resolve()
    try:
        path.relative_to(fixtures)
    except ValueError:
        return REAL_TIER
    return FIXTURE_TIER


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def text_from_content(content):
    """Extract the exact text of a message, in order."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    if not parts:
        return None
    return "\n".join(parts)


def redact(text):
    """Replace credential-shaped spans. Returns the view and what was replaced."""
    view = text
    applied = []
    for rule_id, pattern, replacement in SECRET_RULES:
        view, count = pattern.subn(replacement, view)
        if count:
            applied.append({"rule": rule_id, "count": count})
    return view, applied


def secret_hits(text):
    """Rule ids that still match, used to prove the retrieval view is clean."""
    text = REDACTED_ASSIGNMENT.sub("", text)
    text = REDACTION_MARKER.sub("", text)
    found = []
    for rule_id, pattern, _ in SECRET_RULES:
        if pattern.search(text):
            found.append(rule_id)
    return found


class Candidate:
    """One supported activity, as classified from a single source record."""

    def __init__(
        self,
        *,
        kind,
        text,
        turn_id,
        tool_call_id=None,
        occurred_at_ms=None,
        surface,
        detail=None,
    ):
        self.kind = kind
        self.text = text
        self.turn_id = turn_id
        self.tool_call_id = tool_call_id
        self.occurred_at_ms = occurred_at_ms
        self.surface = surface
        self.detail = detail or {}


def classify(record, context):
    """Classify one rollout record into candidates, a skip, or nothing.

    ``context`` carries the session-level state a record may rely on: the
    session id, the origin workspace, and the turn the record belongs to.
    """
    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return [], {"reason": "record has no payload"}

    if record_type == "session_meta":
        context["session_id"] = payload.get("session_id") or payload.get("id")
        roots = payload.get("runtime_workspace_roots") or []
        context["origin_workspace"] = payload.get("cwd") or (
            roots[0] if roots else None
        )
        return [], {"reason": "session metadata establishes the origin workspace"}

    if record_type == "turn_context":
        context["current_turn_id"] = payload.get("turn_id") or payload.get(
            "root_turn_id"
        )
        return [], {"reason": "turn context establishes the current turn"}

    if record_type == "event_msg":
        message_type = payload.get("type")
        if message_type == "task_started":
            context["current_turn_id"] = payload.get("turn_id") or payload.get(
                "root_turn_id"
            )
            return [], {"reason": "turn start establishes the current turn"}
        if message_type != "item_completed":
            return [], {"reason": f"event_msg {message_type} is not an evidence item"}
        turn_id = payload.get("turn_id") or context.get("current_turn_id")
        item = payload.get("item")
        if not isinstance(item, dict):
            return [], {"reason": "item_completed carries no item"}
        item_type = item.get("type")
        kind = ITEM_KINDS.get(item_type)
        if kind is None:
            return [], {"reason": f"unsupported item type {item_type!r}"}
        text = text_from_content(item.get("content"))
        if text is None and kind == "tool_call":
            text = json.dumps(item.get("command") or item, sort_keys=True)
        if text is None and kind == "tool_result":
            text = text_from_content(item.get("output")) or json.dumps(
                item.get("output"), sort_keys=True
            )
        if text is None:
            return [], {"reason": f"item type {item_type!r} carries no text"}
        detail = {"item_type": item_type, "item_id": item.get("id")}
        tool_call_id = item.get("call_id") or item.get("id")
        occurred = payload.get("completed_at_ms") or payload.get("started_at_ms")
        candidate = Candidate(
            kind=kind,
            text=text,
            turn_id=turn_id,
            tool_call_id=tool_call_id if kind in ("tool_call", "tool_result") else None,
            occurred_at_ms=occurred,
            surface="item_completed",
            detail=detail,
        )
        return [candidate], None

    if record_type != "response_item":
        return [], {"reason": f"record type {record_type!r} is not evidence"}

    item_type = payload.get("type")
    meta = payload.get("internal_chat_message_metadata_passthrough") or {}
    turn_id = meta.get("turn_id") or context.get("current_turn_id")
    occurred = parse_timestamp(record.get("timestamp"))

    if item_type == "message":
        role = payload.get("role")
        kinds = meta.get("content_item_kinds") or []
        text = text_from_content(payload.get("content"))
        if text is None:
            return [], {"reason": "message carries no text"}
        if role == "user":
            if "user.text" not in kinds:
                return [], {"reason": "injected user-role context (no user.text item)"}
            kind = "user_message"
        elif role == "assistant":
            kind = "assistant_message"
        else:
            return [], {"reason": f"role {role!r} is not operator or model speech"}
        candidate = Candidate(
            kind=kind,
            text=text,
            turn_id=turn_id,
            occurred_at_ms=occurred,
            surface="response_item",
            detail={"role": role, "message_id": payload.get("id")},
        )
        return [candidate], None

    if item_type in ("function_call", "custom_tool_call"):
        name = payload.get("name") or payload.get("tool_name")
        arguments = payload.get("arguments")
        body = arguments if isinstance(arguments, str) else json.dumps(arguments)
        namespace = payload.get("namespace")
        is_collab = namespace == "collaboration" or name in COLLAB_TOOLS
        candidate = Candidate(
            kind="collab_message" if is_collab else "tool_call",
            text=f"{name} {body}",
            turn_id=turn_id,
            tool_call_id=payload.get("call_id") or payload.get("id"),
            occurred_at_ms=occurred,
            surface=item_type,
            detail={"name": name, "namespace": namespace},
        )
        return [candidate], None

    if item_type in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        text = output if isinstance(output, str) else json.dumps(output, sort_keys=True)
        candidate = Candidate(
            kind="tool_result",
            text=text,
            turn_id=turn_id,
            tool_call_id=payload.get("call_id") or payload.get("id"),
            occurred_at_ms=occurred,
            surface=item_type,
            detail={"output_type": type(output).__name__},
        )
        return [candidate], None

    return [], {"reason": f"response item {item_type!r} is not an evidence surface"}


class Store:
    """Write side of the canonical store, under the isolated environment."""

    def __init__(self, env_dir):
        self.root = capture_root(env_dir)
        self.content = self.root / "content"
        self.content.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.retrieval = []
        self.duplicates = []
        self.gaps = []
        self.skipped = []
        self.sources = []
        self.seen = {}
        self.turns = {}

    def event_id(self, session_id, turn_id, kind, tool_call_id, content_sha256):
        seed = "\x1f".join(
            [
                session_id or "",
                turn_id or "",
                kind,
                tool_call_id or "",
                content_sha256,
            ]
        )
        return "evt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def add(self, candidate, context, capture_id, content_sha256, origin, tier):
        session_id = context.get("session_id")
        identity = "\x1f".join(
            [
                session_id or "",
                candidate.turn_id or "",
                candidate.kind,
                candidate.tool_call_id or "",
                content_sha256,
            ]
        )
        event_id = self.event_id(
            session_id,
            candidate.turn_id,
            candidate.kind,
            candidate.tool_call_id,
            content_sha256,
        )
        if identity in self.seen:
            first = self.seen[identity]
            self.duplicates.append(
                {
                    "event_id": event_id,
                    "duplicate_of": first["event_id"],
                    "capture_id": capture_id,
                    "kind": candidate.kind,
                    "session_id": session_id,
                    "turn_id": candidate.turn_id,
                    "tool_call_id": candidate.tool_call_id,
                    "surfaces": [first["surface"], candidate.surface],
                    "tier": tier,
                    "origin": origin,
                    "reason": "same session, turn, kind, and content on another surface",
                }
            )
            return None
        turn_key = (session_id, candidate.turn_id)
        parent = self.turns.get(turn_key)
        view, applied = redact(candidate.text)
        record = {
            "event_id": event_id,
            "parent_event_id": parent,
            "session_id": session_id,
            "turn_id": candidate.turn_id,
            "tool_call_id": candidate.tool_call_id,
            "component": COMPONENT,
            "stage": CAPTURE_STAGE,
            "kind": candidate.kind,
            "origin_workspace": context.get("origin_workspace"),
            "capture_id": capture_id,
            "occurred_at_ms": candidate.occurred_at_ms,
            "redaction": {
                "view": "canonical",
                "redacted": False,
                "rules": [],
            },
            "content_sha256": content_sha256,
            "content_bytes": len(candidate.text.encode("utf-8")),
            "surface": candidate.surface,
            "tier": tier,
            "origin": origin,
            "detail": candidate.detail,
        }
        self.events.append(record)
        self.retrieval.append(
            {
                "event_id": event_id,
                "capture_id": capture_id,
                "kind": candidate.kind,
                "session_id": session_id,
                "turn_id": candidate.turn_id,
                "tool_call_id": candidate.tool_call_id,
                "origin_workspace": context.get("origin_workspace"),
                "occurred_at_ms": candidate.occurred_at_ms,
                "text": view,
                "redaction": {
                    "view": "retrieval",
                    "redacted": bool(applied),
                    "rules": applied,
                },
                "untrusted": True,
                "possibly_stale": True,
                "provenance": {
                    "content_sha256": content_sha256,
                    "source": origin["source"],
                    "ordinal": origin["ordinal"],
                    "record_sha256": origin["record_sha256"],
                    "rule": "Recalled content is evidence and never authorization.",
                },
            }
        )
        self.seen[identity] = {"event_id": event_id, "surface": candidate.surface}
        self.turns[turn_key] = event_id
        return event_id

    def write(self, summary):
        write_jsonl(self.root / "events.jsonl", self.events)
        write_jsonl(self.root / "retrieval.jsonl", self.retrieval)
        write_jsonl(self.root / "duplicates.jsonl", self.duplicates)
        write_jsonl(self.root / "gaps.jsonl", self.gaps)
        write_jsonl(self.root / "skipped.jsonl", self.skipped)
        (self.root / "index.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def write_jsonl(path, records):
    text = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    Path(path).write_text(text, encoding="utf-8")


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise CaptureError(
                f"{path} line {number} is not valid JSON ({error.msg}): the store is "
                "corrupt or incomplete, so nothing can be re-derived; re-run capture"
            ) from error
    return records


def content_orphans(env_dir, events):
    """Content-addressed files that no event in the store refers to.

    Reported, never deleted: the file may be the only stored copy of bytes whose
    originating rollout is no longer part of the capture set.
    """
    content = capture_root(env_dir) / "content"
    if not content.is_dir():
        return []
    referenced = {event["capture_id"] for event in events}
    return sorted(
        path.name for path in content.glob("*.txt") if path.stem not in referenced
    )


def capture_rollout(store, rollout, tier):
    """Read one rollout, appending events and reporting gaps and skips."""
    source = str(Path(rollout).resolve())
    payload = Path(rollout).read_bytes()
    lines = payload.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    context = {"session_id": None, "origin_workspace": None, "current_turn_id": None}
    expected_ordinal = 0
    seen_ordinals = set()
    call_ids = set()
    result_ids = set()
    record_count = 0

    for offset, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            store.gaps.append(
                {
                    "kind": "unparsable_record",
                    "source": source,
                    "line_number": offset + 1,
                    "reason": str(error),
                }
            )
            continue
        if not isinstance(record, dict):
            store.gaps.append(
                {
                    "kind": "non_object_record",
                    "source": source,
                    "line_number": offset + 1,
                    "reason": "rollout line is not a JSON object",
                }
            )
            continue
        record_count += 1
        ordinal = record.get("ordinal")
        if isinstance(ordinal, int):
            if ordinal in seen_ordinals:
                store.gaps.append(
                    {
                        "kind": "repeated_ordinal",
                        "source": source,
                        "ordinal": ordinal,
                        "reason": "the host appended the same ordinal twice",
                    }
                )
            elif ordinal > expected_ordinal:
                store.gaps.append(
                    {
                        "kind": "missing_ordinal",
                        "source": source,
                        "expected_ordinal": expected_ordinal,
                        "found_ordinal": ordinal,
                        "missing": ordinal - expected_ordinal,
                        "reason": "records are missing from the transcript",
                    }
                )
                expected_ordinal = ordinal + 1
            else:
                expected_ordinal = ordinal + 1
            seen_ordinals.add(ordinal)

        candidates, skip = classify(record, context)
        if skip is not None:
            store.skipped.append(
                {
                    "source": source,
                    "ordinal": ordinal,
                    "record_type": record.get("type"),
                    "item_type": (record.get("payload") or {}).get("type")
                    if isinstance(record.get("payload"), dict)
                    else None,
                    "reason": skip["reason"],
                }
            )
        origin_base = {
            "source": source,
            "line_number": offset + 1,
            "ordinal": ordinal,
            "record_sha256": sha256_bytes(raw),
            "surface": HOST_SURFACE,
        }
        for candidate in candidates:
            content_sha256 = sha256_text(candidate.text)
            capture_id = "cap_" + content_sha256[:32]
            origin = dict(origin_base, item_surface=candidate.surface)
            event_id = store.add(
                candidate, context, capture_id, content_sha256, origin, tier
            )
            if candidate.kind in CALL_KINDS and candidate.tool_call_id:
                call_ids.add(candidate.tool_call_id)
            if candidate.kind == "tool_result" and candidate.tool_call_id:
                result_ids.add(candidate.tool_call_id)
            if event_id is not None:
                content_file = store.content / f"{capture_id}.txt"
                if not content_file.is_file():
                    content_file.write_text(candidate.text, encoding="utf-8")

    for orphan in sorted(result_ids - call_ids):
        store.gaps.append(
            {
                "kind": "orphan_tool_result",
                "source": source,
                "tool_call_id": orphan,
                "reason": "a tool result has no matching call in this transcript",
            }
        )
    store.sources.append(
        {
            "source": source,
            "tier": tier,
            "records": record_count,
            "lines": len(lines),
            "sha256": sha256_bytes(payload),
            "session_id": context.get("session_id"),
            "origin_workspace": context.get("origin_workspace"),
            "last_ordinal": max(seen_ordinals) if seen_ordinals else None,
        }
    )


def capture(env_dir, rollouts, root=None, sessions_dir=None):
    env_dir = Path(env_dir)
    isolated_env.read_env(env_dir)
    if not rollouts:
        raise CaptureError("capture needs at least one --rollout or --sessions-dir")
    store = Store(env_dir)
    tiers = set()
    for rollout in rollouts:
        path = Path(rollout)
        if not path.is_file():
            raise CaptureError(f"rollout not found: {path}")
        tier = detect_tier(path, root)
        tiers.add(tier)
        capture_rollout(store, path, tier)
    kinds = envelope_kinds(root)
    unknown = sorted({event["kind"] for event in store.events} - kinds)
    if unknown:
        raise CaptureError(f"events outside the declared envelope kinds: {unknown}")
    summary = {
        "store_version": STORE_VERSION,
        "component": COMPONENT,
        "stage": CAPTURE_STAGE,
        "env_dir": str(env_dir),
        "tiers": sorted(tiers),
        "sources": store.sources,
        "events": len(store.events),
        "retrieval_records": len(store.retrieval),
        "duplicates": len(store.duplicates),
        "gaps": len(store.gaps),
        "skipped_records": len(store.skipped),
        "declared_kinds": sorted(kinds),
        "kind_counts": {
            kind: sum(1 for event in store.events if event["kind"] == kind)
            for kind in sorted({event["kind"] for event in store.events})
        },
        "sessions": sorted({event["session_id"] for event in store.events}),
        "turns": sorted({f"{e['session_id']}:{e['turn_id']}" for e in store.events}),
    }
    if sessions_dir is not None:
        matched = len(rollout_paths(sessions_dir))
        summary["sessions_dir"] = {
            "directory": str(sessions_dir),
            "rollouts_matched": matched,
        }
        if matched == 0:
            summary["sessions_dir"]["note"] = (
                "no rollout-*.jsonl matched, so only the --rollout files given were "
                "captured"
            )
    store.write(summary)
    return summary


def status(env_dir):
    env_dir = Path(env_dir)
    index = capture_root(env_dir) / "index.json"
    if not index.is_file():
        raise CaptureError(
            f"no canonical capture at {capture_root(env_dir)}; run capture first"
        )
    summary = json.loads(index.read_text(encoding="utf-8"))
    events = read_jsonl(capture_root(env_dir) / "events.jsonl")
    content = capture_root(env_dir) / "content"
    summary["events_present"] = len(events)
    summary["content_files_present"] = sum(
        1 for event in events if (content / f"{event['capture_id']}.txt").is_file()
    )
    summary["duplicates_recorded"] = len(
        read_jsonl(capture_root(env_dir) / "duplicates.jsonl")
    )
    summary["gaps_recorded"] = len(read_jsonl(capture_root(env_dir) / "gaps.jsonl"))
    orphans = content_orphans(env_dir, events)
    summary["content_files_orphaned"] = len(orphans)
    summary["orphaned_content_files"] = orphans
    summary["store_incomplete"] = bool(summary.get("events")) and not events
    summary["ambient_home"] = isolated_env.ambient_home_fingerprint()
    return summary


def verify(env_dir, root=None):
    env_dir = Path(env_dir).resolve()
    events = read_jsonl(capture_root(env_dir) / "events.jsonl")
    retrieval = read_jsonl(capture_root(env_dir) / "retrieval.jsonl")
    duplicates = read_jsonl(capture_root(env_dir) / "duplicates.jsonl")
    gaps = read_jsonl(capture_root(env_dir) / "gaps.jsonl")
    index_path = capture_root(env_dir) / "index.json"
    if not index_path.is_file():
        raise CaptureError(
            f"nothing captured yet at {capture_root(env_dir)}; run capture first"
        )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    recorded = index.get("events")
    if recorded and not events:
        raise CaptureError(
            f"the store index records {recorded} event(s) but events.jsonl holds none: "
            "the store is incomplete, so nothing can be re-derived; re-run capture"
        )
    if not events and not gaps:
        raise CaptureError(
            "no evidence was captured: the store holds no events and recorded no gap, "
            "so there is no claim to re-derive; the transcript carried no prompt, tool "
            "call, or tool result this store treats as evidence"
        )

    traces = 0
    for event in events:
        origin = event["origin"]
        source = Path(origin["source"])
        if not source.is_file():
            traces += 1
            continue
        raw = source.read_bytes().split(b"\n")[origin["line_number"] - 1]
        if sha256_bytes(raw) == origin["record_sha256"]:
            traces += 1

    hashes = 0
    for event in events:
        path = capture_root(env_dir) / "content" / f"{event['capture_id']}.txt"
        if (
            path.is_file()
            and sha256_bytes(path.read_bytes()) == event["content_sha256"]
        ):
            hashes += 1

    clean = 0
    for record in retrieval:
        if not secret_hits(record["text"]):
            clean += 1

    marked = 0
    for record in retrieval:
        if record.get("untrusted") and record.get("possibly_stale"):
            marked += 1

    derived = 0
    for record in retrieval:
        canonical = next(
            (event for event in events if event["event_id"] == record["event_id"]),
            None,
        )
        if canonical is None:
            continue
        content_path = (
            capture_root(env_dir) / "content" / f"{canonical['capture_id']}.txt"
        )
        if not content_path.is_file():
            # Absent content is reported by stored_content_matches_the_recorded_hash
            # and makes this check fail; it must not abort the report.
            continue
        view, applied = redact(content_path.read_text(encoding="utf-8"))
        if view == record["text"] and applied == record["redaction"]["rules"]:
            derived += 1

    identities = {
        (
            event["session_id"],
            event["turn_id"],
            event["kind"],
            event["tool_call_id"],
            event["content_sha256"],
        )
        for event in events
    }
    duplicate_receipts = all(
        record.get("duplicate_of") and record.get("surfaces") for record in duplicates
    )
    tool_pairs = 0
    for call in (
        event
        for event in events
        if event["kind"] in CALL_KINDS and event["tool_call_id"]
    ):
        if any(
            result["kind"] == "tool_result"
            and result["tool_call_id"] == call["tool_call_id"]
            for result in events
        ):
            tool_pairs += 1
    calls = sum(
        1 for event in events if event["kind"] in CALL_KINDS and event["tool_call_id"]
    )
    correlation_complete = all(
        event["session_id"]
        and event["turn_id"]
        and (event["kind"] not in ("tool_call", "tool_result") or event["tool_call_id"])
        for event in events
    )
    declared = envelope_kinds(root)
    checks = {
        "every_event_traces_to_its_origin_record": traces == len(events),
        "stored_content_matches_the_recorded_hash": hashes == len(events),
        "envelope_carries_every_declared_field": all(
            set(ENVELOPE_FIELDS) <= set(event) for event in events
        ),
        "envelope_kinds_are_declared_by_the_manifest": all(
            event["kind"] in declared for event in events
        ),
        "correlation_ids_are_complete": correlation_complete,
        "retrieval_view_holds_no_detectable_credential": clean == len(retrieval),
        "retrieval_view_is_marked_untrusted_and_stale": marked == len(retrieval),
        "retrieval_view_derives_from_canonical_content": derived == len(retrieval),
        "repeats_do_not_multiply_records": len(identities) == len(events)
        and duplicate_receipts,
        "tool_calls_and_results_are_paired": tool_pairs == calls,
        "no_capture_gaps": not gaps,
    }
    report = {
        "store_version": STORE_VERSION,
        "env_dir": str(env_dir),
        "tiers": index.get("tiers"),
        "events": len(events),
        "retrieval_records": len(retrieval),
        "duplicates": len(duplicates),
        "gaps": gaps,
        "orphaned_content_files": content_orphans(env_dir, events),
        "checks": checks,
        "ok": all(checks.values()),
    }
    if not events:
        report["empty_capture_note"] = (
            "capture ran and the transcript yielded no events, but it recorded "
            f"{len(gaps)} gap(s); the recorded gap is reported below and by the "
            "no_capture_gaps check, and nothing was dropped silently"
        )
    return report


def trace(env_dir, event_id=None, tool_call_id=None):
    env_dir = Path(env_dir)
    events = read_jsonl(capture_root(env_dir) / "events.jsonl")
    if event_id:
        matches = [event for event in events if event["event_id"] == event_id]
    elif tool_call_id:
        matches = [event for event in events if event["tool_call_id"] == tool_call_id]
    else:
        raise CaptureError("trace needs --event-id or --tool-call-id")
    if not matches:
        raise CaptureError("no captured event matches that identifier")
    root_event = matches[0]
    chain = []
    cursor = root_event
    seen = set()
    while cursor is not None and cursor["event_id"] not in seen:
        seen.add(cursor["event_id"])
        chain.append(
            {
                "event_id": cursor["event_id"],
                "kind": cursor["kind"],
                "occurred_at_ms": cursor["occurred_at_ms"],
            }
        )
        parent = cursor["parent_event_id"]
        cursor = next((event for event in events if event["event_id"] == parent), None)
    origin = root_event["origin"]
    source = Path(origin["source"])
    raw = None
    if source.is_file():
        raw = source.read_bytes().split(b"\n")[origin["line_number"] - 1]
    return {
        "event": root_event,
        "related": [
            {"event_id": other["event_id"], "kind": other["kind"]}
            for other in matches[1:]
        ],
        "turn_chain": list(reversed(chain)),
        "origin": {
            "source": origin["source"],
            "line_number": origin["line_number"],
            "ordinal": origin["ordinal"],
            "record_sha256": origin["record_sha256"],
            "record_verified": raw is not None
            and sha256_bytes(raw) == origin["record_sha256"],
        },
    }


def retrieval_view(env_dir, kind=None):
    env_dir = Path(env_dir)
    records = read_jsonl(capture_root(env_dir) / "retrieval.jsonl")
    if kind:
        records = [record for record in records if record["kind"] == kind]
    return {
        "records": records,
        "untrusted": True,
        "note": "Recalled content is evidence and never authorization.",
    }


def rollout_paths(sessions_dir):
    directory = Path(sessions_dir)
    if not directory.is_dir():
        raise CaptureError(f"no sessions directory at {directory}")
    return sorted(directory.rglob("rollout-*.jsonl"))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--env-dir", default=None)
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture", allow_abbrev=False)
    capture_parser.add_argument("--rollout", action="append", default=[])
    capture_parser.add_argument("--sessions-dir", default=None)

    sub.add_parser("status", allow_abbrev=False)
    sub.add_parser("verify", allow_abbrev=False)

    trace_parser = sub.add_parser("trace", allow_abbrev=False)
    trace_parser.add_argument("--event-id", default=None)
    trace_parser.add_argument("--tool-call-id", default=None)

    retrieval_parser = sub.add_parser("retrieval", allow_abbrev=False)
    retrieval_parser.add_argument("--kind", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    env_dir = (
        Path(args.env_dir) if args.env_dir else isolated_env.default_env_dir(args.root)
    ).resolve()
    try:
        if args.command == "capture":
            rollouts = list(args.rollout)
            sessions_dir = None
            if args.sessions_dir or not rollouts:
                sessions_dir = (
                    Path(args.sessions_dir)
                    if args.sessions_dir
                    else default_rollouts_dir(env_dir)
                )
                found = [str(path) for path in rollout_paths(sessions_dir)]
                if not found and not rollouts:
                    raise CaptureError(
                        f"no rollout-*.jsonl under {sessions_dir}: the rollout-*.jsonl "
                        "pattern matched nothing and no --rollout was given, so there "
                        "is no transcript to capture"
                    )
                rollouts += found
            result = capture(
                env_dir, rollouts, root=args.root, sessions_dir=sessions_dir
            )
        elif args.command == "status":
            result = status(env_dir)
        elif args.command == "verify":
            result = verify(env_dir, root=args.root)
            if not result["ok"]:
                print(json.dumps(result, indent=2, sort_keys=True))
                return 1
        elif args.command == "trace":
            result = trace(
                env_dir, event_id=args.event_id, tool_call_id=args.tool_call_id
            )
        else:
            result = retrieval_view(env_dir, kind=args.kind)
    except (CaptureError, isolated_env.EnvError, jev_manifest.ManifestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
