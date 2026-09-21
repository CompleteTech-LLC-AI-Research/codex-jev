#!/usr/bin/env python3
"""A test double for the pinned `jev-prune-kit` dedup stage (priority 100).

The real stage lives in a separate repository that CI does not check out
(`jev-prune-kit`, `jev_prune/worker.py`), so the wire contract the host carrier
depends on is pinned against this stub instead. It speaks the real
`jev-bus.stage.v1` protocol on stdin/stdout and, like the real stage,
**substitutes a duplicate read body in place** without changing the array
length, so a downstream stage can still key messages by position.

The selection rules mirror the real local `project` policy
(`exact-read-repeat-v1`): an older result is replaced only when its call id is
unique, its result is unique, the call resolves to a read-only tool, and an
older and a later result of that same request hold byte-identical bodies.
What the stub does NOT implement is the approval path: the real stage replaces
only the bodies named by approved receipts from a prior paid `assess`, while the
stub substitutes every pair its local rules select. It does mirror the real
stage's cheapest gate: a request with no session id is a passthrough. The
`assess`-side selection rules (the recent-turn cutoff and the body size window)
are deliberately absent for the same reason: they gate which pairs may be
*offered* for approval, not which of them apply. Reverting a substitution the
host cannot prove is the carrier's `C3` job, not this stage's.

Every run it produces is tier `bus-stage-stub`, never real evidence.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import jev_bus  # noqa: E402

MARKER = "[Jev prune: repeated read-result body omitted; retained witness: {witness}]"
READ_TOOLS = frozenset({"read", "read_file", "file_read"})


def _calls(messages):
    """Map each tool call id to its request identity, exactly one call per id."""
    calls = {}
    for message in messages:
        if message.get("_jev_shape") != "function_call":
            continue
        for call in message.get("tool_calls") or []:
            ident = call.get("id")
            if not isinstance(ident, str):
                continue
            function = call.get("function") or {}
            calls.setdefault(ident, []).append(
                (function.get("name", ""), function.get("arguments", ""))
            )
    return calls


def _items(messages):
    """Resolve eligible read results to (index, key, tool, call_id, content)."""
    calls = _calls(messages)
    results = {}
    for message in messages:
        if message.get("_jev_shape") == "function_call_output":
            ident = message.get("tool_call_id")
            if isinstance(ident, str):
                results[ident] = results.get(ident, 0) + 1
    items = []
    for index, message in enumerate(messages):
        if message.get("_jev_shape") != "function_call_output":
            continue
        ident = message.get("tool_call_id")
        matches = calls.get(ident, []) if isinstance(ident, str) else []
        content = message.get("content")
        if len(matches) != 1 or results.get(ident) != 1 or not isinstance(content, str):
            continue
        tool, arguments = matches[0]
        key = jev_bus.dumps({"tool": tool, "request": arguments})
        items.append((index, key, tool, ident, content))
    return items


def handle(request):
    messages = request["messages"]
    if not request.get("session"):
        return {
            "ok": True,
            "messages": messages,
            "notes": [{"action": "passthrough", "detail": "no session id supplied"}],
        }
    items = _items(messages)
    latest = {}
    for index, key, _tool, ident, _content in items:
        latest[key] = (index, ident)
    applied = 0
    saved = 0
    for index, key, tool, _ident, content in items:
        witness_index, witness_id = latest[key]
        if (
            index >= witness_index
            or tool not in READ_TOOLS
            or content != messages[witness_index].get("content")
        ):
            continue
        marker = MARKER.format(witness=witness_id[:160])
        if len(marker.encode()) < len(content.encode()):
            saved += len(content.encode()) - len(marker.encode())
            messages[index]["content"] = marker
            applied += 1
    notes = []
    if applied:
        notes.append(
            {
                "action": "duplicate read bodies substituted",
                "count": applied,
                "bytes": saved,
                "detail": "older identical read bodies replaced by a marker naming the retained copy",
            }
        )
    else:
        notes.append({"action": "passthrough", "detail": "no duplicate read bodies"})
    return {"ok": True, "messages": messages, "notes": notes}


def main():
    try:
        request = jev_bus.read_stage_request()
        response = handle(request)
    except Exception as exc:  # a stage must decline, never break the carrier's turn
        response = {"ok": False, "error": type(exc).__name__}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
