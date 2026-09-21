#!/usr/bin/env python3
"""The combined phase-6 validation record for the integrated stack (#8).

Phase 6 shipped three artifacts - the composed end-to-end regression (#24), the
offline performance comparison (#25), and the packaging and release gates (#26).
This module is the roll-up: it *composes those three* on one revision and refuses
to summarize them, so the record a reviewer reads is the record the harnesses
produced.

What it adds over running them separately:

``stages``       every stage of the composed lifecycle has at least one passing
                 assertion, named by id, so a missing stage is a refusal rather
                 than an absent section.
``authority``    the specific assertions that show recalled content and
                 collaboration messages stayed evidence, that a Sentinel veto was
                 not cleared by a later approval, that an ineligible attempt
                 deferred instead of executing, and that stale authorization and
                 cancellation were refused - all by id, from the regression trace.
``tiers``        a census of every assertion by tier, with ``live-provider``
                 reported as ``not-run``: no provider is contacted here, and a
                 check that *claims* the tier refuses the record.
``rollback``     the isolated create/status/rollback round trip from the #26
                 gates, so "a fresh isolated setup reproduces the configuration
                 and rolls back" is evidence in this document too.

``combined_ready`` and ``release_ready`` answer different questions on purpose.
``combined_ready`` is about the composed *evidence*: every stage present, every
authority assertion holding, no overclaimed tier, and no failed gate.
``release_ready`` is the #26 verdict and stays ``false`` while a supported
platform has no current recorded run; its blockers are carried verbatim.

Usage
-----
    python3 jev/scripts/stack_validation.py run
    python3 jev/scripts/stack_validation.py run --json /tmp/stack.json
    python3 jev/scripts/stack_validation.py run --component /path/to/jev-sentinel

Exit codes: 0 the composed evidence holds (``release_ready`` may still be false),
1 a stage or gate failed or the record was refused, 2 the inputs were unusable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_provenance
import e2e_regression
import perf_validation
import release_readiness

SCHEMA = "jev-stack-validation.v1"

TIER_OFFLINE = "offline-fixture"
TIER_ROLLOUT = "rollout-fixture"
TIER_STAGE_STUB = "bus-stage-stub"
TIER_COMPONENT_STUB = "component-stub"
TIER_REAL_COMPONENT = "real-component"
TIER_REAL_HOST = "real-host-binary"
TIER_LIVE = "live-provider"

#: Every stage the composed lifecycle has to demonstrate, mapped to the
#: assertion namespaces the regression trace uses for it (#24). A stage with no
#: passing assertion is a refusal here, so a stage cannot go missing silently.
REQUIRED_STAGES = (
    ("capture", ("capture.",)),
    ("retrieval", ("retrieval.",)),
    ("screening", ("screening.",)),
    ("projection", ("projection.",)),
    ("collab", ("collab.",)),
    ("sentinel_and_veto", ("sentinel.", "veto.")),
    ("approval", ("approval.",)),
    ("execution", ("execution.",)),
)

#: The assertions that carry the authority invariants. ``all_of`` requires every
#: listed id; ``any_of`` requires at least one, for invariants the trace proves
#: from more than one angle. Ids are the composed regression's own, so the mapping
#: is checkable against a trace dump rather than asserted in prose.
AUTHORITY_ASSERTIONS = (
    (
        "recalled_content_is_evidence_not_authorization",
        "Recalled content and collaboration messages stay evidence and never become authorization.",
        (
            "retrieval.note_marks_recalled_content_as_evidence_only",
            "screening.injection_block_is_marked_as_evidence",
            "collab.recalled_collaboration_is_marked_untrusted_evidence",
            "retrieval.remote_enrichment_is_refused_without_consent_and_budget",
        ),
        (),
    ),
    (
        "veto_not_cleared_by_approval",
        "A Sentinel veto latches and a later approval cannot clear it.",
        (
            "veto.an_enforced_finding_vetoes_the_ingress_event_and_latches",
            "veto.an_ordinary_tool_call_inherits_the_session_latch",
            "approval.a_later_allowance_cannot_clear_a_latched_veto",
        ),
        (),
    ),
    (
        "approval_preflight_scope",
        "Approval preflight applies only to an eligible, explicitly approved change.",
        (
            "projection.view_application_requires_explicit_approval",
            "projection.an_unapproved_removal_is_reverted_rather_than_shipped",
            "approval.enforcement_is_off_under_the_default_environment",
            "approval.the_gate_reads_readiness_and_never_flips_a_switch",
        ),
        (),
    ),
    (
        "stale_and_cancelled_refused",
        "Stale, cancelled, and out-of-scope input is refused rather than executed.",
        (
            "projection.a_stale_view_is_refused_rather_than_applied",
            "projection.a_cancelled_turn_removes_nothing",
            "retrieval.hydration_refuses_to_cross_workspaces",
        ),
        (),
    ),
)


class StackError(Exception):
    """Unusable inputs: a missing checkout, harness, or fixture set."""


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose_stages(trace_document: dict, refusals: list[dict]) -> list[dict]:
    """One row per required stage, with the assertions that demonstrate it."""
    checks = trace_document.get("checks") or []
    rows = []
    for name, namespaces in REQUIRED_STAGES:
        matching = [check for check in checks if check["id"].startswith(namespaces)]
        passed = [check for check in matching if check["ok"]]
        rows.append(
            {
                "stage": name,
                "assertions": sorted(check["id"] for check in matching),
                "passed": len(passed),
                "failed": len(matching) - len(passed),
                "tiers": sorted({check["tier"] for check in matching}),
                "status": "pass" if passed and len(passed) == len(matching) else "fail",
            }
        )
    for row in rows:
        if not row["assertions"]:
            refusals.append(
                {
                    "code": "E_STACK_STAGE_MISSING",
                    "detail": f"the composed trace carries no assertion for stage {row['stage']}",
                }
            )
        elif row["status"] != "pass":
            refusals.append(
                {
                    "code": "E_STACK_STAGE_FAILED",
                    "detail": f"stage {row['stage']} has {row['failed']} failing assertion(s)",
                }
            )
    return rows


def compose_authority(trace_document: dict, refusals: list[dict]) -> dict:
    """Resolve the authority invariants against the trace's assertion ids."""
    outcomes = {}
    for ident, requirement, all_of, any_of in AUTHORITY_ASSERTIONS:
        by_id = {check["id"]: check for check in (trace_document.get("checks") or [])}
        present = [name for name in (*all_of, *any_of) if name in by_id]
        for name in all_of:
            if name not in by_id:
                refusals.append(
                    {
                        "code": "E_STACK_AUTHORITY_MISSING",
                        "detail": f"{ident}: the trace carries no assertion {name}",
                    }
                )
        if any_of and not present:
            refusals.append(
                {
                    "code": "E_STACK_AUTHORITY_MISSING",
                    "detail": (
                        f"{ident}: the trace carries none of {', '.join(any_of)}"
                    ),
                }
            )
        failed = [name for name in present if not by_id[name]["ok"]]
        if failed:
            refusals.append(
                {
                    "code": "E_STACK_AUTHORITY_FAILED",
                    "detail": f"{ident}: assertion(s) failed: {', '.join(sorted(failed))}",
                }
            )
        outcomes[ident] = {
            "requirement": requirement,
            "assertions": sorted(present),
            "status": "pass" if present and not failed else "fail",
        }
    return outcomes


def census_tiers(documents: dict, refusals: list[dict]) -> dict:
    """Count every assertion by tier, and refuse a tier that was only claimed."""
    counts: dict[str, int] = {}
    for label, checks in (
        ("e2e", documents["e2e"].get("checks") or []),
        ("perf", documents["perf"].get("cases") or []),
        ("gates", documents["gates"].get("gates") or []),
    ):
        for check in checks:
            tier = check.get("tier") or "unlabelled"
            counts[tier] = counts.get(tier, 0) + 1
    if counts.get(TIER_LIVE):
        refusals.append(
            {
                "code": "E_STACK_TIER_OVERCLAIM",
                "detail": (
                    f"{counts[TIER_LIVE]} assertion(s) claim the {TIER_LIVE} tier; "
                    "this harness contacts no provider and cannot produce one"
                ),
            }
        )
    recorded = release_readiness.load_platform_evidence(
        Path(documents["repo_root"])
    ) or {"platforms": {}}
    real_host_runs = {
        name: record.get("tier")
        for name, record in sorted((recorded.get("platforms") or {}).items())
    }
    if TIER_REAL_HOST not in counts and not real_host_runs:
        refusals.append(
            {
                "code": "E_STACK_REAL_HOST_UNREPRESENTED",
                "detail": (
                    "no assertion ran at the real-host tier and no recorded "
                    "platform run exists, so the record cannot distinguish "
                    "offline from real-host coverage"
                ),
            }
        )
    return {
        "counts": dict(sorted(counts.items())),
        "labels": {
            "offline": [
                TIER_OFFLINE,
                TIER_ROLLOUT,
                TIER_STAGE_STUB,
                TIER_COMPONENT_STUB,
            ],
            "real_component": [TIER_REAL_COMPONENT],
            "real_host": [TIER_REAL_HOST],
            "live_provider": [],
        },
        "recorded_real_host_runs": real_host_runs,
        "live_provider": "not-run",
        "live_provider_note": (
            "no live-provider run exists: paid inference is outside this "
            "authorization and needs explicit consent and a positive budget; "
            "nothing in this record is live model accuracy, safety, or "
            "performance evidence"
        ),
    }


def compose(
    repo_root: Path,
    *,
    component: str = "",
    scratch: Path | None = None,
    repetitions: int = 5,
) -> dict:
    """Run the three phase-6 harnesses on this revision and compose the record."""
    repo_root = Path(repo_root).resolve()
    owned = scratch is None
    scratch = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="jev-stack-"))
    scratch.mkdir(parents=True, exist_ok=True)
    e2e_scratch = scratch / "e2e"
    try:
        try:
            e2e_document, _ = e2e_regression.run(
                scratch=e2e_scratch,
                component=component or None,
                root=repo_root,
                keep=True,
            )
            perf_document = perf_validation.measure(repetitions=repetitions)
            gates_document = release_readiness.evaluate_gates(
                repo_root, scratch=scratch / "gates"
            )
        except FileNotFoundError as exc:
            raise StackError(str(exc)) from exc
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)

    documents = {
        "repo_root": str(repo_root),
        "e2e": e2e_document,
        "perf": perf_document,
        "gates": gates_document,
    }
    refusals: list[dict] = []
    stages = compose_stages(e2e_document, refusals)
    authority = compose_authority(e2e_document, refusals)
    tiers = census_tiers(documents, refusals)

    perf_summary = perf_document["summary"]
    if perf_summary["failed"]:
        refusals.append(
            {
                "code": "E_STACK_PERF_FAILED",
                "detail": f"{perf_summary['failed']} performance case(s) failed",
            }
        )
    if perf_summary["service_usage"]["provider_requests"]:
        refusals.append(
            {
                "code": "E_STACK_PROVIDER_CONTACTED",
                "detail": (
                    "the performance harness reports provider requests; the "
                    "offline composition must contact none"
                ),
            }
        )

    roundtrip = gates_document.get("isolated_roundtrip")
    if not roundtrip or not roundtrip["runs"]:
        refusals.append(
            {
                "code": "E_STACK_ROUNDTRIP_MISSING",
                "detail": "the release gates produced no isolated round-trip record",
            }
        )
    else:
        rolled_back = all(
            run["state"] == "moved"
            and run["source_absent"]
            and run["plan_preserved"]
            and run["binary_matched_plan"]
            and not run["optional_features_enabled"]
            and not run["remote_inference_enabled"]
            for run in roundtrip["runs"]
        )
        if not rolled_back:
            refusals.append(
                {
                    "code": "E_STACK_ROUNDTRIP_FAILED",
                    "detail": "an isolated environment did not reproduce or roll back",
                }
            )

    deterministic = {
        "e2e_digest": e2e_document["digest"],
        "e2e_counts": e2e_document["counts"],
        "perf_digest": perf_document["deterministic_digest"],
        "perf_summary": {
            key: value for key, value in perf_summary.items() if key != "latency_ms"
        },
        "gate_statuses": {
            gate["id"]: [gate["status"], gate["evidence"]]
            for gate in gates_document["gates"]
        },
        "platform_rows": {
            row["platform"]: [row["status"], row["tier"], row["revision"]]
            for row in gates_document["platforms"]
        },
        "release_blockers": gates_document["blocking"],
        "stages": stages,
        "authority": {
            ident: [row["status"], row["assertions"]]
            for ident, row in sorted(authority.items())
        },
        "tiers": tiers,
        "profile_digests": (roundtrip or {}).get("profile_digests", {}),
    }
    combined_ready = (
        not refusals
        and e2e_document["ok"]
        and perf_document["ok"]
        and not gates_document["failed"]
    )
    return {
        "schema": SCHEMA,
        "generated_at_unix_ms": int(time.time() * 1000),
        "revision": build_provenance.git_revision(repo_root),
        "host_pin": gates_document["host_pin"],
        "rust_toolchain": gates_document["rust_toolchain"],
        "supported_profile": gates_document["supported_profile"],
        "safe_profile": gates_document["safe_profile"],
        "component": component or None,
        "component_tier": (TIER_REAL_COMPONENT if component else TIER_COMPONENT_STUB),
        "stages": stages,
        "authority": authority,
        "tiers": tiers,
        "refusals": refusals,
        "combined_ready": combined_ready,
        "release_ready": gates_document["release_ready"],
        "release_blockers": gates_document["blocking"],
        "e2e": {
            "ok": e2e_document["ok"],
            "counts": e2e_document["counts"],
            "digest": e2e_document["digest"],
            "error": e2e_document.get("error"),
        },
        "perf": {
            "ok": perf_document["ok"],
            "digest": perf_document["deterministic_digest"],
            "summary": perf_summary,
        },
        "gates": {
            "release_ready": gates_document["release_ready"],
            "failed": gates_document["failed"],
            "not_run": gates_document["not_run"],
            "gates": gates_document["gates"],
            "platforms": gates_document["platforms"],
        },
        "isolated_roundtrip": (roundtrip or {}).get("runs", []),
        "digest": _sha256(_canonical(deterministic)),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    node = sub.add_parser("run", help="compose the phase-6 validation record")
    node.add_argument("--root", default="", help="repository root override")
    node.add_argument(
        "--component",
        default="",
        help="pinned jev-sentinel checkout, to run the real-component tier",
    )
    node.add_argument("--scratch", default="", help="working directory to use")
    node.add_argument(
        "--repetitions", type=int, default=5, help="performance latency samples"
    )
    node.add_argument("--json", default="", help="write the combined record here")
    node.add_argument("--quiet", action="store_true", help="print only the verdict")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = (
        Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    )
    try:
        document = compose(
            root,
            component=args.component,
            scratch=Path(args.scratch) if args.scratch else None,
            repetitions=args.repetitions,
        )
    except StackError as exc:
        print(f"stack validation: unusable input {exc}", file=sys.stderr)
        return 2
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        for row in document["stages"]:
            print(
                f"{'ok  ' if row['status'] == 'pass' else 'FAIL'} "
                f"stage {row['stage']:<16} {row['passed']}/{len(row['assertions'])} "
                f"assertions tiers={','.join(row['tiers']) or '-'}"
            )
        for ident, row in sorted(document["authority"].items()):
            print(
                f"{'ok  ' if row['status'] == 'pass' else 'FAIL'} "
                f"authority {ident} ({len(row['assertions'])} assertion(s))"
            )
        perf = document["perf"]["summary"]
        print(
            f"perf bytes {perf['baseline_bytes']}->{perf['integrated_bytes']} "
            f"ratio {perf['bytes_ratio']} fallbacks {perf['fell_back']} "
            f"refusals {perf['refused']} failures {perf['failed']}"
        )
        print(
            f"tiers {_canonical(document['tiers']['counts'])} "
            f"live_provider={document['tiers']['live_provider']}"
        )
    for refusal in document["refusals"]:
        print(f"REFUSED {refusal['code']}: {refusal['detail']}", file=sys.stderr)
    print(
        f"combined_ready={str(document['combined_ready']).lower()} "
        f"release_ready={str(document['release_ready']).lower()} "
        f"revision={document['revision'][:10]} "
        f"stages={len(document['stages'])} digest={document['digest'][:16]}"
        + (
            f" release_blockers={','.join(document['release_blockers'])}"
            if document["release_blockers"]
            else ""
        )
    )
    return 0 if document["combined_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
