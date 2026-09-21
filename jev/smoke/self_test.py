#!/usr/bin/env python3
"""Self-test for the plaintext smoke fixture.

Runs the mock server and the checker with synthetic request bodies and asserts
that the checker both passes the plaintext shape and *fails* the encrypted
shape. A checker that cannot fail is not evidence, so the negative controls are
part of the fixture's own test suite.

Standard library only. Writes to TMPDIR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "mock_responses_server.py")
CHECKER = os.path.join(HERE, "check_plaintext.py")

SENTINEL = "JEV-SMOKE-PLAINTEXT-TASK-7f3c"
CALL_ID = "call_jev_smoke_spawn_1"


def tool(name: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


def collaboration_tools(encrypted: bool) -> list[dict]:
    marker = {"encrypted": True} if encrypted else {}
    message = {"type": "string", "description": "Message text.", **marker}
    return [
        tool(
            "spawn_agent",
            {"message": message, "task_name": {"type": "string"}},
            ["task_name", "message"],
        ),
        tool(
            "send_message",
            {"target": {"type": "string"}, "message": message},
            ["message"],
        ),
        tool(
            "followup_task",
            {"target": {"type": "string"}, "message": message},
            ["message"],
        ),
    ]


def post(port: str, body: dict) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return response.status


def run_case(
    root: str,
    encrypted: bool,
    include_child: bool,
    namespace: str | None = "collaboration",
    rendezvous: float = 5.0,
) -> tuple[int, dict]:
    requests_dir = os.path.join(root, "requests")
    os.makedirs(requests_dir, exist_ok=True)
    port_file = os.path.join(root, "port")
    mock = subprocess.Popen(
        [
            sys.executable,
            MOCK,
            "--requests-dir",
            requests_dir,
            "--port-file",
            port_file,
            "--child-rendezvous-seconds",
            str(rendezvous),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if os.path.exists(port_file) and os.path.getsize(port_file):
                break
            time.sleep(0.1)
        port = open(port_file).read().strip()
        tools = collaboration_tools(encrypted)
        post(
            port,
            {
                "model": "m",
                "input": [{"role": "user", "content": "delegate"}],
                "tools": tools,
            },
        )
        if include_child:
            post(
                port,
                {
                    "model": "m",
                    "input": [{"role": "user", "content": f"task: {SENTINEL}"}],
                    "tools": tools,
                },
            )
        spawn_call = {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": CALL_ID,
            "arguments": json.dumps({"task_name": "c", "message": SENTINEL}),
        }
        if namespace is not None:
            spawn_call["namespace"] = namespace
        post(
            port,
            {
                "model": "m",
                "input": [
                    spawn_call,
                    {
                        "type": "function_call_output",
                        "call_id": CALL_ID,
                        "output": "{}",
                    },
                ],
                "tools": tools,
            },
        )
    finally:
        mock.terminate()
        mock.wait(timeout=10)
    result = subprocess.run(
        [sys.executable, CHECKER, "--requests-dir", requests_dir, "--json"],
        capture_output=True,
        text=True,
    )
    try:
        verdict = json.loads(result.stdout)
    except json.JSONDecodeError:
        verdict = {"stdout": result.stdout, "stderr": result.stderr}
    return result.returncode, verdict


def check_rendezvous(root: str) -> tuple[int, dict]:
    """Prove the mock holds the parent's post-spawn turn until the child arrives.

    The child request is issued *after* the parent's post-spawn request and from
    a different thread, so the only way the parent can be answered last is if
    the rendezvous actually blocked it.
    """
    requests_dir = os.path.join(root, "requests")
    os.makedirs(requests_dir, exist_ok=True)
    port_file = os.path.join(root, "port")
    mock = subprocess.Popen(
        [
            sys.executable,
            MOCK,
            "--requests-dir",
            requests_dir,
            "--port-file",
            port_file,
            "--child-rendezvous-seconds",
            "10",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            if os.path.exists(port_file) and os.path.getsize(port_file):
                break
            time.sleep(0.1)
        port = open(port_file).read().strip()
        tools = collaboration_tools(encrypted=False)
        base = {"model": "m", "tools": tools}
        post(port, {**base, "input": [{"role": "user", "content": "delegate"}]})

        parent = {
            **base,
            "input": [
                {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "namespace": "collaboration",
                    "call_id": CALL_ID,
                    "arguments": json.dumps({"task_name": "c", "message": SENTINEL}),
                },
                {"type": "function_call_output", "call_id": CALL_ID, "output": "{}"},
            ],
        }
        thread = threading.Thread(target=post, args=(port, parent))
        thread.start()
        time.sleep(1.0)
        post(
            port, {**base, "input": [{"role": "user", "content": f"task: {SENTINEL}"}]}
        )
        thread.join(timeout=15)
    finally:
        mock.terminate()
        mock.wait(timeout=10)
    result = subprocess.run(
        [sys.executable, CHECKER, "--requests-dir", requests_dir, "--json"],
        capture_output=True,
        text=True,
    )
    try:
        verdict = json.loads(result.stdout)
    except json.JSONDecodeError:
        verdict = {"stdout": result.stdout, "stderr": result.stderr}
    return result.returncode, verdict


def main() -> int:
    failures: list[str] = []
    root = tempfile.mkdtemp(prefix="jev-smoke-selftest-")
    try:
        for name, encrypted, include_child, namespace, want_ok, want_substring in (
            ("plaintext", False, True, "collaboration", True, None),
            (
                "encrypted-schema",
                True,
                True,
                "collaboration",
                False,
                "advertises an `encrypted` marker",
            ),
            (
                "no-child",
                False,
                False,
                "collaboration",
                False,
                "never served a `child` turn",
            ),
            (
                "bare-namespace",
                False,
                True,
                None,
                False,
                "omitted the `collaboration` namespace",
            ),
        ):
            case_root = os.path.join(root, name)
            os.makedirs(case_root, exist_ok=True)
            code, verdict = run_case(
                case_root,
                encrypted,
                include_child,
                namespace,
                rendezvous=5.0 if include_child else 1.0,
            )
            ok = code == 0
            print(
                f"case {name}: exit={code} ok={verdict.get('ok')} failures={verdict.get('failures')}"
            )
            if ok != want_ok:
                failures.append(f"case {name}: expected ok={want_ok}, got exit {code}")
            if want_substring and not any(
                want_substring in failure for failure in verdict.get("failures", [])
            ):
                failures.append(
                    f"case {name}: no failure mentioned {want_substring!r}: {verdict.get('failures')}"
                )

        code, verdict = check_rendezvous(os.path.join(root, "rendezvous"))
        print(
            f"case rendezvous: exit={code} turns={verdict.get('turns')} "
            f"failures={verdict.get('failures')}"
        )
        if code != 0:
            failures.append(f"case rendezvous: expected ok, got exit {code}")
        if verdict.get("turns") != ["parent_initial", "child", "parent_after_spawn"]:
            failures.append(
                "case rendezvous: the parent's post-spawn turn was not held until the "
                f"child turn arrived: {verdict.get('turns')}"
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print("self-test:", "failed" if failures else "passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
