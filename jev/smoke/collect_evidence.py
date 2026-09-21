#!/usr/bin/env python3
"""Collapse per-binary smoke verdicts into one machine-readable summary.

Each `--tag NAME --binary PATH` pair names a scratch directory that
`run-plaintext-smoke.sh` populated (it writes `verdict.json`). The summary
records the binary hash, the observed turn script, whether the run passed, and
the checker's failure list, so the evidence can be diffed across rebuilds
without keeping every request body in the repository.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize(tag: str, binary: str, evidence: str) -> dict:
    workdir = os.path.join(evidence, tag)
    verdict_path = os.path.join(workdir, "verdict.json")
    record: dict = {
        "tag": tag,
        "binary": binary,
        "workdir": workdir,
        "binary_present": os.path.exists(binary),
    }
    if record["binary_present"]:
        record["sha256"] = sha256(binary)
    else:
        record["sha256"] = None

    if not os.path.exists(verdict_path):
        record.update(
            {
                "ok": False,
                "turns": [],
                "failures": [f"no verdict was recorded at {verdict_path}"],
            }
        )
        return record

    with open(verdict_path) as handle:
        verdict = json.load(handle)
    record.update(
        {
            "ok": bool(verdict.get("ok")),
            "turns": verdict.get("turns", []),
            "failures": verdict.get("failures", []),
            "message_schemas": verdict.get("message_schemas", {}),
            "rollout": verdict.get("rollout", {}),
            "requests": verdict.get("requests"),
        }
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--binary", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if len(args.tag) != len(args.binary):
        print("error: --tag/--binary must be given in pairs", file=sys.stderr)
        return 2

    records = [
        summarize(tag, binary, args.evidence)
        for tag, binary in zip(args.tag, args.binary)
    ]
    with open(args.out, "w") as handle:
        json.dump(records, handle, indent=2, sort_keys=True)
        handle.write("\n")
    for record in records:
        print(f"{record['tag']:>22}  ok={record['ok']}  turns={record['turns']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
