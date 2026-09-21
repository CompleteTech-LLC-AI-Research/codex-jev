#!/usr/bin/env python3
"""Vendored Codex hook translation for the pinned ``jev-sentinel`` component.

The host integrates the security evaluator; it does not reimplement detection.
This module carries only the *envelope translation* the Codex host needs - the
native event names, the normalized event shape, and the response the host
honors - copied faithfully from the pinned component:

    jev-sentinel @ 4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a  (0.1.0)
    jev_sentinel/hooks.py   -> EVENTS, as_text, normalize, render
    jev_sentinel/core.py    -> DECISIONS, STAGES, SOURCES, strict_json, MAX_INPUT

The pinned component owns the evaluator: ``sentinel_boundary.py`` runs the
installed/fetched ``launch.py`` for every verdict, so rules, thresholds, the
audit store, and the policy are never duplicated here. What is duplicated is the
serialization contract, because the host has to bound and correlate the payload
*before* it hands it over, and it has to know which response keys are supported.

Two facts are deliberately pinned in code so a reviewer can check them:

``REPLACEMENT_FIELDS``  Codex emits no output-replacement field. The pinned
``render`` can replace an MCP or known-shape Bash result for Claude only, so for
Codex the post-tool path is feedback plus the component's subsequent-action
latch, never replacement. ``assert_no_replacement`` proves it at runtime.

``MAX_INPUT``           The component rejects a normalized event larger than
``core.MAX_INPUT`` (``oversize_input``) before it evaluates anything. The host
uses the same number as its own forward bound.

Changing this file requires re-checking it against the pinned revision; the
tests compare it to the real component when a checkout is available.
"""

from __future__ import annotations

import json

# Pinned revision of CompleteTech-LLC-AI-Research/jev-sentinel (see
# jev/compatibility-manifest.json -> components[jev-sentinel].revision).
REVISION = "4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a"
VERSION = "0.1.0"

HARNESS = "codex"

# native event name -> boundary stage (hooks.py EVENTS["codex"]).
EVENTS = {
    "UserPromptSubmit": "ingress",
    "PreToolUse": "tool_before",
    "PostToolUse": "tool_after",
}

STAGES = frozenset(
    {"ingress", "tool_before", "tool_after", "context", "memory", "egress"}
)
SOURCES = frozenset({"user", "external", "agent", "unknown"})
DECISIONS = frozenset({"DEFER", "REVIEW", "BLOCK", "QUARANTINE"})

# core.MAX_INPUT: the component refuses a normalized event above this size.
MAX_INPUT = 131072

# No Codex response path may carry these; they are Claude-only in the pinned
# render. A regression that starts emitting one is a boundary error, not a
# feature.
REPLACEMENT_FIELDS = ("updatedToolOutput", "updatedMCPToolOutput")

# The component's documented deterministic plumbing canary.
CANARY = "JEV_SENTINEL_TEST_BLOCK"

# Tool-result keys the pinned normalize accepts, in precedence order.
RESULT_KEYS = ("tool_response", "toolResult", "tool_output", "tool_result")


class AdapterError(ValueError):
    """A refusal from the native envelope translator; carries no context text."""


def strict_json(raw):
    """Faithful copy of ``core.strict_json``: no duplicate keys, no non-finite."""

    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise AdapterError("duplicate JSON key")
            obj[key] = value
        return obj

    def constant(value):
        raise AdapterError("non-finite JSON value")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except AdapterError:
        raise
    except Exception as exc:  # noqa: BLE001 - any parse failure is one refusal
        raise AdapterError("invalid JSON payload") from exc


def dumps(value) -> str:
    """Faithful copy of ``core.dumps`` (canonical JSON, no NaN)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def as_text(value) -> str:
    """Faithful copy of ``hooks.as_text``."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def stage_of(event_name: str) -> str:
    try:
        return EVENTS[event_name]
    except KeyError:
        raise AdapterError("unsupported native event") from None


def normalize(event_name: str, data, profile: str) -> dict:
    """Map one native Codex hook payload onto the component's event shape.

    Faithful to ``hooks.normalize`` for the ``codex`` harness: identical stage
    mapping, source classification, tool-argument handling, and failure modes.
    The host calls this so it can bound and correlate the payload; the component
    still performs the same translation internally when a hook command runs.
    """
    if not isinstance(data, dict):
        raise AdapterError("hook payload must be object")
    stage = stage_of(event_name)
    args = data.get("tool_input", data.get("toolArgs", {}))
    if isinstance(args, str):
        args = strict_json(args)
    if not isinstance(args, dict):
        raise AdapterError("tool arguments must be object")
    if stage == "ingress":
        if not isinstance(data.get("prompt"), str):
            raise AdapterError("prompt field missing")
        content = data["prompt"]
    elif stage == "tool_after":
        matches = [key for key in RESULT_KEYS if key in data]
        if not matches:
            raise AdapterError("tool result field missing")
        content = as_text(data[matches[0]])
    else:
        # Inspect actual arguments, not model-authored explanatory text.
        content = ""
    return {
        "stage": stage,
        "harness": HARNESS,
        "profile": profile,
        "source": "user"
        if stage == "ingress"
        else ("external" if stage == "tool_after" else "agent"),
        "content": content,
        "session_id": data.get("session_id")
        or data.get("sessionId")
        or data.get("conversation_id")
        or "",
        "tool_name": data.get("tool_name", data.get("toolName", "")),
        "tool_input": args,
    }


def render(event_name: str, verdict: dict, raw: dict | None = None) -> dict:
    """Faithful copy of ``hooks.render`` for the ``codex`` harness.

    Codex takes no ``permissionDecision=allow``, and the two replacement fields
    are unreachable here. In shadow mode (``enforced`` false) this returns an
    empty object: observation only.
    """
    stage = stage_of(event_name)
    veto = verdict["enforced"] and verdict["decision"] != "DEFER"
    reason = verdict["message"] + " Event: " + verdict["id"]
    if not veto:
        return {}
    if stage == "ingress":
        return {"decision": "block", "reason": reason}
    if stage == "tool_before":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        },
    }


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def assert_no_replacement(response: dict) -> None:
    """Refuse a Codex host response that claims a result-replacement field."""
    if not isinstance(response, dict):
        raise AdapterError("host response must be an object")
    found = sorted(set(_walk_keys(response)) & set(REPLACEMENT_FIELDS))
    if found:
        raise AdapterError("codex cannot replace tool output: " + ",".join(found))
