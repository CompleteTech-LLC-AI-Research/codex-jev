#!/usr/bin/env python3
"""Shadow comparison and the gate that keeps approval enforcement off.

Phase #5 (#23) asks three things that are easy to fake and therefore have to be
built so that faking them fails a check:

``correlate``
    join a typed judgment from the approval component to the host's own final
    decision and to timing, **without** carrying a raw command or a secret into
    the record. A record holds a category and a command *digest*; the audit
    below proves no raw text survives into the report.

``report``
    summarize a run so that every deferral and every failure is present with
    its reason. A report that counts only the cases it understood is not
    evidence, so the counts are derived from the same lists the report prints.

``gate``
    decide whether enforcement may be enabled. It may not, unless the operator
    switch, the declared remote consent, the model-availability check, the
    frozen evaluation set, and the declared criteria all hold. Every missing or
    unmeasured input leaves enforcement **disabled**, never "unknown".

Nothing here measures model quality, latency, or safety. The only timings the
report carries are wall-clock spans of the fixture's own steps, labelled as
such, and the report names what it did not measure in ``unmeasured``.

Exit codes: 0 = ok / enforcement disabled as declared, 10 = enforcement would be
enabled, 1 = a refusal or an unproven input, 2 = usage error.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPORT_SCHEMA = "jev.shadow-report.v1"
SET_SCHEMA = "jev.shadow-set.v1"
CRITERIA_SCHEMA = "jev.shadow-criteria.v1"
FREEZE_SCHEMA = "jev.shadow-freeze.v1"

# The typed decision the component under test returns, and the host's own
# outcome. They are separate vocabularies on purpose: the whole point of the
# comparison is that the two are produced independently.
JUDGMENT_DECISIONS = ("allow", "deny", "abstain")
HOST_OUTCOMES = ("approved", "denied", "abstained")

# One judgment decision maps to exactly one host outcome, and the mapping is
# declared here rather than inferred, so a vocabulary drift is a failing test
# instead of a silent agreement.
DECISION_TO_OUTCOME = {"allow": "approved", "deny": "denied", "abstain": "abstained"}

# The action categories this phase may ever consider. Enforcement is opt-in per
# category; the default set is empty, so "no categories opted in" and "no
# enforcement" are the same state.
ACTION_CATEGORIES = ("shell_command", "file_write", "network_egress", "destructive")

# Why a case was deferred to the existing reviewer, or failed, rather than
# producing a comparable decision. Every reason is declared: a reason that is
# not in this set is a bug in the producer, and ``correlate`` refuses it.
DEFERRAL_REASONS = (
    "ineligible_category",
    "model_unavailable",
    "remote_consent_missing",
    "deadline_exceeded",
    "unbound_answer",
)
FAILURE_REASONS = (
    "component_error",
    "malformed_answer",
    "adapter_timeout",
)

# Fields a record may carry. Anything else is refused, so a producer cannot
# quietly add a raw command or a token to a record that is meant to be safe to
# attach to an issue.
JUDGMENT_FIELDS = {
    "case_id",
    "category",
    "decision",
    "confidence",
    "component_ms",
    "deferral",
    "failure",
}
HOST_FIELDS = {"case_id", "outcome", "host_ms", "command_sha256", "session_ref"}

# Fields that must never appear with raw content. The audit below uses these
# names to show what it scanned, not to trust them.
REDACTED_FIELDS = ("command_sha256", "session_ref", "case_id")

# What no run in this module measures. The report carries this list verbatim so
# a reader cannot mistake the fixture's own timings for a provider measurement.
UNMEASURED = (
    "provider_latency",
    "model_accuracy",
    "safety_in_the_wild",
    "cost",
)

GUARDIAN_ONLY_SWITCH = "JEV_SWITCH_APPROVAL_GUARDIAN_ONLY"
ENFORCEMENT_SWITCH = "JEV_SWITCH_APPROVAL_ENFORCEMENT"
GATE_SWITCH = "JEV_SWITCH_APPROVAL_ENFORCEMENT_GATE"
CONSENT_ENV = "JEV_REMOTE_INFERENCE_CONSENT"
MODEL_AVAILABLE_ENV = "JEV_APPROVAL_MODEL_AVAILABLE"


class ShadowError(Exception):
    """A shadow input is unusable, and the module must not guess."""


def hash_command(command):
    """Return the only form of a command that may leave this process."""
    if not isinstance(command, str) or not command.strip():
        raise ShadowError("E_EMPTY_COMMAND: a command must be a non-empty string")
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _require_fields(record, allowed, what):
    if not isinstance(record, dict):
        raise ShadowError(f"E_{what.upper()}_SCHEMA: {what} must be an object")
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise ShadowError(
            f"E_{what.upper()}_UNSUPPORTED_FIELD: {what} carries unsupported "
            f"fields: {', '.join(unknown)}"
        )
    missing = sorted(allowed - set(record) - {"deferral", "failure"})
    if missing:
        raise ShadowError(
            f"E_{what.upper()}_SCHEMA: {what} is missing fields: {', '.join(missing)}"
        )
    return record


def judgment(
    case_id,
    category,
    decision,
    confidence,
    component_ms,
    *,
    deferral=None,
    failure=None,
):
    """Build a typed judgment record.

    ``deferral`` and ``failure`` are mutually exclusive: a case either produced
    a comparable decision, or it deferred to the existing reviewer, or it
    failed. Collapsing the last two would hide exactly the cases #23 asks the
    report to keep.
    """
    if category not in ACTION_CATEGORIES:
        raise ShadowError(
            f"E_UNKNOWN_CATEGORY: {category!r} is not a declared category"
        )
    if decision not in JUDGMENT_DECISIONS:
        raise ShadowError(
            f"E_UNKNOWN_DECISION: {decision!r} is not a judgment decision"
        )
    if deferral and failure:
        raise ShadowError("E_AMBIGUOUS_CASE: a case cannot both defer and fail")
    if deferral and deferral not in DEFERRAL_REASONS:
        raise ShadowError(f"E_UNKNOWN_DEFERRAL: {deferral!r} is not a declared reason")
    if failure and failure not in FAILURE_REASONS:
        raise ShadowError(f"E_UNKNOWN_FAILURE: {failure!r} is not a declared reason")
    if (deferral or failure) and decision != "abstain":
        # A case that produced no answer cannot also carry a comparable
        # decision: that is how a deferral gets counted as an agreement.
        raise ShadowError(
            "E_DEFERRAL_DECISION: a deferred or failed case must abstain, "
            f"not {decision!r}"
        )
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise ShadowError("E_CONFIDENCE: confidence must be a number in [0, 1]")
    if not isinstance(component_ms, (int, float)) or component_ms < 0:
        raise ShadowError("E_TIMING: component_ms must be a non-negative number")
    record = {
        "case_id": case_id,
        "category": category,
        "decision": decision,
        "confidence": float(confidence),
        "component_ms": float(component_ms),
    }
    if deferral:
        record["deferral"] = deferral
    if failure:
        record["failure"] = failure
    return _require_fields(record, JUDGMENT_FIELDS, "judgment")


def host_decision(case_id, outcome, host_ms, command_sha256, session_ref):
    """Build the host's own record of what it finally did."""
    if outcome not in HOST_OUTCOMES:
        raise ShadowError(f"E_UNKNOWN_OUTCOME: {outcome!r} is not a host outcome")
    if not isinstance(host_ms, (int, float)) or host_ms < 0:
        raise ShadowError("E_TIMING: host_ms must be a non-negative number")
    if not isinstance(command_sha256, str) or len(command_sha256) != 64:
        raise ShadowError(
            "E_COMMAND_DIGEST: command_sha256 must be a sha256 hex digest"
        )
    record = {
        "case_id": case_id,
        "outcome": outcome,
        "host_ms": float(host_ms),
        "command_sha256": command_sha256,
        "session_ref": session_ref,
    }
    return _require_fields(record, HOST_FIELDS, "host")


def correlate(typed_judgment, host_record):
    """Join one judgment to one host decision.

    Returns a case with a ``kind`` of ``agreement``, ``disagreement``,
    ``deferral``, or ``failure``. A deferral or a failure is never counted as an
    agreement, and a disagreement names the two decisions it saw.
    """
    _require_fields(typed_judgment, JUDGMENT_FIELDS, "judgment")
    _require_fields(host_record, HOST_FIELDS, "host")
    if typed_judgment["case_id"] != host_record["case_id"]:
        raise ShadowError(
            "E_CASE_MISMATCH: judgment and host record disagree on case_id"
        )
    case = {
        "case_id": typed_judgment["case_id"],
        "category": typed_judgment["category"],
        "judgment": typed_judgment["decision"],
        "host": host_record["outcome"],
        "confidence": typed_judgment["confidence"],
        "component_ms": typed_judgment["component_ms"],
        "host_ms": host_record["host_ms"],
        "command_sha256": host_record["command_sha256"],
        "session_ref": host_record["session_ref"],
    }
    if "failure" in typed_judgment:
        case["kind"] = "failure"
        case["reason"] = typed_judgment["failure"]
        case["detail"] = "the component did not produce a comparable decision"
        return case
    if "deferral" in typed_judgment:
        case["kind"] = "deferral"
        case["reason"] = typed_judgment["deferral"]
        case["detail"] = "the case was left to the existing reviewer"
        return case
    expected = DECISION_TO_OUTCOME[typed_judgment["decision"]]
    if expected == host_record["outcome"]:
        case["kind"] = "agreement"
        case["reason"] = None
        return case
    case["kind"] = "disagreement"
    case["reason"] = "judgment_outcome_conflict"
    case["detail"] = (
        f"judgment said {typed_judgment['decision']}, host did {host_record['outcome']}"
    )
    return case


def _summary(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "count": 0,
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "p95_ms": None,
        }
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "p95_ms": ordered[index],
    }


def shadow_report(
    cases, *, tier="offline-fixture", opted_in_categories=(), documents=()
):
    """Summarize correlated cases, keeping every deferral and failure visible.

    The counts are derived from the lists the report prints, so a case cannot be
    counted without also being shown.
    """
    unknown = sorted(set(opted_in_categories) - set(ACTION_CATEGORIES))
    if unknown:
        raise ShadowError(
            f"E_UNKNOWN_CATEGORY: opted-in categories are unknown: {unknown}"
        )
    deferrals = [case for case in cases if case.get("kind") == "deferral"]
    failures = [case for case in cases if case.get("kind") == "failure"]
    disagreements = [case for case in cases if case.get("kind") == "disagreement"]
    agreements = [case for case in cases if case.get("kind") == "agreement"]
    countable = len(agreements) + len(disagreements)
    report = {
        "schema": REPORT_SCHEMA,
        "tier": tier,
        "topline": {
            "cases": len(cases),
            "agreements": len(agreements),
            "disagreements": len(disagreements),
            "deferrals": len(deferrals),
            "failures": len(failures),
            "comparable": countable,
        },
        # The correlated cases travel with the report, so the counts above can
        # be re-derived instead of trusted.
        "cases": cases,
        "opted_in_categories": sorted(opted_in_categories),
        "deferrals": deferrals,
        "failures": failures,
        "disagreements": disagreements,
        "deferrals_by_reason": _tally(deferrals),
        "failures_by_reason": _tally(failures),
        "timings": {
            "unit": "ms",
            "measured": "wall clock inside this fixture, not a provider measurement",
            "component": _summary([case["component_ms"] for case in cases]),
            "host": _summary([case["host_ms"] for case in cases]),
        },
        "unmeasured": list(UNMEASURED),
        "redaction": redaction_audit(cases, documents=documents),
    }
    return report


def _tally(cases):
    tally = {}
    for case in cases:
        reason = case.get("reason") or "unspecified"
        tally[reason] = tally.get(reason, 0) + 1
    return tally


def metrics(report):
    """Derive the numbers a criterion may refer to, or ``None`` if unmeasurable."""
    topline = report["topline"]
    cases = topline["cases"]
    if not cases:
        return {
            "cases": 0,
            "agreement_rate": None,
            "deferral_rate": None,
            "failure_rate": None,
            "disagreement_rate": None,
            "label_mismatch_rate": None,
        }
    comparable = topline["comparable"]
    mismatches = len(report.get("label_mismatches", []))
    return {
        "cases": cases,
        "agreement_rate": round(topline["agreements"] / cases, 4),
        "deferral_rate": round(topline["deferrals"] / cases, 4),
        "failure_rate": round(topline["failures"] / cases, 4),
        "disagreement_rate": round(topline["disagreements"] / cases, 4),
        # Only cases the component answered can be labelled wrong; a deferral or
        # a failure is reported separately so "did not decide" is never dressed
        # up as "decided correctly".
        "label_mismatch_rate": round(mismatches / comparable, 4)
        if comparable
        else None,
    }


def evaluate_criteria(criteria, observed):
    """Fail closed: a criterion with no measurement is not met.

    ``criteria`` is a list of ``{"name", "metric", "op", "value"}``. The result
    names every unmet criterion with what was required and what was seen, so a
    near miss is visible rather than rounded into a pass.
    """
    unmet = []
    checked = []
    for criterion in criteria:
        name = criterion.get("name")
        metric = criterion.get("metric")
        op = criterion.get("op")
        target = criterion.get("value")
        if op not in (">=", "<=", "=="):
            unmet.append({"name": name, "reason": "unsupported_operator", "op": op})
            continue
        seen = observed.get(metric)
        if seen is None:
            unmet.append({"name": name, "reason": "unmeasured", "metric": metric})
            continue
        passed = {">=": seen >= target, "<=": seen <= target, "==": seen == target}[op]
        entry = {
            "name": name,
            "metric": metric,
            "op": op,
            "required": target,
            "observed": seen,
        }
        checked.append(entry)
        if not passed:
            unmet.append({**entry, "reason": "requirement_not_met"})
    return {"met": not unmet, "checked": checked, "unmet": unmet}


def redaction_audit(cases, raw_commands=(), documents=()):
    """Prove no raw command text and no secret reached the correlated cases.

    ``raw_commands`` lets a caller hand over the exact text it fed in; the audit
    then shows that none of it survives. ``documents`` are the set, criteria and
    report documents that will be attached to an issue, and they are scanned the
    same way, because a safe case list next to a leaky fixture is not safe. Any
    field not declared above is reported, so a new field cannot slip in
    unscanned.
    """
    scanned = [json.dumps(cases, sort_keys=True)]
    scanned += [json.dumps(document, sort_keys=True) for document in documents]
    leaked = []
    for command in raw_commands:
        if not command:
            continue
        for index, text in enumerate(scanned):
            if command in text:
                leaked.append(
                    {"document": index, "command_sha256": hash_command(command)}
                )
    fields = set()
    for case in cases:
        fields.update(case)
    return {
        "schema": "jev.shadow-redaction.v1",
        "allowed_fields": sorted(fields),
        "checked_fields": sorted(set(REDACTED_FIELDS) & fields),
        "documents_scanned": len(scanned),
        "raw_commands_checked": len([command for command in raw_commands if command]),
        "raw_commands_present": len(leaked),
        "leaks": leaked,
        "clean": not leaked,
    }


# A report may not carry a claim this phase did not measure. The scan is over
# keys, so a rendered sentence has to be reviewed by a human, but a
# machine-readable "latency_saved" or "safety_improvement" field cannot appear.
FORBIDDEN_CLAIM_KEYS = (
    "latency_saved",
    "latency_reduction",
    "speedup",
    "tokens_saved",
    "cost_saved",
    "safety_improvement",
    "accuracy_improvement",
)


def _claim_keys(value, path="$"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_CLAIM_KEYS:
                found.append(f"{path}.{key}")
            found += _claim_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _claim_keys(item, f"{path}[{index}]")
    return found


def verify_report(report):
    """Re-check a report against itself. Returns every problem found, or [].

    The point is that a report cannot be internally inconsistent and still pass:
    a case counted in ``topline`` but absent from the list it is counted from is
    a failure here, not a rounding difference.
    """
    problems = []
    topline = report.get("topline", {})
    cases = report.get("cases")
    if not isinstance(cases, list):
        problems.append("topline.cases is not backed by a list of correlated cases")
        cases = []
    if len(cases) != topline.get("cases"):
        problems.append(
            f"topline.cases={topline.get('cases')} but {len(cases)} cases are listed"
        )
    case_ids = [case.get("case_id") for case in cases]
    seen = {}
    for kind in ("disagreements", "deferrals", "failures"):
        listed = report.get(kind)
        if not isinstance(listed, list):
            problems.append(f"{kind} is not a list")
            continue
        if len(listed) != topline.get(kind):
            problems.append(
                f"topline.{kind}={topline.get(kind)} but {len(listed)} are listed"
            )
        for case in listed:
            case_id = case.get("case_id")
            if case_id not in case_ids:
                problems.append(
                    f"{kind[:-1]} {case_id} is not among the correlated cases"
                )
            if case_id in seen:
                problems.append(
                    f"{case_id} is listed as both {seen[case_id]} and {kind[:-1]}"
                )
            seen[case_id] = kind[:-1]
    total = topline.get("agreements", 0) + topline.get("disagreements", 0)
    total += topline.get("deferrals", 0) + topline.get("failures", 0)
    if total != topline.get("cases"):
        problems.append(
            f"topline.cases={topline.get('cases')} but its kinds sum to {total}"
        )
    marked = render_markdown(report)
    for kind in ("failures", "deferrals"):
        for case in report.get(kind, []):
            if f"`{case['case_id']}`" not in marked:
                problems.append(
                    f"{kind[:-1]} {case['case_id']} is missing from the rendered report"
                )
    if not report.get("unmeasured"):
        problems.append(
            "the report claims no unmeasured quantity, which cannot be true"
        )
    for claim in _claim_keys(report):
        problems.append(f"the report claims an unmeasured quantity at {claim}")
    redaction = report.get("redaction", {})
    if not redaction.get("clean"):
        problems.append("the redaction audit is not clean")
    if not redaction.get("documents_scanned"):
        problems.append("the redaction audit scanned no document")
    return problems


def load_set(path):
    """Load one labeled split and check its own shape."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return _check_set(document, path)


def _check_set(document, origin):
    if document.get("schema") != SET_SCHEMA:
        raise ShadowError(f"E_SET_SCHEMA: {origin} is not a {SET_SCHEMA} document")
    if not document.get("labeling_procedure"):
        raise ShadowError(f"E_SET_UNLABELED: {origin} names no labeling procedure")
    rows = document.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ShadowError(f"E_SET_EMPTY: {origin} carries no cases")
    seen = set()
    for row in rows:
        case_id = row.get("case_id")
        if not case_id or case_id in seen:
            raise ShadowError(
                f"E_SET_CASE_ID: {origin} has a missing or duplicate case_id"
            )
        seen.add(case_id)
        if row.get("label_decision") not in JUDGMENT_DECISIONS:
            raise ShadowError(
                f"E_SET_LABEL: {origin} case {case_id} has no labeled decision"
            )
        if row.get("category") not in ACTION_CATEGORIES:
            raise ShadowError(
                f"E_SET_CATEGORY: {origin} case {case_id} has no known category"
            )
    return document


def freeze(paths, out_path):
    """Pin the digests of the splits, so "frozen" is checkable, not claimed."""
    digests = {}
    for name, path in paths.items():
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        digests[name] = {"path": str(path), "sha256": digest}
    document = {"schema": FREEZE_SCHEMA, "splits": digests}
    Path(out_path).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return document


def verify_freeze(frozen_path, paths):
    """Return the splits whose bytes no longer match the pin."""
    frozen = json.loads(Path(frozen_path).read_text(encoding="utf-8"))
    if frozen.get("schema") != FREEZE_SCHEMA:
        raise ShadowError(
            f"E_FREEZE_SCHEMA: {frozen_path} is not a {FREEZE_SCHEMA} document"
        )
    drift = []
    for name, path in paths.items():
        expected = frozen.get("splits", {}).get(name, {}).get("sha256")
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if expected != actual:
            drift.append({"split": name, "expected": expected, "actual": actual})
    return drift


def enforcement_state(
    env, *, criteria, observed, set_digest_ok, opted_in_categories=()
):
    """Decide whether approval enforcement may be enabled. Fail closed.

    Enforcement requires all of: the operator switch on, the guardian-only kill
    switch off, the gate itself declared by the active profile, declared remote
    consent, a passing model-availability check, a frozen evaluation set, at
    least one opted-in action category, and every declared criterion met. Each
    check is reported with its own verdict, so "disabled" always says why.
    """
    unknown = sorted(set(opted_in_categories) - set(ACTION_CATEGORIES))
    if unknown:
        # An unrecognized category is not "opted in" to anything, and treating
        # it as a bare non-empty list would let a typo read as consent.
        raise ShadowError(
            f"E_UNKNOWN_CATEGORY: opted-in categories are unknown: {unknown}"
        )
    checks = {
        "gate_declared": env.get(GATE_SWITCH) == "1",
        "switch_on": env.get(ENFORCEMENT_SWITCH) == "1",
        "guardian_only_kill_switch_off": env.get(GUARDIAN_ONLY_SWITCH) != "1",
        "remote_consent_declared": env.get(CONSENT_ENV) == "1",
        "model_available": env.get(MODEL_AVAILABLE_ENV) == "1",
        "evaluation_set_frozen": bool(set_digest_ok),
        "categories_opted_in": bool(opted_in_categories),
    }
    criteria_result = evaluate_criteria(criteria, observed)
    checks["criteria_met"] = criteria_result["met"]
    blocked_by = sorted(name for name, ok in checks.items() if not ok)
    return {
        "enabled": not blocked_by,
        "guardian_only": bool(blocked_by),
        "checks": checks,
        "blocked_by": blocked_by,
        "criteria": criteria_result,
        "unmeasured": list(UNMEASURED),
        "fallback": (
            "the existing synchronous reviewer (Guardian) answers the attempt "
            "within the original overall deadline"
        ),
        "exit_code": 10 if not blocked_by else 0,
        "note": (
            "Enforcement is an opt-in consequence of a measured evaluation, never "
            "a configuration default; any unmet input leaves it disabled, and "
            "disabling it needs no source change."
        ),
    }


def _criteria_document(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema") != CRITERIA_SCHEMA:
        raise ShadowError(
            f"E_CRITERIA_SCHEMA: {path} is not a {CRITERIA_SCHEMA} document"
        )
    return document


def _load_criteria(path):
    return _criteria_document(path).get("criteria", [])


def run_split(document, *, tier="offline-fixture", opted_in_categories=()):
    """Correlate one labeled split and report it, plus the label disagreements."""
    cases = []
    label_mismatches = []
    label_unanswered = []
    for index, row in enumerate(document["cases"]):
        try:
            typed = judgment(
                row["case_id"],
                row["category"],
                row["judgment_decision"],
                row.get("confidence", 0.0),
                row.get("component_ms", 0.0),
                deferral=row.get("deferral"),
                failure=row.get("failure"),
            )
        except ShadowError as error:
            raise ShadowError(f"{error} (case {row.get('case_id')})") from error
        host = host_decision(
            row["case_id"],
            row["host_outcome"],
            row.get("host_ms", 0.0),
            row["command_sha256"],
            row.get("session_ref", f"fixture-session-{index}"),
        )
        case = correlate(typed, host)
        entry = {
            "case_id": row["case_id"],
            "labeled": row["label_decision"],
            "judgment": row["judgment_decision"],
        }
        if case["kind"] in ("deferral", "failure"):
            if row["label_decision"] != row["judgment_decision"]:
                label_unanswered.append({**entry, "reason": case["reason"]})
        elif row["label_decision"] != row["judgment_decision"]:
            label_mismatches.append(entry)
        cases.append(case)
    report = shadow_report(
        cases, tier=tier, opted_in_categories=opted_in_categories, documents=[document]
    )
    report["label_mismatches"] = label_mismatches
    report["label_unanswered"] = label_unanswered
    report["labeling_procedure"] = document["labeling_procedure"]
    report["split"] = document.get("split")
    return report


def _cmd_report(args):
    document = load_set(args.set)
    report = run_split(document, tier=args.tier)
    if args.raw_commands:
        # A reviewer re-running this against a rule that carries real command
        # text can hand that text over, so the audit is against the corpus it
        # actually came from rather than an empty list.
        report["redaction"] = redaction_audit(
            report["cases"], raw_commands=args.raw_commands, documents=[document]
        )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(render_markdown(report), encoding="utf-8")
    print(text if not args.out else f"wrote {args.out}")
    return 0


def render_markdown(report):
    """Render the report so a reviewer sees the deferrals and failures first."""
    topline = report["topline"]
    lines = [
        "# Shadow comparison report",
        "",
        f"Tier: `{report['tier']}`. Cases: {topline['cases']} "
        f"(agreements {topline['agreements']}, disagreements {topline['disagreements']}, "
        f"deferrals {topline['deferrals']}, failures {topline['failures']}).",
        "",
        "| Case | Category | Judgment | Host | Kind | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in (
        report.get("failures", [])
        + report.get("deferrals", [])
        + report["disagreements"]
    ):
        lines.append(
            f"| `{case['case_id']}` | `{case['category']}` | `{case['judgment']}` | "
            f"`{case['host']}` | `{case['kind']}` | `{case.get('reason')}` |"
        )
    lines += [
        "",
        "Not measured by this run: " + ", ".join(report["unmeasured"]) + ".",
        "",
    ]
    return "\n".join(lines)


def _cmd_gate(args):
    criteria = _load_criteria(args.criteria)
    document = load_set(args.set)
    split = document.get("split")
    declared_split = args.split
    if split != declared_split:
        # The criteria are declared as evaluated on one split. Measuring them on
        # the split that was tuned on, or on a set that does not say which it is,
        # would make the verdict meaningless, so it is refused rather than
        # approximated.
        raise ShadowError(
            f"E_HOLDOUT_REQUIRED: {args.set} is split {split!r}, but the criteria "
            f"are declared for {declared_split!r}"
        )
    evaluated_on = _criteria_document(args.criteria).get("evaluated_on")
    if evaluated_on != declared_split:
        raise ShadowError(
            f"E_HOLDOUT_REQUIRED: {args.criteria} declares evaluated_on="
            f"{evaluated_on!r}, not {declared_split!r}"
        )
    report = run_split(
        document, tier=args.tier, opted_in_categories=args.category or ()
    )
    problems = verify_report(report)
    if problems:
        raise ShadowError("E_REPORT_INCONSISTENT: " + "; ".join(problems))
    observed = metrics(report)
    drift = verify_freeze(args.freeze, {declared_split: args.set})
    state = enforcement_state(
        os.environ,
        criteria=criteria,
        observed=observed,
        set_digest_ok=not drift,
        opted_in_categories=args.category or (),
    )
    state["evaluation_set"] = args.set
    state["evaluated_split"] = declared_split
    state["freeze_drift"] = drift
    state["observed"] = observed
    state["report_problems"] = problems
    text = json.dumps(state, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text if not args.out else f"wrote {args.out}")
    return 10 if state["enabled"] else 0


def _cmd_freeze(args):
    paths = {}
    for item in args.split:
        name, _, path = item.partition("=")
        if not name or not path:
            raise ShadowError(f"E_SPLIT_SPEC: {item!r} is not <name>=<path>")
        paths[name] = path
    document = freeze(paths, args.out)
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


def _cmd_verify(args):
    document = load_set(args.set)
    report = run_split(
        document, tier=args.tier, opted_in_categories=args.category or ()
    )
    if args.raw_commands:
        report["redaction"] = redaction_audit(
            report["cases"], raw_commands=args.raw_commands, documents=[document]
        )
    problems = verify_report(report)
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    print(
        json.dumps(
            {
                "split": report["split"],
                "topline": report["topline"],
                "unmeasured": report["unmeasured"],
                "redaction": report["redaction"],
                "problems": problems,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", default="offline-fixture")
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="correlate one labeled split")
    report.add_argument("--set", required=True)
    report.add_argument("--out")
    report.add_argument("--markdown-out")
    report.add_argument(
        "--raw-command",
        dest="raw_commands",
        action="append",
        default=[],
        help="raw text to prove absent from the records; repeat as needed",
    )
    report.set_defaults(func=_cmd_report)

    gate = subparsers.add_parser("gate", help="decide whether enforcement may be on")
    gate.add_argument("--set", required=True)
    gate.add_argument("--criteria", required=True)
    gate.add_argument("--freeze", required=True)
    gate.add_argument(
        "--split",
        default="holdout",
        help="the only split the declared criteria may be evaluated on",
    )
    gate.add_argument("--category", action="append")
    gate.add_argument("--out")
    gate.set_defaults(func=_cmd_gate)

    freeze_cmd = subparsers.add_parser("freeze", help="pin the split digests")
    freeze_cmd.add_argument(
        "--split",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="a labeled split to pin; repeat for the calibration and holdout splits",
    )
    freeze_cmd.add_argument("--out", required=True)
    freeze_cmd.set_defaults(func=_cmd_freeze)

    verify_cmd = subparsers.add_parser(
        "verify", help="re-check one split's report against itself"
    )
    verify_cmd.add_argument("--set", required=True)
    verify_cmd.add_argument("--report", help="also write the report here")
    verify_cmd.add_argument("--category", action="append")
    verify_cmd.add_argument(
        "--raw-command",
        dest="raw_commands",
        action="append",
        default=[],
        help="raw text to prove absent from the records; repeat as needed",
    )
    verify_cmd.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ShadowError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
