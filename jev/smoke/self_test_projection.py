#!/usr/bin/env python3
"""Self-test for the projection/reset fixture's checker.

The end-to-end harness needs a built Codex binary, so CI cannot run it. What CI
can gate is the half that must never rot: ``check_projection_reset.py`` has to
accept a faithful verdict set and *reject* every way the evidence could be
wrong. A checker that cannot fail is not evidence, so the negative controls are
part of the fixture's own suite.

Standard library only. Writes small JSON files under ``TMPDIR``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "check_projection_reset.py")

SESSION = "01a0c22b-0000-7000-8000-000000000070"
SOURCE_CALL = "call_jev_proj_source"
WITNESS_CALL = "call_jev_proj_witness"
MARKER = (
    "[Jev prune: repeated read-result body omitted; retained witness: "
    "call_jev_proj_witness]"
)
ARGUMENTS = '{"path":"fixture/status.txt"}'
BODY = "".join(
    f"seeded read evidence: the fixture project reports nominal progress for stage {n:03d}.\n"
    for n in range(8)
)


def message(role: str, text: str) -> dict:
    part = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": part, "text": text}]}


def input_items(replace_source: bool) -> list[dict]:
    items = [
        message("developer", "<skills_instructions>fixture</skills_instructions>"),
        message("user", "Inspect the fixture status file."),
        {
            "type": "function_call",
            "name": "read",
            "arguments": ARGUMENTS,
            "call_id": SOURCE_CALL,
        },
        {
            "type": "function_call_output",
            "call_id": SOURCE_CALL,
            "output": MARKER if replace_source else BODY,
        },
        {
            "type": "function_call",
            "name": "read",
            "arguments": ARGUMENTS,
            "call_id": WITNESS_CALL,
        },
        {"type": "function_call_output", "call_id": WITNESS_CALL, "output": BODY},
    ]
    for n in range(28):
        items.append(message("user" if n % 2 == 0 else "assistant", f"bookkeeping {n}"))
    items.append(
        message("user", "Please summarise the status of the seeded fixture project.")
    )
    return items


def write_case(
    root: str,
    name: str,
    items: list[dict],
    *,
    rollout_body: str = BODY,
    exit_status: str = "0",
    chain: dict | None = None,
    seed: dict | None = None,
) -> None:
    case = os.path.join(root, name)
    os.makedirs(os.path.join(case, "requests"), exist_ok=True)
    with open(
        os.path.join(case, "requests", "request-00.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump({"input": items, "client_metadata": {"turn_id": "fixture"}}, handle)
    with open(os.path.join(case, "exit-status"), "w", encoding="utf-8") as handle:
        handle.write(exit_status)
    record = {
        "timestamp": "2026-09-21T00:00:00.000Z",
        "ordinal": 0,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": SOURCE_CALL,
            "output": rollout_body,
        },
    }
    with open(os.path.join(case, "rollout.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    if chain is not None:
        with open(os.path.join(case, "chain.json"), "w", encoding="utf-8") as handle:
            json.dump(chain, handle)
    if seed is not None:
        with open(os.path.join(case, "seed.json"), "w", encoding="utf-8") as handle:
            json.dump(seed, handle)


def good_chain() -> dict:
    return {
        "report": {
            "applied": ["jev-prune.dedup"],
            "dedup": {"accepted": [3], "reverted": [], "receipts": []},
            "receipts": [{"kind": "projection_receipt"}],
        }
    }


def write_fixture(root: str) -> None:
    seed = {
        "session": SESSION,
        "read_body_sha256": hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
    }
    write_case(root, "pristine", input_items(False), seed=seed)
    write_case(root, "off", input_items(False))
    write_case(root, "on", input_items(True), chain=good_chain())


def run_checker(root: str) -> tuple[int, dict]:
    result = subprocess.run(
        [
            sys.executable,
            CHECKER,
            "--workdir",
            root,
            "--pristine",
            os.path.join(root, "pristine"),
            "--off",
            os.path.join(root, "off"),
            "--on",
            os.path.join(root, "on"),
            "--source-call",
            SOURCE_CALL,
            "--witness-call",
            WITNESS_CALL,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        verdict = json.loads(result.stdout)
    except ValueError:
        verdict = {}
    return result.returncode, verdict


def failed_checks(verdict: dict) -> set[str]:
    return {entry["check"] for entry in verdict.get("checks", []) if not entry["ok"]}


def main() -> int:
    root = tempfile.mkdtemp(prefix="jev-projection-selftest-")
    failures: list[str] = []
    try:
        write_fixture(root)
        code, verdict = run_checker(root)
        if code != 0 or not verdict.get("ok"):
            failures.append(f"faithful fixture was rejected: {failed_checks(verdict)}")

        controls = {
            "no-reduction": lambda: write_case(
                root, "on", input_items(False), chain=good_chain()
            ),
            "reset-drift": lambda: write_case(
                root, "off", input_items(True), chain=good_chain()
            ),
            "count-changed": lambda: write_case(
                root, "on", input_items(True)[1:], chain=good_chain()
            ),
            "receipt-missing": lambda: write_case(
                root,
                "on",
                input_items(True),
                chain={"report": {"applied": [], "receipts": []}},
            ),
            "rollout-projected": lambda: write_case(
                root, "on", input_items(True), rollout_body=MARKER, chain=good_chain()
            ),
            "host-failed": lambda: write_case(
                root, "on", input_items(True), exit_status="1", chain=good_chain()
            ),
        }
        expected = {
            "no-reduction": "reduction-strictly-smaller",
            "reset-drift": "exact-reset",
            "count-changed": "item-count-preserved",
            "receipt-missing": "receipt-emitted-only-when-switched-on",
            "rollout-projected": "on-rollout-unprojected",
            "host-failed": "on-host-exit",
        }
        for name, mutate in controls.items():
            write_fixture(root)
            mutate()
            code, verdict = run_checker(root)
            if code == 0 or verdict.get("ok"):
                failures.append(f"negative control {name!r} was accepted")
            elif expected[name] not in failed_checks(verdict):
                failures.append(
                    f"negative control {name!r} failed the wrong check: {failed_checks(verdict)}"
                )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        for failure in failures:
            print(f"self-test: FAIL {failure}")
        return 1
    print("self-test: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
