#!/usr/bin/env python3
"""Assert plaintext collaboration behaviour from a recorded mock run.

Inputs:
  --requests-dir DIR   directory written by mock_responses_server.py
  --session-dir DIR    optional Codex session/rollout directory to inspect
  --json               emit a machine-readable summary

Failures are reported one per line and exit code 1; a clean run exits 0.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

COLLAB_TOOLS = ("spawn_agent", "send_message", "followup_task")
#: Collaboration tools are advertised inside a Responses API namespace. A call
#: that omits it is rejected by the tool router as an unsupported call, so the
#: parent never reaches a child turn; asserting on it keeps that failure mode
#: diagnosable instead of silent.
EXPECTED_NAMESPACE = "collaboration"


def load_requests(directory: str) -> list[dict]:
    """Load recorded requests with their turn labels.

    The label is read from the sidecar written by the mock, not inferred from
    ordering, because a parent and a child request can be recorded concurrently.
    """
    requests = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or name == "turns.json":
            continue
        with open(os.path.join(directory, name)) as handle:
            body = json.load(handle)
        turn_path = os.path.join(directory, name[: -len(".json")] + ".turn")
        turn = None
        if os.path.exists(turn_path):
            with open(turn_path) as handle:
                turn = handle.read().strip()
        requests.append({"name": name, "body": body, "turn": turn})
    return requests


def walk(node):
    """Yield every dict reachable from node."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def tool_schemas(body: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for node in walk(body.get("tools")):
        name = node.get("name")
        if isinstance(name, str) and "parameters" in node:
            found.setdefault(name, node)
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--session-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    observations: dict = {}

    requests = load_requests(args.requests_dir)
    if not requests:
        failures.append(
            "no request bodies were recorded; the host never reached the mock"
        )
        observations["requests"] = 0
    else:
        observations["requests"] = len(requests)

    turns_path = os.path.join(args.requests_dir, "turns.json")
    turns: list[str] = []
    if os.path.exists(turns_path):
        with open(turns_path) as handle:
            turns = json.load(handle).get("turns", [])
    observations["turns"] = turns

    # 1. Parent/child really happened, not just a single parent turn.
    for expected in ("parent_initial", "child", "parent_after_spawn"):
        if expected not in turns:
            failures.append(f"mock never served a `{expected}` turn (saw {turns})")

    child_requests = [request for request in requests if request["turn"] == "child"]

    # 2. The collaboration message parameter is advertised as plaintext.
    schema_report: dict[str, dict] = {}
    if requests:
        schemas = tool_schemas(requests[0]["body"])
        for tool in COLLAB_TOOLS:
            schema = schemas.get(tool)
            if schema is None:
                failures.append(f"tool `{tool}` was not advertised to the model")
                continue
            message = schema.get("parameters", {}).get("properties", {}).get("message")
            if message is None:
                failures.append(f"tool `{tool}` has no `message` parameter")
                continue
            schema_report[tool] = message
            if "encrypted" in message:
                failures.append(
                    f"tool `{tool}` still advertises an `encrypted` marker: {message['encrypted']!r}"
                )
            if schema.get("strict") is not False and schema.get("strict") is not None:
                failures.append(f"tool `{tool}` unexpectedly became a strict schema")
    observations["message_schemas"] = schema_report

    # 3. The child received the task as readable text.
    sentinel = "JEV-SMOKE-PLAINTEXT-TASK-7f3c"
    if child_requests:
        blob = json.dumps(child_requests[0]["body"])
        if sentinel not in blob:
            failures.append("the child request did not contain the plaintext task text")
        observations["child_request"] = child_requests[0]["name"]
    else:
        observations["child_request"] = None

    # 4. Nothing forwarded the call as ciphertext.
    for request in requests:
        blob = json.dumps(request["body"])
        if "encrypted_function_args" in blob:
            failures.append(
                f"{request['name']} carries `encrypted_function_args`, so the call was not plaintext"
            )
        if "ciphertext" in blob.lower():
            failures.append(f"{request['name']} mentions ciphertext")

    # 5. The replayed spawn call keeps the namespace the tools were advertised under.
    spawn_calls = []
    for request in requests:
        for node in walk(request["body"].get("input")):
            if (
                node.get("type") == "function_call"
                and node.get("name") == "spawn_agent"
            ):
                spawn_calls.append(
                    {"request": request["name"], "namespace": node.get("namespace")}
                )
    observations["spawn_function_calls"] = spawn_calls
    if not spawn_calls:
        failures.append("no recorded request replayed the `spawn_agent` function call")
    elif not any(call["namespace"] == EXPECTED_NAMESPACE for call in spawn_calls):
        failures.append(
            f"the `spawn_agent` call omitted the `{EXPECTED_NAMESPACE}` namespace, "
            "which the host rejects as an unsupported call"
        )

    # 6. Persisted rollout keeps the human-readable message and no encrypted marker.
    rollout_report: dict = {"files": 0, "saw_plaintext_call": False}
    if args.session_dir and os.path.isdir(args.session_dir):
        for root, _dirs, files in os.walk(args.session_dir):
            for name in files:
                path = os.path.join(root, name)
                rollout_report["files"] += 1
                with open(path, "r", errors="replace") as handle:
                    text = handle.read()
                if "encrypted_function_args" in text:
                    failures.append(
                        f"rollout {path} stores encrypted function arguments"
                    )
                if sentinel in text and "spawn_agent" in text:
                    rollout_report["saw_plaintext_call"] = True
        if rollout_report["files"] == 0:
            failures.append("no persisted session/rollout files were found")
        elif not rollout_report["saw_plaintext_call"]:
            failures.append("no rollout recorded the plaintext spawn_agent call")
    observations["rollout"] = rollout_report

    observations["failures"] = failures
    observations["ok"] = not failures

    if args.json:
        json.dump(observations, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(json.dumps(observations, indent=2, sort_keys=True))
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
