#!/usr/bin/env python3
"""Seed a resumable Codex session rollout that carries an eligible read pair.

The host has no ``read``/``read_file`` tool in this revision, so a real shell
or extension read can never enter the eligible set (the dedup policy covers
only ``read``, ``read_file`` and ``file_read``). The only way a *launched* host
can carry a duplicate read pair in its outgoing ``input`` is to resume a
transcript that already contains one, so this script writes a rollout the host
will load and resume.

The pair is placed early and followed by filler turns so it lands outside the
last ``RECENT = 16`` items and before the resumed turn's user message, which is
exactly the region the receipt contract allows a body to be replaced in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone


READ_TOOL = "read"
DEFAULT_SOURCE_CALL = "call_jev_proj_source"
DEFAULT_WITNESS_CALL = "call_jev_proj_witness"
DEFAULT_PROMPT = "Please summarise the status of the seeded fixture project."


def read_body(target_bytes: int) -> str:
    """A deterministic, deliberately boring body with no vetoed substrings."""
    line = "seeded read evidence: the fixture project reports nominal progress for stage {n:03d}."
    lines = []
    total = 0
    index = 0
    while total < target_bytes:
        text = line.format(n=index)
        lines.append(text)
        total += len(text.encode("utf-8")) + 1
        index += 1
    return "\n".join(lines)


def _record(ordinal: int, kind: str, payload: dict, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "ordinal": ordinal,
        "type": kind,
        "payload": payload,
    }


def build_records(args) -> list[dict]:
    body = read_body(args.body_bytes)
    arguments = json.dumps({"path": "fixture/status.txt"}, separators=(",", ":"))
    stamp = "2026-09-21T00:00:00.000Z"
    records = []
    ordinal = 0

    def add(kind: str, payload: dict) -> None:
        nonlocal ordinal
        records.append(_record(ordinal, kind, payload, stamp))
        ordinal += 1

    def message(role: str, text: str) -> None:
        part = "input_text" if role == "user" else "output_text"
        add(
            "response_item",
            {
                "type": "message",
                "id": f"msg_seed_{ordinal}",
                "role": role,
                "content": [{"type": part, "text": text}],
            },
        )

    def read_pair(call_id: str, label: str) -> None:
        add(
            "response_item",
            {
                "type": "function_call",
                "id": f"fc_{call_id}",
                "name": READ_TOOL,
                "arguments": arguments,
                "call_id": call_id,
            },
        )
        add(
            "response_item",
            {
                "type": "function_call_output",
                "id": f"fco_{call_id}",
                "call_id": call_id,
                "output": body,
            },
        )
        del label

    add(
        "session_meta",
        {
            "session_id": args.session,
            "id": args.session,
            "timestamp": stamp,
            "cwd": args.workspace,
            "runtime_workspace_roots": [args.workspace],
            "originator": "codex_exec",
            "cli_version": "0.0.0",
            "source": "exec",
            "thread_source": "user",
            "model_provider": args.provider,
            "base_instructions": {"text": "You are Codex, a coding agent."},
        },
    )
    add(
        "event_msg",
        {
            "type": "task_started",
            "turn_id": "seed-turn-1",
            "root_turn_id": "seed-turn-1",
            "started_at": 1789964006,
            "model_context_window": 258400,
            "collaboration_mode_kind": "default",
        },
    )
    message("user", args.seed_prompt)
    message("assistant", "I will inspect the fixture status file first.")
    read_pair(args.source_call, "source")
    message(
        "assistant", "The first read completed; re-reading the same file to confirm."
    )
    read_pair(args.witness_call, "witness")
    message("assistant", "Both reads agree. Continuing with unrelated bookkeeping.")
    for n in range(args.filler):
        if n % 2 == 0:
            message("user", f"bookkeeping checkpoint {n}: nothing further required.")
        else:
            message("assistant", f"acknowledged bookkeeping checkpoint {n}.")
    return records


def rollout_path(codex_home: str, session: str, when: datetime) -> str:
    day = when.strftime("%Y/%m/%d")
    stamp = when.strftime("%Y-%m-%dT%H-%M-%S")
    return os.path.join(codex_home, "sessions", day, f"rollout-{stamp}-{session}.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--provider", default="jev_projection_mock")
    parser.add_argument("--body-bytes", type=int, default=640)
    parser.add_argument("--filler", type=int, default=24)
    parser.add_argument("--source-call", default=DEFAULT_SOURCE_CALL)
    parser.add_argument("--witness-call", default=DEFAULT_WITNESS_CALL)
    parser.add_argument("--seed-prompt", default="Inspect the fixture status file.")
    parser.add_argument("--resume-prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--manifest", help="write a JSON description of the seed here")
    parser.add_argument("--when", default="2026-09-21T00:00:00+00:00")
    args = parser.parse_args()

    when = datetime.fromisoformat(args.when).astimezone(timezone.utc)
    records = build_records(args)
    path = rollout_path(args.codex_home, args.session, when)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    items = [r for r in records if r["type"] == "response_item"]
    body = read_body(args.body_bytes)
    summary = {
        "session": args.session,
        "rollout": os.path.abspath(path),
        "workspace": args.workspace,
        "provider": args.provider,
        "resume_prompt": args.resume_prompt,
        "response_item_count": len(items),
        "source_call": args.source_call,
        "witness_call": args.witness_call,
        "read_arguments": json.dumps(
            {"path": "fixture/status.txt"}, separators=(",", ":")
        ),
        "read_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "read_body_bytes": len(body.encode("utf-8")),
    }
    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
