#!/usr/bin/env python3
"""A labelled offline-fixture jev-bus stage.

This is not a component. It stands in for one so the host's bus boundary can be
exercised end to end - real host, real adapter, real subprocess transport - on a
machine where the owning packages are not installed. It serves exactly one
``jev-bus.stage.v1`` request on stdin and never calls a model or a network.

Modes (``JEV_BUS_FIXTURE_MODE``):

``dedup``    replace the *earlier* of two identical tool-result bodies with a
             retained-evidence marker; the array length and order never change.
``view``     replace assistant prose with an approved-view marker.
``decline``  report ``ok: false``, which the bus must treat as passthrough.
``fail``     exit non-zero, which the bus must treat as passthrough.

The mode comes from ``JEV_BUS_FIXTURE_MODE`` or from ``--mode`` on the command
line, so one chain can run two different fixture stages.

Every result is tier ``offline-fixture``: it proves host wiring and chain
behaviour, never component semantics and never live-model behaviour.

Exit codes: 0 served, 1 unusable request, 2 requested failure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_bus

TIER = "offline-fixture"
DEDUP_MARKER = "[jev-fixture] duplicate read body retained at {index}"
VIEW_MARKER = "[jev-fixture view] {text}"


def _fail(detail: str) -> int:
    print(f"E_FIXTURE_STAGE: {detail}", file=sys.stderr)
    return 1


def _argv_mode(argv):
    """Read ``--mode <name>`` so one chain can run two different fixture stages."""
    for position, argument in enumerate(argv):
        if argument == "--mode":
            return argv[position + 1] if position + 1 < len(argv) else None
        if argument.startswith("--mode="):
            return argument.partition("=")[2]
    return None


def _tool_bodies(messages):
    """Index every tool-result message that carries a text body."""
    return [
        (index, message.get("content"))
        for index, message in enumerate(messages)
        if isinstance(message, dict)
        and message.get("_jev_shape") == "function_call_output"
        and isinstance(message.get("content"), str)
    ]


def _dedup(messages):
    """Replace the earlier copy of a repeated tool-result body.

    Length and order are preserved, which is what lets a downstream stage keep
    keying messages by position.
    """
    seen = {}
    replaced = []
    for index, body in _tool_bodies(messages):
        if body in seen:
            earlier = seen[body]
            messages[earlier] = {**messages[earlier]}
            messages[earlier]["content"] = DEDUP_MARKER.format(index=index)
            replaced.append(earlier)
        else:
            seen[body] = index
    if not replaced:
        return messages, {"action": "passthrough", "detail": "no duplicate read body"}
    return messages, {
        "action": "replaced duplicate read bodies",
        "count": len(replaced),
        "detail": "earlier copies retained elsewhere in the transcript",
        "id": jev_bus.digest(jev_bus.dumps(sorted(replaced))),
    }


def _view(messages):
    viewed = []
    for index, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and message.get("_jev_shape") == "message"
            and message.get("role") == "assistant"
            and isinstance(message.get("content"), str)
        ):
            messages[index] = {
                **message,
                "content": VIEW_MARKER.format(text=message["content"]),
            }
            viewed.append(index)
    if not viewed:
        return messages, {"action": "passthrough", "detail": "no assistant prose"}
    return messages, {
        "action": "applied approved view",
        "count": len(viewed),
        "detail": "assistant prose replaced by a reversible view marker",
        "id": jev_bus.digest(jev_bus.dumps(sorted(viewed))),
    }


def serve(request: dict, mode: str) -> dict:
    if request.get("schema") != jev_bus.STAGE_SCHEMA:
        raise ValueError("unsupported stage schema")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise ValueError("stage request carries no message list")
    if request.get("op") == "plan":
        # Plan mode reports eligibility and never rewrites the array.
        return {
            "ok": True,
            "messages": messages,
            "notes": [{"action": "planned", "detail": f"mode {mode}", "tier": TIER}],
        }
    if mode == "decline":
        return {"ok": False, "detail": "fixture declined"}
    produced, note = _dedup(list(messages)) if mode == "dedup" else _view(list(messages))
    note["tier"] = TIER
    return {"ok": True, "messages": produced, "notes": [note]}


def main() -> int:
    mode = _argv_mode(sys.argv[1:]) or os.environ.get("JEV_BUS_FIXTURE_MODE", "dedup")
    if mode == "fail":
        return 2
    try:
        request = jev_bus.read_stage_request()
    except ValueError as error:
        # BusError is a ValueError, and so is an unreadable JSON body.
        return _fail(str(error))
    try:
        response = serve(request, mode)
    except ValueError as error:
        return _fail(str(error))
    json.dump(response, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
