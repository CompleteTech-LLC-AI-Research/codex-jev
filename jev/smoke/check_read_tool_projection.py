#!/usr/bin/env python3
"""Assert a real host run projected its outgoing view and reset exactly.

Inputs:
  --on-dir DIR        recorded requests from the run with the projection switch on
  --off-dir DIR       recorded requests from the run with the switch off
  --none-dir DIR      recorded requests from a run with no bus adapter at all
  --on-sessions DIR   optional rollout directory of the switch-on run
  --off-sessions DIR  optional rollout directory of the switch-off run
  --repo-root DIR     repository root, for the deterministic adapter replay
  --stage-command CMD the dedup stage command the run used
  --json              emit a machine-readable summary

Every assertion is asserted from the *recorded request bodies*, because a
successful projection is invisible in the host's own logs: the boundary writes
no event on the happy path, so the wire is the only witness.

Failures are reported one per line and exit code 1; a clean run exits 0.
Standard library only. This is the `real-host-binary` tier: a `codex` binary
built from the merged revision is driven against a loopback mock. No provider is
contacted, so the run measures serialized bytes on the wire and *not* tokens,
and it says nothing about live-model behaviour.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

#: The replacement the pinned dedup policy proves, copied from
#: `jev/scripts/dedup_receipts.py`. The host re-derives it from the call's own
#: name, arguments and bodies; a stage cannot propose anything else.
MARKER_RE = re.compile(
    r"^\[Jev prune: repeated read-result body omitted; retained witness: "
    r"(?P<witness>[^\]\s]+)\]$"
)

#: Per-run volatile identifiers. Two independent host runs cannot produce these
#: equal, so they are masked before the "exact reset" comparison. Masking is
#: deliberately narrow: it covers only values the *host* mints per run, never
#: anything the fixture or the projection controls.
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
HOST_ITEM_ID_RE = re.compile(r"\b(?:fco|msg|fc|rs|item)_[0-9a-z-]{4,}\b")
#: Some headers carry their volatility as a *JSON string* rather than as keys the
#: walk can see, so the same names are masked inside the string too.
EMBEDDED_VOLATILE_RE = re.compile(
    r'"(?:installation_id|session_id|thread_id|turn_id|root_turn_id|window_id'
    r'|context_window_id|turn_started_at_unix_ms)":(?:"[^"]*"|\d+)'
)
UUID_KEYS = frozenset(
    {
        "session_id",
        "thread_id",
        "turn_id",
        "root_turn_id",
        "x-codex-installation-id",
        "x-codex-window-id",
        "prompt_cache_key",
        "id",
    }
)


def load_requests(directory: str) -> list[dict]:
    """Load recorded requests with the turn label the mock wrote beside them."""
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
        raw_path = os.path.join(directory, name)
        requests.append(
            {
                "name": name,
                "body": body,
                "turn": turn,
                "bytes": os.path.getsize(raw_path),
                "path": raw_path,
            }
        )
    return requests


def outputs(body: dict) -> list[dict]:
    items = body.get("input")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


def calls(body: dict) -> list[dict]:
    items = body.get("input")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]


def affected(requests: list[dict]) -> dict | None:
    """The last recorded turn that carries tool results.

    That is the resumed second user turn: it resends the whole transcript, so it
    is the first request in which the duplicate pair has been pushed out of the
    current user turn *and* out of the `RECENT = 16` tail the policy protects.
    """
    for request in reversed(requests):
        if outputs(request["body"]):
            return request
    return None


def markers(body: dict) -> list[tuple[str, str]]:
    found = []
    for item in outputs(body):
        text = item.get("output")
        if isinstance(text, str) and "[Jev prune:" in text:
            found.append((item.get("call_id"), text))
    return found


def normalize(value, key: str = ""):
    """Mask per-run volatile identifiers, then hand back a comparable value."""
    if isinstance(value, dict):
        return {
            k: ("<volatile>" if k in UUID_KEYS else normalize(v, k))
            for k, v in sorted(value.items())
        }
    if isinstance(value, list):
        return [normalize(item, key) for item in value]
    if isinstance(value, str):
        masked = UUID_RE.sub("<uuid>", value)
        masked = EMBEDDED_VOLATILE_RE.sub('"<volatile>"', masked)
        masked = HOST_ITEM_ID_RE.sub("<item>", masked)
        return masked
    return value


def serialize(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def replay(
    repo_root: str,
    body_path: str,
    *,
    switch_on: bool,
    stage_command: str,
    session: str = "jev-projection-replay",
) -> tuple[dict, dict]:
    """Run the boundary adapter exactly the way the host invokes it.

    This is the deterministic half of the evidence: the same recorded control
    body in, the adapter's own report out. It is what makes "exact reset" and
    "no `projection_receipt`" checkable byte-for-byte, because a host run mints
    fresh session and item identifiers on every attempt.
    """
    adapter = os.path.join(repo_root, "jev", "scripts", "bus_boundary.py")
    environment = dict(os.environ)
    environment["JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS"] = "1" if switch_on else "0"
    with open(body_path, "rb") as handle:
        payload = handle.read()
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            adapter,
            "apply",
            "--request",
            "-",
            "--json",
            "--session",
            session,
            "--turn",
            "replay",
            "--workspace",
            "/",
            "--stage",
            f"dedup={stage_command}",
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"adapter replay failed ({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    decoded = json.loads(completed.stdout.decode("utf-8"))
    return decoded["request"], decoded["report"]


def rollout_markers(sessions: str) -> tuple[list[str], bool]:
    """Return (paths whose rollout carries a marker, whether a read body is kept)."""
    marked = []
    kept = False
    for root, _dirs, files in os.walk(sessions):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if "[Jev prune:" in text:
                marked.append(path)
            if "deterministic memory probe content" in text:
                kept = True
    return marked, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--on-dir", required=True)
    parser.add_argument("--off-dir", required=True)
    parser.add_argument("--none-dir", required=True)
    parser.add_argument("--on-sessions", default="")
    parser.add_argument("--off-sessions", default="")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--stage-command", default="")
    parser.add_argument("--expect-marker-bytes", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    failures: list[str] = []
    facts: dict = {"tier": "real-host-binary"}

    on_requests = load_requests(args.on_dir)
    off_requests = load_requests(args.off_dir)
    none_requests = load_requests(args.none_dir)
    if not on_requests or not off_requests or not none_requests:
        print("no recorded requests: the host run produced nothing to check")
        return 1

    on_turn = affected(on_requests)
    off_turn = affected(off_requests)
    none_turn = affected(none_requests)
    if on_turn is None or off_turn is None or none_turn is None:
        print(
            "no recorded turn carried tool results: the projection cannot be observed"
        )
        return 1

    on_items = len(on_turn["body"].get("input") or [])
    off_items = len(off_turn["body"].get("input") or [])
    none_items = len(none_turn["body"].get("input") or [])
    facts["on_bytes"] = on_turn["bytes"]
    facts["off_bytes"] = off_turn["bytes"]
    facts["on_items"] = on_items
    facts["off_items"] = off_items
    facts["reduction_bytes"] = off_turn["bytes"] - on_turn["bytes"]

    # 1. The switch on shrinks the affected turn's `input` and changes no count.
    if not on_turn["bytes"] < off_turn["bytes"]:
        failures.append(
            "switch on did not shrink the affected turn: "
            f"{on_turn['bytes']} bytes on vs {off_turn['bytes']} bytes off"
        )
    if on_items != off_items:
        failures.append(
            f"item count changed under projection: {on_items} on vs {off_items} off"
        )
    if len(outputs(on_turn["body"])) != len(outputs(off_turn["body"])):
        failures.append("tool-result count changed under projection")

    # 2. The switch off resets exactly and writes no receipt.
    off_markers = markers(off_turn["body"])
    none_markers = markers(none_turn["body"])
    if off_markers:
        failures.append(
            f"switch off left {len(off_markers)} projection marker(s) in the body"
        )
    if none_markers:
        failures.append("the unswitched control body already carried a marker")
    if off_items != none_items:
        failures.append(
            f"switch off differs from the unswitched control by item count: "
            f"{off_items} off vs {none_items} none"
        )
    reset_on_the_wire = serialize(normalize(off_turn["body"])) == serialize(
        normalize(none_turn["body"])
    )
    if not reset_on_the_wire:
        failures.append(
            "switch off is not byte-identical to the unswitched control once "
            "per-run identifiers are masked: the boundary did not reset exactly"
        )
    facts["off_matches_unswitched_control"] = reset_on_the_wire

    # 3. The switch on projected the pair the pinned policy proves.
    on_markers = markers(on_turn["body"])
    facts["on_markers"] = len(on_markers)
    if len(on_markers) != 1:
        failures.append(
            f"expected exactly one projected body with the switch on, found {len(on_markers)}"
        )
    for call_id, text in on_markers:
        match = MARKER_RE.match(text)
        if match is None:
            failures.append(f"marker does not match the pinned shape: {text!r}")
            continue
        witness = match.group("witness")
        witness_ids = {item.get("call_id") for item in outputs(on_turn["body"])}
        if witness not in witness_ids:
            failures.append(f"marker names witness {witness!r}, which the body lacks")
        if call_id == witness:
            failures.append("the marker replaced its own witness")
        by_call = {item.get("call_id"): item for item in outputs(on_turn["body"])}
        retained = by_call.get(witness, {}).get("output")
        if not isinstance(retained, str) or len(retained) < 256:
            failures.append(
                f"retained witness {witness!r} does not hold a full read body"
            )
    if args.expect_marker_bytes and on_markers:
        longest = max(len(text) for _call, text in on_markers)
        if longest > args.expect_marker_bytes:
            failures.append("marker is larger than the body it replaced")

    # 4. The deterministic replay: exact reset, and one receipt only when on.
    if args.repo_root and args.stage_command:
        try:
            out_on, report_on = replay(
                args.repo_root,
                off_turn["path"],
                switch_on=True,
                stage_command=args.stage_command,
            )
            out_off, report_off = replay(
                args.repo_root,
                off_turn["path"],
                switch_on=False,
                stage_command=args.stage_command,
            )
        except (RuntimeError, KeyError, ValueError) as error:
            failures.append(f"adapter replay failed: {error}")
        else:
            with open(off_turn["path"], "rb") as handle:
                control = handle.read()
            reset = json.dumps(out_off, separators=(",", ":")) == json.dumps(
                json.loads(control), separators=(",", ":")
            )
            facts["replay_exact_reset"] = reset
            facts["replay_receipts_on"] = len(report_on.get("receipts") or [])
            facts["replay_receipts_off"] = len(report_off.get("receipts") or [])
            facts["replay_applied_off"] = report_off.get("applied") or []
            if not reset:
                failures.append(
                    "switch off did not return the recorded body unchanged "
                    "through the adapter: exact reset is not proven"
                )
            if len(report_off.get("applied") or []) != 0:
                failures.append("switch off still applied a stage")
            if len(report_off.get("receipts") or []) != 0:
                failures.append("a projection_receipt was written with the switch off")
            if len(report_on.get("receipts") or []) != 1:
                failures.append(
                    "the switch-on replay did not write exactly one projection_receipt"
                )
            replay_markers = markers(out_on)
            if len(replay_markers) != 1:
                failures.append("the switch-on replay did not project exactly one body")
            elif on_markers and replay_markers[0][1] != on_markers[0][1]:
                failures.append(
                    "the replayed projection differs from the one that reached the wire"
                )

    # 5. The persisted transcript is untouched in both runs.
    for label, sessions in (("on", args.on_sessions), ("off", args.off_sessions)):
        if not sessions:
            continue
        marked, kept = rollout_markers(sessions)
        facts[f"rollout_{label}_marks"] = len(marked)
        facts[f"rollout_{label}_keeps_full_body"] = kept
        if marked:
            failures.append(
                f"the {label} run persisted a projected body into its rollout: "
                + ", ".join(marked)
            )
        if not kept:
            failures.append(
                f"the {label} run's rollout does not hold the original read body"
            )

    facts["failures"] = failures
    if args.json:
        print(json.dumps(facts, indent=2, sort_keys=True))
    else:
        print(
            "tier: real-host-binary (a codex binary from the merged revision, "
            "driven against a loopback mock; no provider was contacted)"
        )
        print(
            "unmeasured: token metrics and live-provider behaviour -- the "
            "reduction below is serialized bytes on the wire, never tokens"
        )
        print(
            f"affected turn: {facts['off_bytes']} bytes off -> "
            f"{facts['on_bytes']} bytes on ({facts['reduction_bytes']} bytes fewer), "
            f"items {facts['off_items']} -> {facts['on_items']}"
        )
        if "replay_receipts_on" in facts:
            print(
                "adapter replay: exact reset "
                f"{facts['replay_exact_reset']}, receipts on "
                f"{facts['replay_receipts_on']} / off {facts['replay_receipts_off']}"
            )
        for failure in failures:
            print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
