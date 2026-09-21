#!/usr/bin/env python3
"""A test double for the pinned `jev-context-fabric` prose view (priority 200).

The real stage lives in a separate repository that CI does not check out
(`jev-context-fabric`, `src/jev_context/bus.py`). This stub speaks the real
`jev-bus.stage.v1` protocol and rewrites assistant prose only, leaving every
tool item untouched, which is the part of its contract the host carrier needs
to be pinned to. It implements none of the real view's approval semantics.

Every run it produces is tier `bus-stage-stub`, never real evidence.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import jev_bus  # noqa: E402

VIEW = "[Jev view: approved prose view applied]"


def handle(request):
    messages = request["messages"]
    renames = {}
    if request.get("op") == "plan":
        return {
            "ok": True,
            "messages": messages,
            "notes": [{"action": "prose view eligible", "count": 1}],
        }
    for index, message in enumerate(messages):
        if message.get("_jev_shape") == "message" and message.get("role") == "assistant":
            renames[index] = message.get("content")
    for index in renames:
        messages[index]["content"] = VIEW
    notes = (
        [{"action": "approved prose view applied", "count": len(renames)}]
        if renames
        else [{"action": "passthrough", "detail": "no prose view eligible"}]
    )
    return {"ok": True, "messages": messages, "notes": notes}


def main():
    try:
        request = jev_bus.read_stage_request()
        response = handle(request)
    except Exception as exc:
        response = {"ok": False, "error": type(exc).__name__}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
