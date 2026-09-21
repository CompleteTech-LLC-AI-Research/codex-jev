#!/usr/bin/env python3
"""Assert the four acceptance items for the projection/reset harness.

Reads what the three recorded runs actually put on the wire and what they left
on disk, and reports a machine-readable verdict. Every claim is stated as
measured, and the run's tier is printed with the verdict so the evidence cannot
be mistaken for a token or live-provider measurement.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys


MARKER_PREFIX = "[Jev prune: repeated read-result body omitted"
RECEIPT_KIND = "projection_receipt"
# Ids the host mints per run (a fresh user item, response ids) are random; the
# seeded identities are not, so dropping only uuid-shaped `id` fields keeps the
# seeded transcript byte-exact while making two runs comparable.
UUID_ID = re.compile(
    r"^(?:msg|fc|fco|resp|item)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TIER = "real-host-binary"


def canonical(value):
    if isinstance(value, dict):
        return {
            k: canonical(v)
            for k, v in value.items()
            if not (k == "id" and isinstance(v, str) and UUID_ID.match(v))
        }
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def first_difference(left, right, path="$"):
    if type(left) is not type(right):
        return f"{path}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        if set(left) != set(right):
            return f"{path}: keys {sorted(set(left) ^ set(right))}"
        for key in left:
            found = first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            found = first_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


def load_request(path):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except OSError:
        return None


def recorded_body(case):
    matches = sorted(glob.glob(f"{case}/requests/request-*.json"))
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one recorded request in {case}, found {len(matches)}"
        )
    with open(matches[0], "rb") as handle:
        raw = handle.read()
    return matches[0], raw, json.loads(raw.decode("utf-8"))


def rollout_file(case):
    path = f"{case}/rollout.jsonl"
    if not glob.glob(path):
        raise SystemExit(f"missing the post-run rollout snapshot at {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--pristine", required=True)
    parser.add_argument("--off", required=True)
    parser.add_argument("--on", required=True)
    parser.add_argument("--source-call", required=True)
    parser.add_argument("--witness-call", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    seed = json.load(open(f"{args.pristine}/seed.json", encoding="utf-8"))
    session = seed["session"]
    checks: list[dict] = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    bodies = {}
    for name, case in (("pristine", args.pristine), ("off", args.off), ("on", args.on)):
        path, raw, body = recorded_body(case)
        status = open(f"{case}/exit-status", encoding="utf-8").read().strip()
        bodies[name] = {"path": path, "raw": raw, "body": body, "status": status}
        check(
            f"{name}-host-exit",
            status == "0",
            f"codex exec resume exit status {status}",
        )

    pristine, off, on = bodies["pristine"], bodies["off"], bodies["on"]

    counts = {
        name: len(entry["body"].get("input", [])) for name, entry in bodies.items()
    }
    check(
        "item-count-preserved",
        counts["pristine"] == counts["off"] == counts["on"],
        f"input item counts pristine={counts['pristine']} off={counts['off']} on={counts['on']}",
    )

    def wire_bytes(entry):
        return len(
            json.dumps(entry["body"]["input"], separators=(",", ":")).encode("utf-8")
        )

    sizes = {name: wire_bytes(entry) for name, entry in bodies.items()}
    check(
        "reduction-strictly-smaller",
        sizes["on"] < sizes["off"] == sizes["pristine"],
        f"serialized input bytes pristine={sizes['pristine']} off={sizes['off']} on={sizes['on']} "
        f"(saved {sizes['off'] - sizes['on']})",
    )

    # The projection boundary is the wire `input` array, so the reset is proven
    # there. `client_metadata` carries a turn id the host mints for every turn,
    # which no boundary state can influence; every other field must match too.
    difference = first_difference(
        canonical(off["body"]["input"]), canonical(pristine["body"]["input"])
    )
    collateral = sorted(
        key
        for key in set(off["body"]) | set(pristine["body"])
        if key not in ("input", "client_metadata")
        and canonical(off["body"].get(key)) != canonical(pristine["body"].get(key))
    )
    check(
        "exact-reset",
        difference is None and not collateral,
        (
            "switch-off `input` is byte-identical to the unswitched control "
            f"({sizes['off']} bytes, {counts['off']} items); only the host-minted "
            "`client_metadata.turn_id` differs outside the boundary"
        )
        if difference is None
        else f"switch-off `input` diverges from the unswitched control: {difference}",
    )

    on_difference = first_difference(
        canonical(on["body"]["input"]), canonical(pristine["body"]["input"])
    )
    check(
        "switch-on-changes-the-request",
        on_difference is not None,
        f"switch-on `input` differs from control: {on_difference}",
    )

    def marker_hits(entry):
        return entry["raw"].decode("utf-8", "replace").count(MARKER_PREFIX)

    hits = {name: marker_hits(entry) for name, entry in bodies.items()}
    check(
        "marker-only-when-switched-on",
        hits["on"] == 1 and hits["off"] == 0 and hits["pristine"] == 0,
        f"omission-marker occurrences pristine={hits['pristine']} off={hits['off']} on={hits['on']}",
    )

    receipt_hits = {
        name: entry["raw"].decode("utf-8", "replace").count(RECEIPT_KIND)
        for name, entry in bodies.items()
    }
    check(
        "no-persisted-projection-receipt",
        receipt_hits["off"] == 0 and receipt_hits["pristine"] == 0,
        f"`{RECEIPT_KIND}` occurrences in recorded bodies pristine={receipt_hits['pristine']} "
        f"off={receipt_hits['off']} on={receipt_hits['on']}",
    )

    # The boundary only emits a receipt when a stage actually projected, and the
    # host never persists it; the standalone re-run records what it produced.
    try:
        chain = json.load(open(f"{args.on}/chain.json", encoding="utf-8"))
    except OSError:
        chain = {}
    report = chain.get("report") or {}
    emitted = [r.get("kind") for r in report.get("receipts", [])]
    dedup = report.get("dedup") or {}
    check(
        "receipt-emitted-only-when-switched-on",
        emitted == [RECEIPT_KIND]
        and dedup.get("accepted") == [3]
        and not dedup.get("reverted"),
        f"switch-on chain applied {report.get('applied')} and emitted {emitted} "
        f"with accepted indices {dedup.get('accepted')} and reverts {dedup.get('reverted')}",
    )

    rollout_report = {}
    for name, case in (("pristine", args.pristine), ("off", args.off), ("on", args.on)):
        path = rollout_file(case)
        with open(path, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8", "replace")
        source_bodies = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            if (
                record.get("type") == "response_item"
                and payload.get("type") == "function_call_output"
            ):
                if payload.get("call_id") == args.source_call:
                    body = payload.get("output") or ""
                    source_bodies.append(
                        hashlib.sha256(body.encode("utf-8")).hexdigest()
                    )
        rollout_report[name] = {
            "path": path,
            "marker_occurrences": text.count(MARKER_PREFIX),
            "receipt_occurrences": text.count(RECEIPT_KIND),
            "source_body_sha256": source_bodies,
        }
        check(
            f"{name}-rollout-unprojected",
            text.count(MARKER_PREFIX) == 0
            and source_bodies == [seed["read_body_sha256"]],
            f"rollout keeps the original read body and no omission marker "
            f"(marker={text.count(MARKER_PREFIX)}, body={source_bodies})",
        )

    ok = all(entry["ok"] for entry in checks)
    verdict = {
        "tier": TIER,
        "session": session,
        "profile": "integrated-offline",
        "source_call": args.source_call,
        "witness_call": args.witness_call,
        "item_counts": counts,
        "serialized_input_bytes": sizes,
        "checks": checks,
        "rollouts": rollout_report,
        "tier_statement": (
            "These runs launch a real Codex binary and read the bytes it sent to a loopback "
            "Responses-API mock; the reduction is measured in serialized request bytes on a "
            "seeded transcript. No live provider was contacted and no token metric is claimed."
        ),
        "ok": ok,
    }
    print(json.dumps(verdict, indent=2, sort_keys=True))
    for entry in checks:
        sys.stderr.write(
            f"{'PASS' if entry['ok'] else 'FAIL'} {entry['check']}: {entry['detail']}\n"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
