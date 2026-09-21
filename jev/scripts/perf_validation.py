#!/usr/bin/env python3
"""Offline integration-performance validation for phase 6.2 (#25).

Phase 6.2 asks to "measure representative baseline versus integrated workloads"
and to report correctness, payload bytes, measured tokens, latency, fallback
rates, failures, and service usage without dropping unsuccessful cases. Paid
inference is outside this authorization, so this harness measures the thing the
integration actually changes — the outgoing wire request and the boundary that
rewrites it — and never contacts a provider.

What is measured
----------------
* **Payload bytes** — the serialized ``input`` array before and after the bus
  boundary, so "baseline" is the untouched request and "integrated" is what
  would go on the wire.
* **Tokens** — ``tokens_measured`` is always ``null``; the only token figure is
  ``tokens_estimated``, explicitly a byte-derived estimate. A byte count is a
  measurement; a token count is not, offline.
* **Latency** — the observed wall-clock time of the boundary call, sampled over
  ``--repetitions`` and reported as min/median/max. This is local boundary
  latency, not model latency.
* **Fallback rate** — the cases that must leave the request byte-identical
  (disabled switches, a cancelled turn, a protected tail, a stale view) are
  counted as fallbacks, and each carries the reason.
* **Failures** — a case that refuses (e.g. an unapproved view) is counted and
  named with its stable code, never dropped.
* **Service usage** — ``provider_requests`` is fixed at ``0`` and no credential
  is read. The harness binds no socket and resolves nothing off-host.

Evidence tiers
--------------
Stage 100 (dedup) runs over the real subprocess transport with the checked-in
stage double, so it is ``bus-stage-stub``; the stage 200 view and the fallback
paths are host-side ``offline-fixture``. ``real_host`` and ``live_provider`` are
deliberately empty: those belong to the ``jev/smoke`` host harnesses and to a
separately consented live run.

Usage
-----
    python3 jev/scripts/perf_validation.py run --json /tmp/perf.json
    python3 jev/scripts/perf_validation.py run --repetitions 9 --quiet

Exit codes: 0 every case behaved as declared, 1 a case did not, 2 the inputs
were unusable (a missing fixture file).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bus_boundary  # noqa: E402
import fabric_views  # noqa: E402

SCHEMA = "jev-perf-validation.v1"
OFFLINE_TIER = "offline-fixture"
BUS_STUB_TIER = "bus-stage-stub"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "e2e_fixtures"
WIRE = "wire-request.json"
WIRE_PROTECTED = "wire-request-protected.json"

# The pinned fixture's own binding, matching jev/tests/e2e_fixtures.
SESSION = "01a0-e2e-fixture-session"
TURN = "turn-e2e-fixture-1"
PROTECTED_TURN = "turn-e2e-fixture-2"
WORKSPACE = "/workspace/demo"

DEDUP_STAGE = bus_boundary.DEDUP_STAGE
VIEW_STAGE = bus_boundary.VIEW_STAGE


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bytes(items) -> int:
    return len(_canonical(items).encode("utf-8"))


def _load_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def _stub_transports() -> dict:
    stubs = Path(__file__).resolve().parents[1] / "tests" / "bus_stage_stub"
    return {
        "dedup": [sys.executable, str(stubs / "dedup.py")],
        "fabric_view": [sys.executable, str(stubs / "view.py")],
    }


def _routing_invoke(real):
    """Stage 100 over the real transport; stage 200 answered host-side.

    The pinned ``jev-context-fabric`` revision exposes no jev-bus stage entry
    point, so the host's own C4 owner applies the approved view. The dedup stage
    still runs over the wire, so the invocation order and receipts are real.
    """

    def invoke(stage, stage_request, workspace, budget):
        if stage["name"] == DEDUP_STAGE:
            return real(stage, stage_request, workspace, budget)
        return {"ok": True, "messages": stage_request["messages"], "notes": []}

    return invoke


class Measurement:
    """One declared case and the observable it must produce."""

    def __init__(self, name, tier, expect):
        self.name = name
        self.tier = tier
        self.expect = expect
        self.record: dict = {}

    def observe(self, *, before, after, ok, fallback=False, refused=False, code=""):
        self.record = {
            "name": self.name,
            "tier": self.tier,
            "ok": bool(ok),
            "bytes_before": _bytes(before),
            "bytes_after": _bytes(after),
            "items_before": len(before),
            "items_after": len(after),
            "fallback": bool(fallback),
            "refused": bool(refused),
            "code": code,
        }
        return self


def _registry(transports, enabled=True):
    state = {
        "projection.dedup_receipts": enabled,
        "projection.fabric_views": enabled,
    }
    return bus_boundary.build_registry(state, transports if enabled else {})


def measure(fixtures_dir=None, *, repetitions: int = 5) -> dict:
    """Run every declared case and return the validation document."""
    fixtures = Path(fixtures_dir) if fixtures_dir else FIXTURE_DIR
    for name in (WIRE, WIRE_PROTECTED):
        if not (fixtures / name).is_file():
            raise FileNotFoundError(f"missing fixture {fixtures / name}")

    transports = _stub_transports()
    registry = _registry(transports, enabled=True)
    invoke = _routing_invoke(bus_boundary._subprocess_invoke)

    request = _load_json(fixtures / WIRE)
    protected = _load_json(fixtures / WIRE_PROTECTED)
    baseline = copy.deepcopy(request)

    # --- the integrated path: dedup, then the approved prose view -------------
    snapshot, _ = bus_boundary.project(
        request,
        registry=registry,
        session=SESSION,
        turn=TURN,
        workspace=WORKSPACE,
        invoke=invoke,
    )
    plan = fabric_views.plan(snapshot["input"], target_bytes=10**6)
    view = fabric_views.apply(snapshot["input"], plan, approved=True)
    projected, view_report = bus_boundary.project(
        request,
        registry=registry,
        session=SESSION,
        turn=TURN,
        workspace=WORKSPACE,
        invoke=invoke,
        view=view,
    )
    integrated = projected["input"]
    removed = [item["index"] for item in view_report["view"]["removed"]]

    # --- declared cases ------------------------------------------------------
    cases: list[Measurement] = []

    cases.append(
        Measurement("integrated_dedup_and_view", BUS_STUB_TIER, "shrink").observe(
            before=baseline["input"],
            after=integrated,
            ok=_bytes(integrated) < _bytes(baseline["input"]) and removed == [3, 6],
        )
    )

    cases.append(
        Measurement("dedup_only_replaces_the_body", BUS_STUB_TIER, "shrink").observe(
            before=baseline["input"],
            after=snapshot["input"],
            ok=_bytes(snapshot["input"]) < _bytes(baseline["input"])
            and len(snapshot["input"]) == len(baseline["input"]),
        )
    )

    # Disabled switches: nothing is invoked and the bytes are untouched.
    passthrough = _routing_invoke(
        lambda stage, stage_request, workspace, budget: {
            "ok": True,
            "messages": stage_request["messages"],
            "notes": [],
        }
    )
    off_registry = _registry(transports, enabled=False)
    off_out, off_report = bus_boundary.project(
        protected,
        registry=off_registry,
        session=SESSION,
        invoke=passthrough,
    )
    cases.append(
        Measurement(
            "disabled_switches_leave_bytes_alone", OFFLINE_TIER, "identical"
        ).observe(
            before=protected["input"],
            after=off_out["input"],
            ok=off_out["input"] == protected["input"]
            and off_report["invoked"] == []
            and off_report["receipts"] == [],
            fallback=True,
        )
    )

    # A cancelled turn removes nothing.
    cancelled_out, cancelled_report = bus_boundary.project(
        request,
        registry=registry,
        session=SESSION,
        turn=TURN,
        workspace=WORKSPACE,
        invoke=invoke,
        view=view,
        cancelled=True,
    )
    cases.append(
        Measurement(
            "cancelled_turn_removes_nothing", OFFLINE_TIER, "identical"
        ).observe(
            # The view is bound to the post-dedup snapshot, so "removes nothing"
            # means it falls back to that snapshot, not to the canonical request.
            before=snapshot["input"],
            after=cancelled_out["input"],
            ok=cancelled_report["view"]["cancelled"] is True
            and cancelled_report["view"]["removed"] == []
            and cancelled_out["input"] == snapshot["input"],
            fallback=True,
        )
    )

    # A duplicate inside the protected tail is never replaced.
    protected_out, protected_report = bus_boundary.project(
        protected,
        registry=registry,
        session=SESSION,
        turn=PROTECTED_TURN,
        workspace=WORKSPACE,
        invoke=invoke,
    )
    cases.append(
        Measurement(
            "protected_tail_is_never_replaced", BUS_STUB_TIER, "identical"
        ).observe(
            before=protected["input"],
            after=protected_out["input"],
            ok=protected_report["dedup"]["accepted"] == []
            and protected_out["input"] == protected["input"],
            fallback=True,
        )
    )

    # A stale view (bound to a different snapshot) is refused, not applied.
    stale_plan = fabric_views.plan(baseline["input"], target_bytes=10**6)
    stale = fabric_views.apply(baseline["input"], stale_plan, approved=True)
    stale_out, stale_report = bus_boundary.project(
        request,
        registry=registry,
        session=SESSION,
        turn=TURN,
        workspace=WORKSPACE,
        invoke=invoke,
        view=stale,
    )
    stale_refused = any(
        note.get("action") == "refused" and note.get("detail") == "stale_view"
        for note in stale_report["notes"]
    )
    cases.append(
        Measurement("stale_view_is_refused", OFFLINE_TIER, "identical").observe(
            before=baseline["input"],
            after=stale_out["input"],
            ok=stale_refused and stale_out["input"] == baseline["input"],
            fallback=True,
            refused=True,
            code="stale_view",
        )
    )

    # An unapproved removal is reverted, not shipped.
    def drop_for_view(stage, stage_request, workspace, budget):
        messages = copy.deepcopy(stage_request["messages"])
        if stage["name"] == VIEW_STAGE:
            messages = messages[:-1]
        return {"ok": True, "messages": messages, "notes": []}

    unapproved_out, unapproved_report = bus_boundary.project(
        request,
        registry=registry,
        session=SESSION,
        turn=TURN,
        workspace=WORKSPACE,
        invoke=drop_for_view,
        view=None,
    )
    reverted = any(
        note.get("action") == "reverted" and note.get("detail") == "unapproved_removal"
        for note in unapproved_report["notes"]
    )
    cases.append(
        Measurement(
            "unapproved_removal_is_reverted", OFFLINE_TIER, "identical"
        ).observe(
            before=baseline["input"],
            after=unapproved_out["input"],
            ok=reverted and len(unapproved_out["input"]) == len(baseline["input"]),
            fallback=True,
            refused=True,
            code="unapproved_removal",
        )
    )

    # Applying a view without approval raises; count it, do not drop it.
    raise_code = ""
    try:
        fabric_views.apply(snapshot["input"], plan, approved=False)
    except fabric_views.ViewError as exc:
        raise_code = str(exc)
    cases.append(
        Measurement(
            "view_application_requires_approval", OFFLINE_TIER, "refuse"
        ).observe(
            before=baseline["input"],
            after=baseline["input"],
            ok=raise_code == "view_not_approved",
            fallback=False,
            refused=True,
            code=raise_code,
        )
    )

    # --- latency: observed, sampled, and never conflated with a model ---------
    samples = []
    for _ in range(max(1, int(repetitions))):
        start = time.perf_counter()
        bus_boundary.project(
            request,
            registry=registry,
            session=SESSION,
            turn=TURN,
            workspace=WORKSPACE,
            invoke=invoke,
            view=view,
        )
        samples.append(round((time.perf_counter() - start) * 1000.0, 3))

    before_bytes = _bytes(baseline["input"])
    after_bytes = _bytes(integrated)
    applied = [
        case.record
        for case in cases
        if not case.record["fallback"] and case.record["ok"]
    ]
    fell_back = [case.record["name"] for case in cases if case.record["fallback"]]
    refused = [case.record["name"] for case in cases if case.record["refused"]]
    failed = [case.record["name"] for case in cases if not case.record["ok"]]

    summary = {
        "cases": len(cases),
        "passed": len(cases) - len(failed),
        "failed": len(failed),
        "applied": len(applied),
        "fell_back": len(fell_back),
        "refused": len(refused),
        "fallback_names": fell_back,
        "refused_names": refused,
        "failed_names": failed,
        "baseline_bytes": before_bytes,
        "integrated_bytes": after_bytes,
        "bytes_removed": before_bytes - after_bytes,
        "bytes_ratio": round(after_bytes / before_bytes, 6) if before_bytes else 0.0,
        "items_before": len(baseline["input"]),
        "items_after": len(integrated),
        "tokens_measured": None,
        "tokens_estimated": {
            "before": before_bytes // 4,
            "after": after_bytes // 4,
            "note": "byte-derived estimate, not a measured token count",
        },
        "latency_ms": {
            "samples": samples,
            "min": min(samples),
            "median": round(statistics.median(samples), 3),
            "max": max(samples),
            "source": "observed",
            "note": "local boundary wall-clock, not model latency",
        },
        "service_usage": {
            "provider_requests": 0,
            "budget_spent_usd": 0.0,
            "note": "no provider is contacted and no credential is read",
        },
    }

    tier_counts: dict[str, int] = {}
    for case in cases:
        tier_counts[case.tier] = tier_counts.get(case.tier, 0) + 1

    document = {
        "schema": SCHEMA,
        "ok": not failed,
        "tiers": dict(sorted(tier_counts.items())),
        "tier_labels": {
            "offline": sorted(set(tier_counts) & {OFFLINE_TIER, BUS_STUB_TIER}),
            "real_host": [],
            "live_provider": [],
        },
        "cases": [case.record for case in cases],
        "summary": summary,
    }
    document["deterministic_digest"] = _digest(document)
    return document


def _digest(document: dict) -> str:
    """Hash only the facts a re-run must reproduce (latency is excluded)."""
    facts = {
        "schema": document["schema"],
        "ok": document["ok"],
        "tiers": document["tiers"],
        "tier_labels": document["tier_labels"],
        "cases": document["cases"],
        "summary": {
            key: value
            for key, value in document["summary"].items()
            if key != "latency_ms"
        },
    }
    return hashlib.sha256(_canonical(facts).encode("utf-8")).hexdigest()


def _print(document: dict) -> None:
    for case in document["cases"]:
        mark = "ok  " if case["ok"] else "FAIL"
        tail = case["code"] or ("fallback" if case["fallback"] else "applied")
        print(
            f"{mark} {case['name']:<34} {case['tier']:<14} "
            f"{case['bytes_before']}->{case['bytes_after']}B  {tail}"
        )
    summary = document["summary"]
    print(
        f"bytes {summary['baseline_bytes']}->{summary['integrated_bytes']} "
        f"({summary['bytes_removed']} removed, ratio {summary['bytes_ratio']}); "
        f"tokens_measured={summary['tokens_measured']}; "
        f"fallbacks={summary['fell_back']} refusals={summary['refused']} "
        f"failures={summary['failed']}; "
        f"latency median {summary['latency_ms']['median']}ms "
        f"(observed, n={len(summary['latency_ms']['samples'])})"
    )
    print(f"digest {document['deterministic_digest']}")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    node = sub.add_parser("run", help="run every declared measurement case")
    node.add_argument("--fixtures", default="", help="fixture directory override")
    node.add_argument(
        "--repetitions", type=int, default=5, help="latency samples to take"
    )
    node.add_argument("--json", default="", help="write the validation document here")
    node.add_argument(
        "--quiet", action="store_true", help="do not print per-case lines"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        document = measure(args.fixtures or None, repetitions=args.repetitions)
    except FileNotFoundError as exc:
        print(f"perf validation: unusable input {exc}", file=sys.stderr)
        return 2
    if args.json:
        Path(args.json).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        _print(document)
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
