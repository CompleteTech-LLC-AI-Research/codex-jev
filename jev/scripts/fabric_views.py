#!/usr/bin/env python3
"""Approved, reversible Fabric prose views: the host-side control of contract ``C4``.

An approved view is a plan that drops standalone assistant prose from the
outgoing request to save context. This module owns the host half of that control:

* ``plan``    previews which assistant-prose messages are eligible and how much
              they would save. It never mutates the request.
* ``apply``   turns a plan into an **approved view** bound to the exact snapshot
              of the post-dedup array it was previewed against. Approval is an
              explicit act; a plan is never applied implicitly.
* ``reset``   discards the view, restoring the original bytes exactly.
* ``filter``  drops the approved messages, and refuses (drops nothing) when the
              array no longer matches the bound snapshot, or when the turn was
              cancelled.

The semantics mirror the ``jev-context-fabric`` model, but they are the host's,
for the host's own wire ``ResponseItem`` array. Only standalone assistant prose
is ever eligible: user, system, developer, tool, reasoning, and compaction items
are never touched, so native compaction keeps working. Savings are reported as
measured bytes and a separately-labelled token figure and are never conflated.

Subcommands:

``plan``     print an eligible-view plan for a wire request;
``apply``    approve a plan for a wire request and print the bound view;
``validate`` validate that a view still matches a request, and report savings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

STATE_VERSION = "jev-view.v1"
KIND = "approved-view"
RECENT = 16
MAX_TARGET = 2_000_000
DEFAULT_TARGET = 12_000

# A message that states a constraint is never eligible, whatever its role says.
GUARD = re.compile(
    r"\b(must|never|constraint|requirement|unresolved|blocker|security|permission|decision)\b"
    r"|do not|don't",
    re.I,
)


class ViewError(ValueError):
    """A bounded, non-sensitive refusal; never carries message text."""


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _shape(item):
    return item.get("type") if isinstance(item, dict) else None


def _text(item):
    """The joined text of a message whose content is text-only, else ``None``."""
    if _shape(item) != "message":
        return None
    parts = item.get("content")
    if not isinstance(parts, list) or not parts:
        return None
    if any(
        not isinstance(p, dict) or p.get("type") not in ("input_text", "output_text")
        for p in parts
    ):
        return None
    return "\n".join(p.get("text", "") for p in parts)


def is_prose(item) -> bool:
    """Only standalone assistant prose may leave the view.

    Anything carrying a tool call, reasoning, or a non-text part is not prose and
    is never eligible, so tool and compaction structure is preserved.
    """
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return False
    if any(k in item for k in ("tool_calls", "function_call", "reasoning", "thinking")):
        return False
    text = _text(item)
    return text is not None and bool(text.strip())


def message_key(item, index: int) -> str:
    """A stable key over the whole wire item, so any edit invalidates a view."""
    return "msg_" + _digest([index, item])


def snapshot(items) -> str:
    """The fingerprint of an array: the identity a plan is bound to."""
    if not isinstance(items, list):
        raise ViewError("items must be an array")
    return _digest([[index, item] for index, item in enumerate(items)])


def _turn_start(items) -> int:
    start = -1
    for index, item in enumerate(items):
        if _shape(item) == "message" and item.get("role") == "user":
            start = index
    return start


def protected_indices(items, keep_recent: int = RECENT) -> set:
    """The current user turn plus the recent tail; nothing here may leave the view."""
    protected = set(range(max(len(items) - keep_recent, 0), len(items)))
    start = _turn_start(items)
    if start >= 0:
        protected.update(range(start, len(items)))
    return protected


def _bytes(value) -> int:
    return len(_canonical(value).encode("utf-8"))


def eligible(items, keep_recent: int = RECENT) -> list:
    """Assistant-prose messages that may be dropped, in reading order."""
    if not isinstance(items, list):
        raise ViewError("items must be an array")
    protected = protected_indices(items, keep_recent)
    picked = []
    for index, item in enumerate(items):
        if index in protected or not is_prose(item):
            continue
        text = _text(item)
        if GUARD.search(text):
            continue
        picked.append(
            {
                "index": index,
                "key": message_key(item, index),
                "bytes": _bytes(item),
                "preview": text[:120],
            }
        )
    return picked


def plan(
    items, goal: str = "", target_bytes: int = DEFAULT_TARGET, keep_recent: int = RECENT
) -> dict:
    """A non-mutating preview bound to this array's snapshot."""
    if not 1 <= int(target_bytes) <= MAX_TARGET:
        raise ViewError("target_bytes out of range")
    candidates = eligible(items, keep_recent)
    picked, saved = [], 0
    for item in sorted(candidates, key=lambda c: c["index"]):
        if saved >= target_bytes:
            break
        picked.append(item)
        saved += item["bytes"]
    result = {
        "version": STATE_VERSION,
        "fingerprint": snapshot(items),
        "goal": goal,
        "target_bytes": int(target_bytes),
        "candidates": picked,
        "estimated_bytes_removed": saved,
        "sufficient": saved >= int(target_bytes),
        "applied": False,
        "native_compaction_called": False,
    }
    result["plan_id"] = "plan_" + _digest(result)
    return result


def apply(items, plan_doc: dict, *, approved: bool) -> dict:
    """Approve a plan into a view bound to ``items``; refuse a stale or unapproved one."""
    if approved is not True:
        raise ViewError("view_not_approved")
    if not isinstance(plan_doc, dict) or plan_doc.get("version") != STATE_VERSION:
        raise ViewError("not_a_plan")
    current = snapshot(items)
    if plan_doc.get("fingerprint") != current:
        raise ViewError("stale_plan")
    if not plan_doc.get("candidates"):
        raise ViewError("empty_plan")
    return {
        "kind": KIND,
        "version": STATE_VERSION,
        "plan_id": plan_doc.get("plan_id"),
        "fingerprint": current,
        "keys": [c["key"] for c in plan_doc["candidates"]],
        "created_ms": int(time.time() * 1000),
    }


def reset(view=None) -> None:
    """Discard a view. Nothing else is stored, so the original bytes return exactly."""
    return None


def filter(items, view, *, keep_recent: int = RECENT, cancelled: bool = False):
    """Drop the approved prose; refuse (drop nothing) when stale or cancelled.

    Returns ``(items_out, report)``. ``items`` and ``view`` are never mutated.
    """
    if not isinstance(view, dict) or view.get("kind") != KIND:
        raise ViewError("not_a_view")
    if cancelled:
        return list(items), {"cancelled": True, "removed": [], "keys": []}
    if view.get("fingerprint") != snapshot(items):
        raise ViewError("stale_view")
    allowed = set(view.get("keys") or [])
    dropped, out = [], []
    protected = protected_indices(items, keep_recent)
    for index, item in enumerate(items):
        key = message_key(item, index)
        if key in allowed and is_prose(item) and index not in protected:
            dropped.append({"index": index, "key": key})
        else:
            out.append(item)
    return out, {"cancelled": False, "removed": dropped, "keys": sorted(allowed)}


def metrics(before, after, token_counter=None) -> dict:
    """Measured bytes and a separately-labelled token figure. Never conflated."""
    bytes_before, bytes_after = _bytes(before), _bytes(after)
    measured = None
    if callable(token_counter):
        measured = {
            "before": int(token_counter(before)),
            "after": int(token_counter(after)),
        }
    return {
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "bytes_removed": bytes_before - bytes_after,
        "tokens_measured": measured,
        "tokens_estimated": {
            "before": bytes_before // 4,
            "after": bytes_after // 4,
            "note": "byte-derived estimate, not a measured token count",
        },
        "native_compaction_called": False,
    }


# --------------------------------------------------------------------- CLI


def _read(path):
    raw = (
        Path(path).read_text(encoding="utf-8")
        if path and path != "-"
        else sys.stdin.read()
    )
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "validate"):
        node = sub.add_parser(name)
        node.add_argument("--request", default="-")
        node.add_argument("--goal", default="")
        node.add_argument("--target-bytes", type=int, default=DEFAULT_TARGET)
        node.add_argument("--keep-recent", type=int, default=RECENT)
    sub.choices["apply"].add_argument("--plan", required=True)
    sub.choices["apply"].add_argument("--approve", action="store_true")
    sub.choices["validate"].add_argument("--view", required=True)
    args = parser.parse_args(argv)

    try:
        items = _read(args.request)["input"]
        if args.command == "plan":
            doc = plan(items, args.goal, args.target_bytes, args.keep_recent)
            print(json.dumps(doc, indent=2))
            return 0
        if args.command == "apply":
            doc = _read(args.plan)
            print(json.dumps(apply(items, doc, approved=args.approve), indent=2))
            return 0
        view = _read(args.view)
        out, report = filter(items, view, keep_recent=args.keep_recent)
        print(json.dumps({"report": report, "metrics": metrics(items, out)}, indent=2))
        return 0
    except ViewError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
