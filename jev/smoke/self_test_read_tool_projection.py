#!/usr/bin/env python3
"""Self-test for the projection smoke fixture.

Builds a synthetic recorded run -- one control body, and the same body put
through the boundary adapter -- then asserts that `check_read_tool_projection.py` accepts
the good shape and *rejects* each way the evidence could be hollow. A checker
that cannot fail is not evidence, so the negative controls live beside the
fixture itself.

The transcript is synthetic on purpose: this file must run in CI, where no
`codex` binary is built. It therefore proves the *checker*, not the host. The
host behaviour is proven by `run-read-tool-projection-smoke.sh` against a real binary.

Standard library only. Writes to TMPDIR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CHECKER = os.path.join(HERE, "check_read_tool_projection.py")
ADAPTER = os.path.join(REPO_ROOT, "jev", "scripts", "bus_boundary.py")
STUB = os.path.join(REPO_ROOT, "jev", "tests", "bus_stage_stub", "dedup.py")
STAGE_COMMAND = f"{sys.executable} {STUB}"

#: A read body comfortably past the policy's minimum, so the marker that would
#: replace it is strictly shorter.
READ_BODY = "deterministic memory probe content alphabet soup " * 8

#: A stage that always declines. Pointing the replay at it must make the checker
#: fail, which is what proves the replay leg is asserted rather than assumed.
DECLINING_STAGE = """\
import sys
sys.stdin.read()
sys.stdout.write('{"ok": false, "error": "ProjectionRefused"}')
"""


def call_item(index: int, call_id: str, path: str) -> dict:
    return {
        "type": "function_call",
        "id": f"call_item_{index:02d}",
        "name": "read",
        "namespace": "memories",
        "call_id": call_id,
        "arguments": json.dumps({"path": path}),
        "status": "completed",
    }


def output_item(index: int, call_id: str, body: str) -> dict:
    return {
        "type": "function_call_output",
        "id": f"out_item_{index:02d}",
        "call_id": call_id,
        "output": body,
    }


def message(role: str, text: str) -> dict:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def control_body() -> dict:
    """A transcript whose duplicate pair is outside the user turn and the tail.

    The first two reads share a path *and* a byte-identical body, so the pinned
    policy selects the older one; the remaining nine are distinct. Two messages
    up front and eleven reads put the pair well behind the `RECENT = 16` window
    the policy protects.
    """
    duplicate = f"{READ_BODY}\n"
    items = [
        message("developer", "you are codex"),
        message("user", "read the memory files"),
    ]
    for index in range(11):
        path = "jev-probe.txt" if index < 2 else f"probe-{index:02d}.txt"
        call_id = f"call_jev_projection_{index + 1:02d}"
        body = duplicate if index < 2 else f"{READ_BODY}{index}\n"
        items.append(call_item(index, call_id, path))
        items.append(output_item(index, call_id, body))
    items.append(message("assistant", "transcript complete"))
    items.append(message("user", "wrap up the review"))
    return {"model": "jev-probe-model", "input": items, "stream": True}


def project(body: dict, *, switch_on: bool) -> tuple[dict, dict]:
    """Run the boundary adapter over `body`, exactly as the host would."""
    environment = dict(os.environ)
    environment["JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS"] = "1" if switch_on else "0"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            ADAPTER,
            "apply",
            "--request",
            "-",
            "--json",
            "--session",
            "self-test-session",
            "--turn",
            "self-test-turn",
            "--workspace",
            "/",
            "--stage",
            f"dedup={STAGE_COMMAND}",
        ],
        input=json.dumps(body).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "adapter refused the synthetic body: "
            + completed.stderr.decode("utf-8", "replace")
        )
    decoded = json.loads(completed.stdout.decode("utf-8"))
    return decoded["request"], decoded["report"]


def write_run(directory: str, body: dict, rollout: str | None = None) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "request-00.json"), "w") as handle:
        json.dump(body, handle)
    with open(os.path.join(directory, "request-00.turn"), "w") as handle:
        handle.write("projection_final")
    if rollout is not None:
        sessions = os.path.join(directory, "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "rollout.jsonl"), "w") as handle:
            handle.write(rollout)


def run_checker(work: str, *, case: dict) -> tuple[int, str]:
    root = os.path.join(work, "case")
    shutil.rmtree(root, ignore_errors=True)
    write_run(os.path.join(root, "on"), case["on"], case.get("on_rollout"))
    write_run(os.path.join(root, "off"), case["off"], case.get("off_rollout"))
    write_run(os.path.join(root, "none"), case.get("none", case["off"]))
    argv = [
        sys.executable,
        CHECKER,
        "--on-dir",
        os.path.join(root, "on"),
        "--off-dir",
        os.path.join(root, "off"),
        "--none-dir",
        os.path.join(root, "none"),
    ]
    for flag, key, label in (
        ("--on-sessions", "on_rollout", "on"),
        ("--off-sessions", "off_rollout", "off"),
    ):
        if case.get(key) is not None:
            argv += [flag, os.path.join(root, label, "sessions")]
    if case.get("repo_root"):
        argv += [
            "--repo-root",
            case["repo_root"],
            "--stage-command",
            case.get("stage_command", ""),
        ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace")


def marker_text(body: dict) -> str:
    for item in body.get("input") or []:
        text = item.get("output") if isinstance(item, dict) else None
        if isinstance(text, str) and "[Jev prune:" in text:
            return text
    raise AssertionError("the synthetic control carried no projection marker")


def witness_of(body: dict) -> str:
    return marker_text(body).split("retained witness: ")[1].rstrip("]")


def rewrite_marker(
    body: dict, *, call_id: str | None = None, text: str | None = None
) -> dict:
    """Return a copy of `body` whose marker item is bent out of shape."""
    bent = json.loads(json.dumps(body))
    for item in bent["input"]:
        if isinstance(item, dict) and isinstance(item.get("output"), str):
            if "[Jev prune:" in item["output"]:
                if call_id is not None:
                    item["call_id"] = call_id
                if text is not None:
                    item["output"] = text
                return bent
    raise AssertionError("the synthetic control carried no projection marker")


def shorten_witness(body: dict) -> dict:
    """Return a copy of `body` whose witness no longer holds a whole read body."""
    bent = json.loads(json.dumps(body))
    witness = witness_of(body)
    for item in bent["input"]:
        # A call and its result share a `call_id`, so match the result shape.
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == witness
        ):
            item["output"] = "too small to be the retained evidence"
            return bent
    raise AssertionError("the synthetic control carried no witness result item")


def main() -> int:
    work = tempfile.mkdtemp(prefix="jev-projection-self-test-")
    control = control_body()
    projected, report = project(control, switch_on=True)
    unchanged, off_report = project(control, switch_on=False)
    if len(report.get("receipts") or []) != 1:
        raise AssertionError("the synthetic control is not projectable at all")
    if len(off_report.get("receipts") or []) != 0:
        raise AssertionError("the switch-off replay wrote a receipt")
    if json.dumps(unchanged, sort_keys=True) != json.dumps(control, sort_keys=True):
        raise AssertionError("the switch-off replay did not return the body unchanged")

    declining_stage = os.path.join(work, "declining_stage.py")
    with open(declining_stage, "w") as handle:
        handle.write(DECLINING_STAGE)

    failures = []
    rollout_ok = json.dumps(control)
    good = {
        "on": projected,
        "off": control,
        "none": control,
        "on_rollout": rollout_ok,
        "off_rollout": rollout_ok,
        "repo_root": REPO_ROOT,
        "stage_command": STAGE_COMMAND,
    }
    code, out = run_checker(work, case=good)
    if code != 0:
        failures.append(f"the checker rejected a good run:\n{out}")

    #: Each control must make the checker fail -- and fail for its own reason.
    controls = {
        "no projection at all": {"on": control},
        "switch off did not reset": {"off": projected},
        "item count dropped": {"on": {**projected, "input": projected["input"][:-1]}},
        "marker names a missing witness": {
            "on": rewrite_marker(
                projected,
                text=(
                    "[Jev prune: repeated read-result body omitted; "
                    "retained witness: call_does_not_exist]"
                ),
            )
        },
        "marker replaced its own witness": {
            "on": rewrite_marker(projected, call_id=witness_of(projected)),
        },
        "witness holds no full body": {"on": shorten_witness(projected)},
        "unswitched control already carried a marker": {"none": projected},
        "rollout leaked the projection": {"on_rollout": json.dumps(projected)},
        "rollout lost the original body": {"on_rollout": "{}"},
        "the replay leg has no teeth": {
            "stage_command": f"{sys.executable} {declining_stage}",
        },
    }
    for name, override in controls.items():
        case = dict(good)
        case.update(override)
        code, out = run_checker(work, case=case)
        if code == 0:
            failures.append(f"the checker accepted a hollow run: {name}")
        elif "FAIL" not in out:
            failures.append(f"the checker failed {name!r} without saying why:\n{out}")

    # A run that records no replay is still checked on the wire alone. It must
    # not crash, and it must not report replay facts it never took.
    wire_only = {
        "on": projected,
        "off": control,
        "none": control,
        "on_rollout": rollout_ok,
        "off_rollout": rollout_ok,
    }
    code, out = run_checker(work, case=wire_only)
    if code != 0:
        failures.append(f"the checker rejected a wire-only run:\n{out}")
    elif "adapter replay" in out:
        failures.append("a wire-only run reported replay facts it never took:\n" + out)

    shutil.rmtree(work, ignore_errors=True)
    for failure in failures:
        print(f"self-test FAIL: {failure}")
    if failures:
        return 1
    print(
        "self-test ok: the checker accepts a real projection and rejects "
        f"{len(controls)} hollow variants"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
