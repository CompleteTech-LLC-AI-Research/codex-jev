#!/usr/bin/env python3
"""Write a pinned-component receipt store proving one duplicate-read replacement.

The dedup stage is ``jev-prune-kit``. Its ``project`` step only replaces a body
when an approved receipt names the source and the retained witness by native
identity *and* re-derives both proofs from the request it is handed, so the
receipts must be computed with the component's own proof formula over exactly
the message array the host boundary will normalise the wire ``input`` into.

This script therefore imports the boundary adapter for the normalisation and the
pinned component for the proof, instead of restating either. It also dry-runs
both the component's ``project`` and the adapter's C3 re-derivation so a
receipt that would be silently ignored is reported here rather than at the end
of a host run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_modules(repo: Path, kit: Path):
    sys.path.insert(0, str(repo / "jev" / "scripts"))
    sys.path.insert(0, str(kit))
    import bus_boundary  # noqa: PLC0415 - resolved after sys.path setup
    from jev_prune import core as kit_core  # noqa: PLC0415

    return bus_boundary, kit_core


def build_receipt(bus_boundary, kit_core, items, session, source_id, witness_id):
    messages = bus_boundary.normalize_input(items)
    snapshot = kit_core.snapshot("openai", session, messages)
    by_id = {item.id: item for item in snapshot.items}
    missing = [call for call in (source_id, witness_id) if call not in by_id]
    if missing:
        raise SystemExit(f"seed did not yield the expected read results: {missing}")
    source, witness = by_id[source_id], by_id[witness_id]
    if not (source.eligible and witness.eligible):
        raise SystemExit("seeded read pair is not eligible in the component's terms")
    if source.request_key != witness.request_key or source.text != witness.text:
        raise SystemExit("seeded read pair does not share arguments and body")
    if source.index >= witness.index:
        raise SystemExit("seeded witness does not follow the source")
    return (
        {
            "policy": kit_core.POLICY,
            "model": kit_core.MODEL,
            "session": session,
            "format": "openai",
            "source": source.proof(),
            "witness": witness.proof(),
        },
        snapshot,
        source,
        witness,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request", required=True, help="recorded switch-off request body"
    )
    parser.add_argument("--session", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--repo", required=True, help="codex-jev checkout root")
    parser.add_argument("--kit", required=True, help="pinned jev-prune-kit root")
    parser.add_argument("--source-call", required=True)
    parser.add_argument("--witness-call", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    bus_boundary, kit_core = load_modules(Path(args.repo), Path(args.kit))
    body = json.loads(Path(args.request).read_text(encoding="utf-8"))
    items = body.get("input")
    receipt, snapshot, source, witness = build_receipt(
        bus_boundary, kit_core, items, args.session, args.source_call, args.witness_call
    )

    kit_core._validate_receipts([receipt])  # noqa: SLF001 - the component's own validator
    _, report = kit_core.project(snapshot, [receipt])
    if report.get("applied") != 1:
        raise SystemExit(f"component refused the receipt: {report}")

    state_dir = Path(os.path.abspath(args.state_dir))
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = state_dir / (kit_core.digest([args.profile, args.session]) + ".json")
    store = {
        "schema": "jev-prune.receipts.v1",
        "profile": args.profile,
        "session": args.session,
        "receipts": [receipt],
    }
    target.write_text(
        json.dumps(store, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    summary = {
        "store": str(target),
        "profile": args.profile,
        "session": args.session,
        "source": args.source_call,
        "witness": args.witness_call,
        "normalized_item_count": len(snapshot.messages),
        "source_index": source.index,
        "witness_index": witness.index,
        "component_report": report,
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
