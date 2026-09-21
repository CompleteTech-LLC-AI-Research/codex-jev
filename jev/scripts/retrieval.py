#!/usr/bin/env python3
"""Budgeted retrieval and hydration over the canonical capture store.

Phase #4 (#14) makes recalled evidence usable without ever turning it into
authority. The canonical capture store (
``jev/scripts/canonical_capture.py``) holds the exact bytes and the origin of
every supported activity. This module exposes that store two ways:

``search``   return source-backed excerpts that match a query, inside an
             explicit budget, from the redacted retrieval view;
``hydrate``  return the exact canonical bytes behind one event, but only after
             re-proving the content hash and the origin record, so the operator
             sees what actually happened rather than a paraphrase.

Both subcommands:

* spend an explicit budget (excerpt count, bytes, approximate tokens) and
  report exactly how much was spent and what was dropped;
* mark every excerpt untrusted and possibly stale, with provenance and a note
  that recalled content is evidence and never authorization (contract C9);
* refuse a workspace mismatch instead of leaking another workspace's evidence;
* refuse optional remote enrichment, because the manifest records no consent
  and no budget for it, and never make an implicit network call.

Hydration fails closed on a missing source, a changed content file, or a
changed origin record: it reports the drift instead of returning stale bytes as
if they were current. Exit codes: 0 = ok, 1 = a refusal or an unproven
reference, 2 = usage error.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_capture
import isolated_env
import jev_manifest

DEFAULT_MAX_EXCERPTS = 8
DEFAULT_MAX_BYTES = 4096
DEFAULT_MAX_TOKENS = 1024

# A conservative bytes-per-token estimate. The budget is a guard rail for how
# much context a caller pulls in, not a billing measure, so a rough constant is
# enough and avoids a tokenizer dependency.
BYTES_PER_TOKEN = 4

EVIDENCE_NOTE = "Recalled content is evidence and never authorization."
REMOTE_CONSENT_KEY = "remote_inference"


class RetrievalError(Exception):
    """Retrieval cannot proceed as requested, and must not guess."""


def _clip(text, limit_bytes):
    """Clip text to a byte budget, never splitting a UTF-8 sequence."""
    raw = text.encode("utf-8")
    if len(raw) <= limit_bytes:
        return text, False
    return raw[:limit_bytes].decode("utf-8", errors="ignore"), True


def _budget(max_excerpts, max_bytes, max_tokens):
    if max_excerpts < 1:
        raise RetrievalError("max_excerpts must be at least 1")
    if max_bytes < 1:
        raise RetrievalError("max_bytes must be at least 1")
    if max_tokens < 1:
        raise RetrievalError("max_tokens must be at least 1")
    return {
        "max_excerpts": int(max_excerpts),
        "max_bytes": int(max_bytes),
        "max_tokens": int(max_tokens),
        "byte_cap": min(int(max_bytes), int(max_tokens) * BYTES_PER_TOKEN),
    }


def _refuse_remote(root, requested):
    """Optional remote enrichment stays disabled unless separately authorized."""
    manifest = isolated_env.load_manifest(root)
    credential = manifest.get("credentials", {}).get(REMOTE_CONSENT_KEY, {})
    consent = credential.get("consent") is True
    budget = credential.get("budget_usd_max")
    authorized = consent and isinstance(budget, (int, float)) and budget > 0
    if not requested:
        return False
    if not authorized:
        raise RetrievalError(
            "E_REMOTE_ENRICHMENT_UNAUTHORIZED: remote enrichment needs explicit "
            "consent and a positive budget; retrieval stays local"
        )
    return True


def _load_store(env_dir, root=None):
    env_dir = Path(env_dir)
    isolated_env.read_env(env_dir, root=root)
    events = canonical_capture.read_jsonl(
        canonical_capture.capture_root(env_dir) / "events.jsonl"
    )
    if not events:
        raise RetrievalError(
            f"no canonical capture at {canonical_capture.capture_root(env_dir)}; "
            "run capture first"
        )
    records = canonical_capture.read_jsonl(
        canonical_capture.capture_root(env_dir) / "retrieval.jsonl"
    )
    return events, records


def _authorization_note():
    return {
        "untrusted": True,
        "possibly_stale": True,
        "provenance_rule": EVIDENCE_NOTE,
    }


def _excerpt(record):
    return {
        "event_id": record["event_id"],
        "capture_id": record["capture_id"],
        "kind": record["kind"],
        "session_id": record["session_id"],
        "turn_id": record["turn_id"],
        "tool_call_id": record["tool_call_id"],
        "origin_workspace": record["origin_workspace"],
        "occurred_at_ms": record["occurred_at_ms"],
        "text": record["text"],
        "redaction": record["redaction"],
        "untrusted": True,
        "possibly_stale": True,
        "provenance": record["provenance"],
    }


def search(
    env_dir,
    query=None,
    kind=None,
    workspace=None,
    max_excerpts=DEFAULT_MAX_EXCERPTS,
    max_bytes=DEFAULT_MAX_BYTES,
    max_tokens=DEFAULT_MAX_TOKENS,
    root=None,
    remote_enrichment=False,
):
    """Return budgeted, source-backed excerpts from the retrieval view."""
    remote = _refuse_remote(root, remote_enrichment)
    _, records = _load_store(env_dir, root=root)
    budget = _budget(max_excerpts, max_bytes, max_tokens)

    matched = []
    for record in records:
        if kind and record["kind"] != kind:
            continue
        if workspace is not None and record["origin_workspace"] != workspace:
            continue
        if query and query.lower() not in record["text"].lower():
            continue
        matched.append(record)
    matched.sort(
        key=lambda record: (
            record.get("occurred_at_ms") or 0,
            record.get("event_id") or "",
        )
    )

    all_workspaces = sorted(
        {record["origin_workspace"] for record in records if record["origin_workspace"]}
    )
    workspace_matched = workspace is None or any(
        record["origin_workspace"] == workspace for record in records
    )

    excerpts = []
    spent = 0
    for record in matched:
        if len(excerpts) >= budget["max_excerpts"]:
            break
        remaining = budget["byte_cap"] - spent
        if remaining <= 0:
            break
        text, truncated = _clip(record["text"], remaining)
        entry = _excerpt(record)
        entry["text"] = text
        entry["truncated"] = truncated
        excerpts.append(entry)
        spent += len(text.encode("utf-8"))

    return {
        "query": query,
        "kind": kind,
        "workspace": workspace,
        "workspaces_present": all_workspaces,
        "workspace_matched": workspace_matched,
        "budget": dict(
            budget,
            excerpts_returned=len(excerpts),
            bytes_returned=spent,
            truncated=any(entry["truncated"] for entry in excerpts)
            or len(excerpts) < len(matched),
            matched=len(matched),
            dropped=len(matched) - len(excerpts),
        ),
        "excerpts": excerpts,
        "remote_enrichment": remote,
        "note": EVIDENCE_NOTE,
    }


def _find_event(events, event_id=None, capture_id=None):
    for event in events:
        if event_id and event["event_id"] == event_id:
            return event
        if capture_id and event["capture_id"] == capture_id:
            return event
    return None


def hydrate(
    env_dir,
    event_id=None,
    capture_id=None,
    workspace=None,
    max_bytes=DEFAULT_MAX_BYTES,
    max_tokens=DEFAULT_MAX_TOKENS,
    root=None,
    remote_enrichment=False,
):
    """Return the exact canonical bytes behind one event, re-proving origin."""
    remote = _refuse_remote(root, remote_enrichment)
    if not event_id and not capture_id:
        raise RetrievalError("hydration needs --event-id or --capture-id")
    env_dir = Path(env_dir)
    events, _ = _load_store(env_dir, root=root)
    budget = _budget(1, max_bytes, max_tokens)
    event = _find_event(events, event_id=event_id, capture_id=capture_id)
    if event is None:
        raise RetrievalError("no captured event matches that identifier")

    identity = {
        "event_id": event["event_id"],
        "capture_id": event["capture_id"],
        "kind": event["kind"],
        "session_id": event["session_id"],
        "turn_id": event["turn_id"],
        "tool_call_id": event["tool_call_id"],
        "origin_workspace": event["origin_workspace"],
    }
    result = dict(
        identity,
        budget=budget,
        remote_enrichment=remote,
        **_authorization_note(),
    )

    if workspace is not None and event["origin_workspace"] != workspace:
        return dict(
            result,
            status="workspace_mismatch",
            ok=False,
            reason=(
                "the event was captured in a different workspace; hydration "
                "refuses to cross workspaces"
            ),
        )

    content_path = (
        canonical_capture.capture_root(env_dir)
        / "content"
        / f"{event['capture_id']}.txt"
    )
    if not content_path.is_file():
        return dict(
            result,
            status="missing_content",
            ok=False,
            reason="the canonical content file is gone",
        )
    content = content_path.read_bytes()
    if canonical_capture.sha256_bytes(content) != event["content_sha256"]:
        return dict(
            result,
            status="content_changed",
            ok=False,
            reason="the stored canonical content no longer matches its recorded hash",
        )

    origin = event["origin"]
    source = Path(origin["source"])
    if not source.is_file():
        return dict(
            result,
            status="missing_source",
            ok=False,
            reason="the origin rollout is gone, so provenance cannot be re-proved",
        )
    lines = source.read_bytes().split(b"\n")
    line = (
        lines[origin["line_number"] - 1] if origin["line_number"] <= len(lines) else b""
    )
    if canonical_capture.sha256_bytes(line) != origin["record_sha256"]:
        return dict(
            result,
            status="source_changed",
            ok=False,
            reason=(
                "the origin record changed since capture; the transcript is no "
                "longer the canonical evidence"
            ),
        )

    text = content.decode("utf-8", errors="replace")
    clipped, truncated = _clip(text, budget["byte_cap"])
    return dict(
        result,
        status="ok",
        ok=True,
        text=clipped,
        truncated=truncated,
        bytes_returned=len(clipped.encode("utf-8")),
        content_sha256=event["content_sha256"],
        provenance={
            "source": origin["source"],
            "line_number": origin["line_number"],
            "ordinal": origin["ordinal"],
            "record_sha256": origin["record_sha256"],
            "record_verified": True,
            "rule": EVIDENCE_NOTE,
            "tier": event["tier"],
        },
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--env-dir", default=None)
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    search_parser = sub.add_parser("search", allow_abbrev=False)
    search_parser.add_argument("--query", default=None)
    search_parser.add_argument("--kind", default=None)
    search_parser.add_argument("--workspace", default=None)
    search_parser.add_argument("--max-excerpts", type=int, default=DEFAULT_MAX_EXCERPTS)
    search_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    search_parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    search_parser.add_argument("--remote-enrichment", action="store_true")

    hydrate_parser = sub.add_parser("hydrate", allow_abbrev=False)
    hydrate_parser.add_argument("--event-id", default=None)
    hydrate_parser.add_argument("--capture-id", default=None)
    hydrate_parser.add_argument("--workspace", default=None)
    hydrate_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    hydrate_parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    hydrate_parser.add_argument("--remote-enrichment", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    env_dir = (
        Path(args.env_dir) if args.env_dir else isolated_env.default_env_dir(args.root)
    ).resolve()
    try:
        if args.command == "search":
            result = search(
                env_dir,
                query=args.query,
                kind=args.kind,
                workspace=args.workspace,
                max_excerpts=args.max_excerpts,
                max_bytes=args.max_bytes,
                max_tokens=args.max_tokens,
                root=args.root,
                remote_enrichment=args.remote_enrichment,
            )
        else:
            result = hydrate(
                env_dir,
                event_id=args.event_id,
                capture_id=args.capture_id,
                workspace=args.workspace,
                max_bytes=args.max_bytes,
                max_tokens=args.max_tokens,
                root=args.root,
                remote_enrichment=args.remote_enrichment,
            )
    except (
        RetrievalError,
        isolated_env.EnvError,
        jev_manifest.ManifestError,
        canonical_capture.CaptureError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "hydrate" and not result["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
