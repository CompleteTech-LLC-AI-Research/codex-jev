#!/usr/bin/env python3
"""Shadow comparison and the controlled enforcement gate for the approval preflight.

This is the host half of the last clause of contract ``C6`` in
[``CONTRACTS.md``](CONTRACTS.md): "The final host execution decision remains
authoritative, and enforcement stays disabled until declared evaluation criteria
are met. Shadow reports include every failure and deferral." It backs issue
[#23](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/23).

Three facts are load-bearing:

* **Correlation is by review id, never by content.** The native adapter keeps the
  Guardian review id for the attempt it replaced, so a JEV judgment and the
  host's final decision for the same attempt share ``request_id``. A record that
  carries command text, a patch body, a provider payload, or a credential is
  refused (``E_SHADOW_RAW_FIELD``) instead of being copied into a report, and
  only flat scalar fields are accepted at all, so a nested command object cannot
  smuggle content through a field the deny-list does not name.
* **Every attempt is recounted, not asserted.** The report counts each JEV row in
  exactly one outcome bucket, counts every deferral and every failure separately,
  and recomputes ``all_attempts_categorized`` from those counts, so a report
  cannot look better by dropping the attempts it could not pair. Unpaired ids are
  retained in full rather than discarded.
* **The gate never flips a switch.** ``approval.enforcement`` stays at its
  declared ``false`` default; the gate only reports whether the manifest's
  declared criteria are met, so enabling enforcement stays an explicit operator
  action on ``JEV_SWITCH_APPROVAL_ENFORCEMENT``. No speedup is claimed: latency
  is reported only from observed measurements, and an unmeasured run blocks the
  gate rather than being reported as fast.

The labeled evaluation set this consumes must be independently adjudicated. The
fixture shipped in ``jev/tests/approval_fixtures/`` is illustrative only and is
not a safety benchmark; ``split`` freezes a calibration/holdout partition by
scenario family so a holdout cannot be silently re-tuned.

The frozen split is not advisory paperwork. ``split`` records a digest of the
labelled rows it partitioned and of the holdout rows it selected, and ``gate``
re-derives both when it is given the split (``gate --split``). A report whose
measured rows are not the declared holdout is refused (``E_HOLDOUT_REQUIRED``)
rather than warned about, a split edited after it was frozen leaves enforcement
disabled and names the drift, and a permitted verdict names the split, its seed
and its digest, so a permission can be read against the exact set that produced
it. A ``gate`` run without ``--split`` behaves exactly as it did before and says
in its verdict that no holdout was checked.

Subcommands:

``correlate``  pair a JEV audit stream with a normalized Guardian stream (and
               optional independent labels) and write a shadow report;
``gate``       evaluate a shadow report against the declared enforcement criteria,
               optionally binding it to a frozen split;
``criteria``   print the declared criteria and the current switch/gate state;
``split``      freeze a calibration/holdout split by scenario family.

Exit codes: 0 ok, 1 refused or criteria unmet, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_manifest  # noqa: E402

COMPONENT_ID = "jev-codex-approval"
SHADOW_SWITCH = "approval.preflight"
ENFORCE_SWITCH = "approval.enforcement"

REPORT_KIND = "approval_shadow_report"
REPORT_SCHEMA = "jev-approval.shadow.v1"
GATE_SCHEMA = "jev-approval.enforcement-gate.v1"
SPLIT_SCHEMA = "jev-approval.evaluation-split.v1"

# The only decisions a record may carry, per stream. A shadow report never invents
# an outcome it was not given, so an unknown value is a refusal, not a bucket.
JEV_DECISIONS = ("allow", "deny", "defer", "error")
GUARDIAN_DECISIONS = ("allow", "deny", "defer", "error", "timeout")
LABELS = ("allow", "deny")

# Bounds. A shadow stream is metadata, so both a per-record and a whole-file bound
# are enforced; content that exceeds them is refused rather than truncated.
MAX_RECORD_BYTES = 8192
MAX_STREAM_BYTES = 64 * 1024 * 1024

# Field names that carry raw content or a credential. A record may name an action
# class and a digest, never the command text or a secret, so a record that carries
# one is refused instead of being copied into a report.
RAW_FIELD_RE = re.compile(
    r"^(command|cmd|argv|args|patch|patch_text|content|text|output|stdout|stderr|"
    r"prompt|messages|input|body|token|secret|api_key|apikey|authorization|"
    r"credential|password)$",
    re.IGNORECASE,
)

# Stable refusal codes.
E_SHADOW_SCHEMA = "E_SHADOW_SCHEMA"
E_SHADOW_SHAPE = "E_SHADOW_SHAPE"
E_SHADOW_RAW_FIELD = "E_SHADOW_RAW_FIELD"
E_SHADOW_DECISION = "E_SHADOW_DECISION"
E_SHADOW_DUPLICATE = "E_SHADOW_DUPLICATE"
E_SHADOW_BOUND = "E_SHADOW_BOUND"
E_SHADOW_TIMING = "E_SHADOW_TIMING"
E_GATE_CRITERIA = "E_GATE_CRITERIA"
E_SPLIT_FAMILY = "E_SPLIT_FAMILY"
E_HOLDOUT_REQUIRED = "E_HOLDOUT_REQUIRED"
E_HOLDOUT_SPLIT = "E_HOLDOUT_SPLIT"


class ShadowError(ValueError):
    """A bounded, non-sensitive refusal. Its message never contains record text."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def switch_key(feature: str) -> str:
    """The declared switch key for a feature (CONTRACTS.md C10)."""
    return "JEV_SWITCH_" + feature.upper().replace(".", "_")


def switch_on(feature: str, env=None) -> bool:
    """``1`` is the only value that turns a declared switch on."""
    environ = os.environ if env is None else env
    return environ.get(switch_key(feature)) == "1"


def load_manifest():
    return jev_manifest.load_json(jev_manifest.default_manifest_path(), "manifest")


def _component(manifest, component_id):
    for component in manifest.get("components", []):
        if isinstance(component, dict) and component.get("id") == component_id:
            return component
    raise ShadowError(E_SHADOW_SCHEMA, f"manifest has no component {component_id}")


def declared_evaluation(manifest) -> dict:
    """The declared evaluation and enforcement record for the approval component."""
    record = _component(manifest, COMPONENT_ID).get("evaluation")
    if not isinstance(record, dict):
        raise ShadowError(
            E_SHADOW_SCHEMA, f"{COMPONENT_ID} declares no evaluation record"
        )
    return record


def declared_criteria(manifest) -> dict:
    criteria = declared_evaluation(manifest).get("criteria")
    if not isinstance(criteria, dict) or not criteria:
        raise ShadowError(E_GATE_CRITERIA, "evaluation declares no criteria")
    return criteria


def _raw_paths(value, prefix=""):
    """Yield the dotted paths of any key that names raw content or a credential."""
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}{key}"
            if RAW_FIELD_RE.match(str(key)):
                yield path
            yield from _raw_paths(item, f"{path}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _raw_paths(item, f"{prefix}{index}.")


def _check_flat_scalars(value, prefix=""):
    """Refuse nested structure: a shadow record is metadata, not a payload."""
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, (dict, list)):
            raise ShadowError(
                E_SHADOW_SHAPE, f"field {path} must be a scalar, not a nested value"
            )


def _timing(row):
    """The observed milliseconds a record carries, or ``None`` when it carries none."""
    for key in ("elapsed_ms", "latency_ms"):
        if key in row:
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ShadowError(E_SHADOW_TIMING, f"{key} must be a number")
            if value < 0:
                raise ShadowError(E_SHADOW_TIMING, f"{key} must not be negative")
            return float(value)
    return None


def read_records(text, *, kind):
    """Parse a JSONL stream into ``{request_id: row}``, refusing unsafe records.

    ``kind`` selects the decision vocabulary and the required label field, so the
    three streams the comparator consumes are validated by one path. Order is
    preserved on the returned ``record_ids`` list for stable unpaired reporting.
    """
    if kind == "labels":
        id_field, decisions, value_field = "request_id", LABELS, "label"
    elif kind == "jev":
        id_field, decisions, value_field = "request_id", JEV_DECISIONS, None
    else:
        id_field, decisions, value_field = "request_id", GUARDIAN_DECISIONS, None
    rows: dict[str, dict] = {}
    order: list[str] = []
    if len(text.encode("utf-8")) > MAX_STREAM_BYTES:
        raise ShadowError(E_SHADOW_BOUND, "stream exceeds the metadata bound")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ShadowError(E_SHADOW_BOUND, f"line {number} exceeds the record bound")
        try:
            row = json.loads(line)
        except ValueError as error:
            raise ShadowError(E_SHADOW_SCHEMA, f"line {number} is not JSON") from error
        if not isinstance(row, dict):
            raise ShadowError(E_SHADOW_SCHEMA, f"line {number} is not an object")
        raw = sorted(_raw_paths(row))
        if raw:
            raise ShadowError(
                E_SHADOW_RAW_FIELD,
                f"line {number} carries raw content or a credential at {', '.join(raw)}",
            )
        _check_flat_scalars(row)
        key = row.get(id_field)
        if not isinstance(key, str) or not key:
            raise ShadowError(E_SHADOW_SCHEMA, f"line {number} needs a {id_field}")
        if key in rows:
            raise ShadowError(
                E_SHADOW_DUPLICATE,
                f"{key} appears twice; select one attempt explicitly before comparing",
            )
        if value_field is not None:
            if row.get(value_field) not in decisions:
                raise ShadowError(
                    E_SHADOW_DECISION, f"{key} has no independent {value_field}"
                )
        else:
            value = row.get("decision", row.get("outcome"))
            if value not in decisions:
                raise ShadowError(
                    E_SHADOW_DECISION,
                    f"{key} decision {value!r} is outside {decisions}",
                )
            if kind == "jev" and row.get("candidate") not in (None, *decisions):
                raise ShadowError(
                    E_SHADOW_DECISION,
                    f"{key} candidate {row['candidate']!r} is outside {decisions}",
                )
        rows[key] = row
        order.append(key)
    return rows, order


def _percentile(values, fraction):
    """Linear-interpolation percentile (the numpy default), or ``None`` if empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = fraction * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _tally(rows, key_of):
    counts = {}
    for row in rows:
        value = key_of(row)
        counts[value] = counts.get(value, 0) + 1
    return counts


def correlate(jev_text, guardian_text, labels_text=None) -> dict:
    """Build a shadow report from the two streams and optional independent labels."""
    jev, _ = read_records(jev_text, kind="jev")
    guardian, _ = read_records(guardian_text, kind="guardian")
    labels = {}
    if labels_text is not None:
        labels, _ = read_records(labels_text, kind="labels")

    paired = sorted(set(jev) & set(guardian))
    only_jev = sorted(set(jev) - set(guardian))
    only_guardian = sorted(set(guardian) - set(jev))

    def candidate(record_id):
        row = jev[record_id]
        return row.get("candidate", row.get("decision"))

    def decision(record_id):
        row = jev[record_id]
        return row.get("decision", row.get("candidate"))

    def outcome(record_id):
        row = guardian[record_id]
        return row.get("decision", row.get("outcome"))

    disagreements = [
        {"request_id": key, "jev": candidate(key), "guardian": outcome(key)}
        for key in paired
        if candidate(key) != outcome(key)
    ]

    outcomes = {
        "jev": _tally(jev, decision),
        "guardian": _tally(guardian, outcome),
    }
    covered = sum(outcomes["jev"].values())
    total = len(jev)

    observed = [
        _timing(guardian[key]) for key in paired if _timing(guardian[key]) is not None
    ]
    latency = {
        "unit": "ms",
        "source": "observed" if observed else "unmeasured",
        "count": len(observed),
        "p50": _percentile(observed, 0.50),
        "p95": _percentile(observed, 0.95),
        "p99": _percentile(observed, 0.99),
        "max": max(observed) if observed else None,
    }

    report = {
        "report_schema": REPORT_SCHEMA,
        "report_kind": REPORT_KIND,
        "component": COMPONENT_ID,
        "guardian_is_ground_truth": False,
        "speedup_claimed": False,
        "pairing": {
            "paired": len(paired),
            "only_jev": only_jev,
            "only_guardian": only_guardian,
        },
        "outcomes": outcomes,
        "fallbacks": {
            "jev_deferrals": outcomes["jev"].get("defer", 0),
            "jev_failures": outcomes["jev"].get("error", 0),
            "unpaired": len(only_jev) + len(only_guardian),
            "total_attempts": total,
            "categorized": covered,
            "all_attempts_categorized": covered == total,
        },
        "disagreements": disagreements,
        "disagreement_rate": len(disagreements) / len(paired) if paired else None,
        "latency_ms": latency,
    }
    if labels_text is not None:
        labeled = [key for key in paired if key in labels]
        unsafe = [key for key in labeled if labels[key]["label"] == "deny"]
        allowed = [key for key in labeled if candidate(key) == "allow"]
        false_allows = [key for key in allowed if labels[key]["label"] == "deny"]

        def family_of(key):
            # The split is frozen from the labels file, so its family is the one
            # that binds a measured row to a holdout; the JEV row is the fallback.
            value = labels[key].get("scenario_family") or jev[key].get(
                "scenario_family"
            )
            return str(value) if value else ""

        families = {family_of(key) for key in labeled if family_of(key)}
        report["independent_labels"] = {
            "paired_labeled": len(labeled),
            "unlabeled_paired": len(paired) - len(labeled),
            "allow_count": len(allowed),
            "unsafe_count": len(unsafe),
            "false_allow_count": len(false_allows),
            "allow_error_rate": len(false_allows) / len(allowed) if allowed else None,
            "unsafe_acceptance_rate": (
                len(false_allows) / len(unsafe) if unsafe else None
            ),
            "scenario_families": len(families),
            "families": sorted(families),
            # What the numbers above were actually computed from, so a gate can
            # prove they came from the declared holdout rather than from a
            # re-drawn or union split. Metadata only: an identity is a digest.
            "label_records_digest": labelled_rows_digest(list(labels.values())),
            "measured_members": sorted(
                member({"request_id": key, "scenario_family": family_of(key)})
                for key in labeled
            ),
            "zero_error_one_sided_95_upper_bound": (
                1 - 0.05 ** (1 / len(allowed)) if allowed and not false_allows else None
            ),
        }
    return report


def gate(report, criteria, split=None) -> dict:
    """Evaluate a shadow report against the declared criteria.

    The gate is advisory: it never sets a switch, and ``enforcement_enabled`` is
    always ``False``. It reports ``permitted: True`` only when every criterion
    holds, so "enforcement stays disabled until declared evaluation criteria are
    met" is a computed statement rather than a promise.

    When ``split`` is supplied the criteria are only half the question: a
    criteria-passing report still may not permit enforcement unless its measured
    rows are the declared holdout of a split that still matches its own digest.
    The three outcomes are deliberately different, because they mean different
    things to an operator:

    * **refused** (``E_HOLDOUT_REQUIRED``) - the report records no measured rows,
      was measured against different labelled rows than the split, or measured at
      least one row outside the holdout. Enforcement cannot be justified from the
      split that was tuned on, so this is a refusal, not a false verdict.
    * **drift reported** - the split document no longer matches its own digest, so
      it was edited after it was frozen. Enforcement stays disabled and the
      verdict names the drift instead of erroring, because "this split changed"
      is a finding to record, not a malformed input.
    * **incomplete holdout** - every measured row is in the holdout but some
      holdout row was not measured. The verdict is reported and does not permit.
    """
    reasons = []
    fallbacks = report.get("fallbacks") or {}
    if not fallbacks.get("all_attempts_categorized"):
        reasons.append("report_drops_attempts")
    pairing = report.get("pairing") or {}
    if (
        pairing.get("only_jev")
        or pairing.get("only_guardian")
        or not pairing.get("paired")
    ):
        reasons.append("unpaired_or_empty")

    labels = report.get("independent_labels") or {}
    if labels.get("paired_labeled", 0) < criteria.get("min_labeled_pairs", 0):
        reasons.append("insufficient_labeled_pairs")
    if labels.get("scenario_families", 0) < criteria.get("min_scenario_families", 0):
        reasons.append("insufficient_scenario_families")
    allow_error_rate = labels.get("allow_error_rate")
    if allow_error_rate is None or allow_error_rate > criteria.get(
        "max_false_allow_rate", 0.0
    ):
        reasons.append("false_allow_rate_unknown_or_high")

    disagreement_rate = report.get("disagreement_rate")
    if disagreement_rate is None or disagreement_rate > criteria.get(
        "max_disagreement_rate", 1.0
    ):
        reasons.append("disagreement_rate_too_high")

    total = fallbacks.get("total_attempts", 0)
    defer_rate = fallbacks.get("jev_deferrals", 0) / total if total else None
    if defer_rate is None or defer_rate > criteria.get("max_defer_rate", 1.0):
        reasons.append("defer_rate_too_high")

    latency = report.get("latency_ms") or {}
    if criteria.get("require_measured_latency", True):
        if latency.get("source") != "observed":
            reasons.append("latency_unmeasured")
        elif latency.get("p95") is None or latency["p95"] > criteria.get(
            "max_latency_p95_ms", float("inf")
        ):
            reasons.append("latency_p95_too_high")

    verdict = {
        "gate_schema": GATE_SCHEMA,
        "permitted": not reasons,
        "enforcement_enabled": False,
        "enforcement_switch": ENFORCE_SWITCH,
        "shadow_switch": SHADOW_SWITCH,
        "reasons": reasons,
        "criteria": dict(criteria),
    }
    if split is None:
        verdict["holdout"] = {
            "checked": False,
            "detail": (
                "no frozen split was supplied, so this verdict does not bind the "
                "measured rows to the holdout they were declared against"
            ),
        }
        return verdict

    missing = [
        key
        for key in (
            "split_digest",
            "seed",
            "holdout_fraction",
            "calibration_families",
            "holdout_families",
            "labelled_rows_digest",
            "holdout_members",
        )
        if key not in split
    ]
    if missing:
        raise ShadowError(
            E_HOLDOUT_SPLIT,
            f"the frozen split is missing {', '.join(missing)}; re-freeze it",
        )
    observed = split["split_digest"]
    verdict["split"] = {
        "seed": split["seed"],
        "holdout_fraction": split["holdout_fraction"],
        "split_digest": observed,
        "labelled_rows_digest": split["labelled_rows_digest"],
        "holdout_families": sorted(split["holdout_families"]),
        "holdout_rows": len(split["holdout_members"]),
    }
    expected = split_digest(split)
    if expected != observed:
        verdict["permitted"] = False
        verdict["reasons"] = [*reasons, "split_drift"]
        verdict["drift"] = {
            "expected": expected,
            "observed": observed,
            "detail": (
                "the split does not match its own digest, so it changed after it "
                "was frozen; enforcement stays disabled"
            ),
        }
        verdict["holdout"] = {
            "checked": False,
            "detail": "the split is drifted, so its holdout is not compared",
        }
        return verdict

    labels = report.get("independent_labels") or {}
    if not labels.get("measured_members"):
        raise ShadowError(
            E_HOLDOUT_REQUIRED,
            "the report records no measured rows, so it cannot be bound to a holdout",
        )
    if labels.get("label_records_digest") != split["labelled_rows_digest"]:
        raise ShadowError(
            E_HOLDOUT_REQUIRED,
            "the report was measured against different labelled rows than the frozen split",
        )
    measured = set(labels["measured_members"])
    holdout = set(split["holdout_members"])
    outside = sorted(measured - holdout)
    if outside:
        raise ShadowError(
            E_HOLDOUT_REQUIRED,
            f"{len(outside)} measured row(s) are outside the declared holdout; "
            "the split that was tuned on cannot justify enforcement",
        )
    unmeasured = sorted(holdout - measured)
    if unmeasured:
        reasons.append("holdout_incomplete")
    verdict["holdout"] = {
        "checked": True,
        "measured_rows": len(measured),
        "holdout_rows": len(holdout),
        "unmeasured": len(unmeasured),
    }
    verdict["permitted"] = not reasons
    verdict["reasons"] = reasons
    return verdict


def switch_state(manifest, env=None) -> dict:
    """The declared switches and the current effective enforcement state."""
    evaluation = declared_evaluation(manifest)
    shadow_on = switch_on(evaluation["shadow_switch"], env)
    enforce_on = switch_on(evaluation["enforce_switch"], env)
    return {
        "shadow_switch": evaluation["shadow_switch"],
        "shadow_enabled": shadow_on,
        "enforce_switch": evaluation["enforce_switch"],
        "enforce_switch_set": enforce_on,
        # Enforcement is reported inactive unconditionally here: activation needs a
        # gate-permitted report *and* both switches set, and the gate never writes a
        # switch, so this module cannot observe an active enforcement state.
        "enforcement_active": False,
        "note": (
            "the host reads only JEV_SWITCH_<FEATURE>; the gate reports readiness "
            "and never writes a switch"
        ),
    }


def freeze_split(rows, *, holdout_fraction=0.4, seed=0, freeze=None) -> dict:
    """Partition labeled rows by scenario family and record the frozen pins.

    Rows without a ``scenario_family`` are refused, because splitting by row would
    leak near-duplicate commands or contexts across the split. The split is
    deterministic in ``(family, seed)`` so it can be reproduced, and ``freeze``
    records the model version, question hash and effective policy hash so a change
    to any of them invalidates the holdout instead of silently inheriting it.

    The document also carries the digest of the labelled rows it partitioned, the
    identity of every holdout row, and a ``split_digest`` over exactly those
    fields, so :func:`gate` can prove a report was measured on this holdout
    rather than on the split that was tuned on. None of these are content: an
    identity is a digest of a review id and a family name.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ShadowError(E_SPLIT_FAMILY, "holdout_fraction must be between 0 and 1")
    families = []
    for row in rows:
        family = row.get("scenario_family")
        if not isinstance(family, str) or not family:
            raise ShadowError(
                E_SPLIT_FAMILY,
                "every labeled row needs a scenario_family; splitting by row leaks near-duplicates",
            )
        if family not in families:
            families.append(family)
    ordered = sorted(families, key=lambda family: hash_digest(f"{seed}:{family}"))
    cut = max(1, round(len(ordered) * holdout_fraction)) if ordered else 0
    holdout = set(ordered[:cut])
    calibration = [row for row in rows if row["scenario_family"] not in holdout]
    evaluation = [row for row in rows if row["scenario_family"] in holdout]
    document = {
        "split_schema": SPLIT_SCHEMA,
        "seed": seed,
        "holdout_fraction": holdout_fraction,
        "calibration_families": sorted(set(families) - holdout),
        "holdout_families": sorted(holdout),
        "calibration_count": len(calibration),
        "holdout_count": len(evaluation),
        "freeze": dict(freeze or {}),
        "labelled_rows_digest": labelled_rows_digest(rows),
        "holdout_members": sorted(member(row) for row in evaluation),
        "note": (
            "freeze model version, question hash and effective policy hash before "
            "holdout evaluation; a change invalidates this split"
        ),
    }
    document["split_digest"] = split_digest(document)
    return document


def _row_order(row) -> str:
    """A stable sort key for a labelled row, so a digest never depends on file order."""
    return str(row.get("request_id") or "")


def member(row) -> str:
    """A non-content identity for one labelled row: its review id and its family."""
    return _digest(
        {
            "request_id": str(row.get("request_id") or ""),
            "scenario_family": str(row.get("scenario_family") or ""),
        }
    )


def labelled_rows_digest(rows) -> str:
    """A digest of the labelled rows themselves, independent of their order."""
    return _digest(sorted(rows, key=_row_order))


def split_digest(split) -> str:
    """Re-derive a frozen split's own digest from the content it carries.

    ``gate`` calls this on the split it is handed and compares it with the
    document's own ``split_digest``, so a partition edited after it was frozen is
    detected rather than inherited.
    """
    return _digest(
        {
            "seed": split.get("seed"),
            "holdout_fraction": split.get("holdout_fraction"),
            "calibration_families": list(split.get("calibration_families") or []),
            "holdout_families": list(split.get("holdout_families") or []),
            "labelled_rows_digest": split.get("labelled_rows_digest"),
            "holdout_members": sorted(split.get("holdout_members") or []),
        }
    )


def hash_digest(text: str) -> str:
    """A stable digest for a family label, independent of Python's hash salt."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    corr = sub.add_parser("correlate", help="write a shadow report")
    corr.add_argument("--jev", required=True, help="JEV audit JSONL")
    corr.add_argument("--guardian", required=True, help="normalized Guardian JSONL")
    corr.add_argument("--labels", help="independent adjudications: request_id, label")
    corr.add_argument(
        "--split",
        help="frozen split to bind the report to; the embedded verdict refuses a "
        "report whose measured rows are not its holdout",
    )
    corr.add_argument("--out", required=True, help="report path, or - for stdout")

    gate_cmd = sub.add_parser("gate", help="evaluate a report against the criteria")
    gate_cmd.add_argument("--report", required=True, help="shadow report JSON")
    gate_cmd.add_argument(
        "--split",
        help="frozen split the report was measured against; without it the holdout "
        "is not checked and the verdict says so",
    )

    sub.add_parser("criteria", help="print the declared criteria and switch state")

    split = sub.add_parser("split", help="freeze a calibration/holdout split")
    split.add_argument("--labels", required=True, help="labeled evaluation set JSONL")
    split.add_argument("--holdout-fraction", type=float, default=0.4)
    split.add_argument("--seed", type=int, default=0)
    split.add_argument("--freeze", help="JSON file of pins to record with the split")
    split.add_argument("--out", help="split path, or - for stdout")

    args = parser.parse_args(argv)
    manifest = load_manifest()

    if args.command == "correlate":
        report = correlate(
            _read(args.jev),
            _read(args.guardian),
            _read(args.labels) if args.labels else None,
        )
        split = json.loads(_read(args.split)) if args.split else None
        if split is not None:
            report["evaluation_split"] = split
        verdict = gate(report, declared_criteria(manifest), split)
        report["enforcement"] = verdict
        text = json.dumps(report, indent=2) + "\n"
        if args.out == "-":
            sys.stdout.write(text)
        else:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
        return 0 if report["fallbacks"]["all_attempts_categorized"] else 1

    if args.command == "gate":
        report = json.loads(_read(args.report))
        split = json.loads(_read(args.split)) if args.split else None
        verdict = gate(report, declared_criteria(manifest), split)
        sys.stdout.write(json.dumps(verdict, indent=2) + "\n")
        return 0 if verdict["permitted"] else 1

    if args.command == "criteria":
        state = {
            "criteria": declared_criteria(manifest),
            "opt_in_action_categories": declared_evaluation(manifest).get(
                "opt_in_action_categories", []
            ),
            "switch_state": switch_state(manifest),
        }
        sys.stdout.write(json.dumps(state, indent=2) + "\n")
        return 0

    rows = [
        json.loads(line) for line in _read(args.labels).splitlines() if line.strip()
    ]
    split = freeze_split(
        rows,
        holdout_fraction=args.holdout_fraction,
        seed=args.seed,
        freeze=json.loads(_read(args.freeze)) if args.freeze else None,
    )
    text = json.dumps(split, indent=2) + "\n"
    if args.out and args.out != "-":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ShadowError as error:
        sys.stderr.write(f"{error}\n")
        sys.exit(1)
