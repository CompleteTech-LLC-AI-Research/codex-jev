#!/usr/bin/env python3
"""Composed phase-6 release validation (#8).

Phase 6/6 asks for one proof that the integrated lifecycle is *validated and
released*: an offline correlated trace that exercises every stage with the
authority boundaries preserved, reproducible evidence that keeps offline,
real-host, and live-provider coverage distinct, and a fresh isolated setup that
reproduces the validated configuration and rolls back - with release readiness
following that evidence rather than issue completion.

This module composes the phase-6 harnesses instead of re-implementing them:

* ``e2e_regression`` (6.1) supplies the composed, correlated offline trace;
* ``perf_validation`` (6.2) supplies the baseline-vs-integrated measurement and
  the service-usage counters;
* ``release_readiness`` (6.3) supplies the release gates, the isolated
  create/rollback round trip, and the candidate digest.

What it adds is the composition and its honesty rules:

* ``trace``     - every lifecycle stage in the declared order with its
  assertions, plus the authority invariants that must hold across the trace;
* ``tiers``     - offline / real-host / live-provider coverage computed from
  evidence, where a live-provider claim is refused and never implied;
* ``readiness`` - the gates, the reproduced-and-rolled-back round trip, and the
  property that ``release_ready`` mirrors the gate evidence.

``phase_validated`` is true only when the trace is ok, the tiers do not
overclaim, the isolated setup reproduces and rolls back, and readiness tracks
evidence. It is deliberately independent of ``release_ready``: this host can
validate the lifecycle while release readiness stays false because two supported
platforms have no run. Nothing here is live-model evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_regression  # noqa: E402
import perf_validation  # noqa: E402
import release_readiness  # noqa: E402

SCHEMA = "jev-phase6-release.v1"

#: The composed lifecycle, in the order ``ARCHITECTURE.md`` promises.
STAGE_ORDER = (
    "capture",
    "retrieval",
    "screening",
    "projection",
    "collab",
    "sentinel_and_veto",
    "approval",
    "execution",
)

#: Check-id namespaces that belong to each stage. ``negatives`` (fixture
#: labelling) is cross-cutting and reported separately.
STAGE_PREFIXES = {
    "capture": ("capture",),
    "retrieval": ("retrieval",),
    "screening": ("screening",),
    "projection": ("projection",),
    "collab": ("collab",),
    "sentinel_and_veto": ("sentinel", "veto"),
    "approval": ("approval",),
    "execution": ("execution",),
}
CROSS_CUTTING_PREFIX = "negatives"

#: Invariants that must hold across the composed trace, named by the check that
#: proves each one. These are the authority boundaries the phase promises.
AUTHORITY_INVARIANTS = {
    "stages_run_in_the_declared_order": (
        "execution.the_composed_lifecycle_runs_in_the_declared_order"
    ),
    "the_canonical_request_is_never_mutated": (
        "projection.the_canonical_request_is_never_mutated"
    ),
    "unapproved_removal_is_reverted": (
        "projection.an_unapproved_removal_is_reverted_rather_than_shipped"
    ),
    "quarantine_is_journaled_before_it_is_honored": (
        "screening.quarantine_is_journaled_before_it_is_honored"
    ),
    "veto_clear_requires_explicit_confirmation": (
        "veto.clearing_a_latch_requires_explicit_confirmation"
    ),
    "a_later_allowance_cannot_clear_a_latched_veto": (
        "approval.a_later_allowance_cannot_clear_a_latched_veto"
    ),
    "approval_never_flips_a_switch": (
        "approval.the_gate_reads_readiness_and_never_flips_a_switch"
    ),
    "no_assertion_claims_a_host_or_provider_run": (
        "execution.no_assertion_claims_a_host_or_provider_run"
    ),
    "no_check_claims_a_measured_token_count": (
        "execution.no_check_claims_a_measured_token_count"
    ),
}


def review_trace(document: dict) -> dict:
    """Group the correlated trace by stage and check the authority invariants."""
    by_id = {check["id"]: check for check in document["checks"]}
    stages = []
    assigned: set[str] = set()
    for stage in STAGE_ORDER:
        prefixes = STAGE_PREFIXES[stage]
        ids = sorted(
            check_id for check_id in by_id if check_id.split(".", 1)[0] in prefixes
        )
        assigned.update(ids)
        stages.append(
            {
                "stage": stage,
                "tier": next(
                    (s["tier"] for s in document["scenarios"] if s["name"] == stage),
                    None,
                ),
                "assertions": len(ids),
                "ok": bool(ids) and all(by_id[check_id]["ok"] for check_id in ids),
                "failed": [c for c in ids if not by_id[c]["ok"]],
            }
        )
    cross_cutting = sorted(
        check_id
        for check_id in by_id
        if check_id.split(".", 1)[0] == CROSS_CUTTING_PREFIX
    )
    assigned.update(cross_cutting)
    unassigned = sorted(set(by_id) - assigned)
    authority = []
    for invariant, check_id in AUTHORITY_INVARIANTS.items():
        check = by_id.get(check_id)
        authority.append(
            {
                "invariant": invariant,
                "check": check_id,
                "present": check is not None,
                "ok": bool(check and check["ok"]),
                "tier": check.get("tier") if check else None,
            }
        )
    order = [scenario["name"] for scenario in document["scenarios"]]
    ok = (
        bool(document["ok"])
        and document["counts"]["failed"] == 0
        and order == list(STAGE_ORDER)
        and all(stage["ok"] for stage in stages)
        and all(entry["ok"] for entry in authority)
        and not unassigned
    )
    return {
        "ok": ok,
        "digest": document.get("digest"),
        "counts": document["counts"],
        "order": order,
        "stages": stages,
        "cross_cutting": {
            "prefix": CROSS_CUTTING_PREFIX,
            "assertions": len(cross_cutting),
            "ok": all(by_id[c]["ok"] for c in cross_cutting),
        },
        "authority": authority,
        "unassigned": unassigned,
    }


def review_tiers(e2e_document: dict, perf_document: dict, gates: dict) -> dict:
    """Derive tier coverage from evidence and name every overclaim."""
    overclaims: list[str] = []
    for source, document in (
        ("e2e trace", e2e_document),
        ("performance run", perf_document),
    ):
        labels = document["tier_labels"]
        if labels.get("real_host"):
            overclaims.append(
                f"the {source} labels a real-host tier: {labels['real_host']}"
            )
        if labels.get("live_provider"):
            overclaims.append(
                f"the {source} labels a live-provider tier: {labels['live_provider']}"
            )
    usage = perf_document["summary"]["service_usage"]
    if usage.get("provider_requests") != 0:
        overclaims.append(
            f"the performance run reported {usage.get('provider_requests')} provider "
            "request(s) instead of 0"
        )
    if usage.get("budget_spent_usd") not in (0, 0.0):
        overclaims.append(
            f"the performance run spent {usage.get('budget_spent_usd')} USD"
        )
    if e2e_document["token_measurement"]["measured"] is not None:
        overclaims.append("the offline trace claims a measured token count")
    verified = sorted(
        row["platform"] for row in gates["platforms"] if row["status"] == "verified"
    )
    live_claims = sorted(
        row["platform"]
        for row in gates["platforms"]
        if row["status"] == "fail" and "live-provider" in (row.get("reason") or "")
    )
    if live_claims:
        overclaims.append(
            f"a platform record claims a live-provider tier: {live_claims}"
        )
    return {
        "ok": not overclaims,
        "coverage": {
            "offline": {
                "tiers": sorted(e2e_document["tiers"]),
                "measured_tiers": sorted(perf_document["tiers"]),
            },
            "real_host_binary": {"platforms": verified},
            "live_provider": {"status": "not-run", "sources": []},
        },
        "provider_requests": usage.get("provider_requests"),
        "budget_spent_usd": usage.get("budget_spent_usd"),
        "overclaims": overclaims,
    }


def review_readiness(gates: dict) -> dict:
    """Check the rollback round trip and that readiness follows the evidence."""
    expected = not gates["failed"] and not gates["not_run"]
    roundtrip = gates.get("isolated_roundtrip")
    runs = (roundtrip or {}).get("runs", [])
    reproduced = (
        bool(roundtrip)
        and len(runs) == 2
        and all(
            run["state"] == "moved"
            and run["source_absent"]
            and run["plan_preserved"]
            and run["binary_matched_plan"]
            and not run["optional_features_enabled"]
            and not run["remote_inference_enabled"]
            for run in runs
        )
    )
    typed = all(
        gate["status"] in release_readiness.GATE_STATUSES
        and gate["evidence"] in release_readiness.GATE_EVIDENCE
        and gate["requirement"]
        and gate["detail"]
        for gate in gates["gates"]
    )
    passes_are_evidence = all(
        gate["evidence"] in ("verified-here", "recorded")
        for gate in gates["gates"]
        if gate["status"] == "pass"
    )
    return {
        "release_ready": gates["release_ready"],
        "tracks_evidence": gates["release_ready"] is expected,
        "reproduced_and_rolled_back": reproduced,
        "gates_typed": typed,
        "passes_are_evidence": passes_are_evidence,
        "gates": [
            {"id": gate["id"], "status": gate["status"], "evidence": gate["evidence"]}
            for gate in gates["gates"]
        ],
        "failed": list(gates["failed"]),
        "not_run": list(gates["not_run"]),
        "blocking": sorted(set(gates["blocking"])),
        "candidate_bundle_digest": gates.get("candidate_bundle_digest"),
    }


def compose(root: Path, scratch: Path | None = None, repetitions: int = 5) -> dict:
    """Compose the three phase-6 harnesses into one validation document."""
    root = Path(root).resolve()
    owned = scratch is None
    scratch = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="jev-phase6-"))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        e2e_document, _ = e2e_regression.run(scratch=scratch / "e2e", root=root)
        perf_document = perf_validation.measure(repetitions=repetitions)
        gates = release_readiness.evaluate_gates(root, scratch=scratch / "gates")
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)
    trace = review_trace(e2e_document)
    tiers = review_tiers(e2e_document, perf_document, gates)
    readiness = review_readiness(gates)
    criteria = {
        "offline_correlated_trace": trace["ok"],
        "tier_evidence_distinct": tiers["ok"],
        "reproducible_and_rollback": readiness["reproduced_and_rolled_back"],
        "readiness_tracks_evidence": readiness["tracks_evidence"]
        and readiness["gates_typed"]
        and readiness["passes_are_evidence"],
    }
    return {
        "schema": SCHEMA,
        "revision": gates["revision"],
        "evaluated_at_unix_ms": int(time.time() * 1000),
        "phase_validated": all(criteria.values()),
        "criteria": criteria,
        "trace": trace,
        "tiers": tiers,
        "readiness": readiness,
        "release_ready": readiness["release_ready"],
        "blocking": readiness["blocking"],
        "limits": [
            "offline fixtures and stubs only; no live-provider tier was run",
            "the recorded real-host runs use a debug binary, not a release artifact",
            "release_ready is false while a supported platform has no recorded run",
        ],
    }


def print_document(document: dict) -> None:
    criteria = document["criteria"]
    print(
        "phase_validated="
        f"{str(document['phase_validated']).lower()} "
        f"trace={str(criteria['offline_correlated_trace']).lower()} "
        f"tiers={str(criteria['tier_evidence_distinct']).lower()} "
        f"rollback={str(criteria['reproducible_and_rollback']).lower()} "
        f"readiness={str(criteria['readiness_tracks_evidence']).lower()} "
        f"release_ready={str(document['release_ready']).lower()} "
        f"revision={document['revision']} "
        f"blocking={','.join(document['blocking'])}"
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    node = sub.add_parser("run", help="compose the phase-6 release validation")
    node.add_argument("--root", default="", help="repository root override")
    node.add_argument("--scratch", default="", help="working directory to use")
    node.add_argument(
        "--repetitions", type=int, default=5, help="boundary latency samples"
    )
    node.add_argument("--json", default="", help="write the document here")
    node.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = (
        Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    )
    document = compose(
        root,
        Path(args.scratch) if args.scratch else None,
        repetitions=args.repetitions,
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        print_document(document)
    return 0 if document["phase_validated"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
