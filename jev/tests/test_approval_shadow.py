#!/usr/bin/env python3
"""Focused tests for the approval shadow report and the enforcement gate (#23).

These drive the host module directly with in-memory streams, so they prove what
the host half of contract C6 actually does: correlation by review id, a report
that recounts every deferral and failure, a refusal of raw content, and a gate
that never flips a switch and never claims a speedup. The manifest-declared
criteria are read from the real manifest, so a drift between the record and the
code fails here as well. Tier: ``offline-fixture``.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import approval_shadow as shadow  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "approval_fixtures"


def stream(rows):
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def jev_rows():
    return [
        {"request_id": "rev-1", "candidate": "allow", "decision": "defer"},
        {"request_id": "rev-2", "candidate": "deny", "decision": "defer"},
        {"request_id": "rev-3", "candidate": "defer", "decision": "defer"},
        {"request_id": "rev-4", "candidate": "error", "decision": "error"},
    ]


def guardian_rows():
    return [
        {"request_id": "rev-1", "decision": "allow", "elapsed_ms": 100.0},
        {"request_id": "rev-2", "decision": "deny", "elapsed_ms": 200.0},
        {"request_id": "rev-3", "decision": "defer", "elapsed_ms": 300.0},
        {"request_id": "rev-4", "decision": "allow", "elapsed_ms": 400.0},
    ]


def label_rows():
    return [
        {"request_id": "rev-1", "label": "allow", "scenario_family": "f1"},
        {"request_id": "rev-2", "label": "deny", "scenario_family": "f2"},
        {"request_id": "rev-3", "label": "allow", "scenario_family": "f3"},
        {"request_id": "rev-4", "label": "allow", "scenario_family": "f4"},
    ]


def passing_report():
    return {
        "fallbacks": {
            "total_attempts": 500,
            "jev_deferrals": 1,
            "all_attempts_categorized": True,
        },
        "pairing": {"paired": 500, "only_jev": [], "only_guardian": []},
        "disagreement_rate": 0.0,
        "latency_ms": {"source": "observed", "p95": 250.0},
        "independent_labels": {
            "paired_labeled": 500,
            "allow_error_rate": 0.0,
            "scenario_families": 25,
        },
    }


class ShadowReportTests(unittest.TestCase):
    def test_correlate_pairs_by_review_id_and_retains_unpaired(self):
        jev = stream(
            jev_rows()
            + [{"request_id": "rev-9", "candidate": "allow", "decision": "defer"}]
        )
        guardian = stream(
            guardian_rows()
            + [{"request_id": "rev-8", "decision": "allow", "elapsed_ms": 1.0}]
        )
        report = shadow.correlate(jev, guardian)
        self.assertEqual(report["pairing"]["paired"], 4)
        self.assertEqual(report["pairing"]["only_jev"], ["rev-9"])
        self.assertEqual(report["pairing"]["only_guardian"], ["rev-8"])
        self.assertEqual(report["fallbacks"]["unpaired"], 2)

    def test_report_counts_every_deferral_and_failure(self):
        report = shadow.correlate(stream(jev_rows()), stream(guardian_rows()))
        self.assertEqual(report["fallbacks"]["jev_deferrals"], 3)
        self.assertEqual(report["fallbacks"]["jev_failures"], 1)
        self.assertEqual(report["fallbacks"]["total_attempts"], 4)
        self.assertEqual(report["fallbacks"]["categorized"], 4)
        self.assertTrue(report["fallbacks"]["all_attempts_categorized"])
        # Unpaired ids are retained, so an attempt cannot vanish from the count.
        report = shadow.correlate(
            stream(
                jev_rows()
                + [{"request_id": "rev-9", "candidate": "allow", "decision": "defer"}]
            ),
            stream(guardian_rows()),
        )
        self.assertEqual(report["fallbacks"]["categorized"], 5)

    def test_disagreement_is_candidate_against_host_decision(self):
        report = shadow.correlate(stream(jev_rows()), stream(guardian_rows()))
        self.assertEqual(
            [row["request_id"] for row in report["disagreements"]], ["rev-4"]
        )
        self.assertEqual(report["disagreement_rate"], 0.25)
        self.assertFalse(report["guardian_is_ground_truth"])
        self.assertFalse(report["speedup_claimed"])

    def test_report_refuses_raw_content_fields(self):
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.correlate(
                stream(
                    [
                        {
                            "request_id": "rev-1",
                            "candidate": "allow",
                            "decision": "defer",
                            "command": "rm -rf /",
                        }
                    ]
                ),
                stream(guardian_rows()),
            )
        self.assertEqual(caught.exception.code, shadow.E_SHADOW_RAW_FIELD)

    def test_report_refuses_nested_payloads(self):
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.correlate(
                stream(
                    [
                        {
                            "request_id": "rev-1",
                            "candidate": "allow",
                            "decision": "defer",
                            "action": {"tool": "exec_command"},
                        }
                    ]
                ),
                stream(guardian_rows()),
            )
        self.assertEqual(caught.exception.code, shadow.E_SHADOW_SHAPE)

    def test_report_refuses_duplicate_review_ids(self):
        rows = [{"request_id": "rev-1", "candidate": "allow", "decision": "defer"}] * 2
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.correlate(stream(rows), stream(guardian_rows()))
        self.assertEqual(caught.exception.code, shadow.E_SHADOW_DUPLICATE)

    def test_report_refuses_an_unknown_decision(self):
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.correlate(
                stream(
                    [{"request_id": "rev-1", "candidate": "maybe", "decision": "defer"}]
                ),
                stream(guardian_rows()),
            )
        self.assertEqual(caught.exception.code, shadow.E_SHADOW_DECISION)

    def test_latency_is_unmeasured_without_observations(self):
        guardian = stream(
            [
                {"request_id": f"rev-{index}", "decision": "allow"}
                for index in range(1, 5)
            ]
        )
        report = shadow.correlate(stream(jev_rows()), guardian)
        self.assertEqual(report["latency_ms"]["source"], "unmeasured")
        self.assertEqual(report["latency_ms"]["count"], 0)
        self.assertIsNone(report["latency_ms"]["p95"])

    def test_latency_percentiles_use_observed_values_only(self):
        report = shadow.correlate(stream(jev_rows()), stream(guardian_rows()))
        self.assertEqual(report["latency_ms"]["source"], "observed")
        self.assertEqual(report["latency_ms"]["count"], 4)
        self.assertEqual(report["latency_ms"]["max"], 400.0)

    def test_independent_labels_report_error_rates_separately(self):
        report = shadow.correlate(
            stream(jev_rows()), stream(guardian_rows()), stream(label_rows())
        )
        labels = report["independent_labels"]
        self.assertEqual(labels["paired_labeled"], 4)
        self.assertEqual(labels["allow_count"], 1)
        self.assertEqual(labels["unsafe_count"], 1)
        self.assertEqual(labels["false_allow_count"], 0)
        self.assertEqual(labels["allow_error_rate"], 0.0)
        self.assertEqual(labels["scenario_families"], 4)


class EnforcementGateTests(unittest.TestCase):
    def setUp(self):
        self.criteria = shadow.declared_criteria(shadow.load_manifest())

    def test_gate_never_enables_enforcement(self):
        failing = passing_report()
        failing["disagreement_rate"] = 0.9
        for report in (passing_report(), failing):
            verdict = shadow.gate(report, self.criteria)
            # A passing report still leaves the switch to the operator; a failing
            # one is no different. The gate only ever reports.
            self.assertFalse(verdict["enforcement_enabled"])
            self.assertEqual(verdict["enforcement_switch"], "approval.enforcement")

    def test_gate_permits_only_when_every_criterion_holds(self):
        verdict = shadow.gate(passing_report(), self.criteria)
        self.assertTrue(verdict["permitted"], verdict["reasons"])
        self.assertEqual(verdict["reasons"], [])
        self.assertFalse(verdict["enforcement_enabled"])

    def test_gate_blocks_on_unpaired_attempts(self):
        report = passing_report()
        report["pairing"]["only_jev"] = ["rev-9"]
        self.assertIn(
            "unpaired_or_empty", shadow.gate(report, self.criteria)["reasons"]
        )

    def test_gate_blocks_when_the_report_drops_attempts(self):
        report = passing_report()
        report["fallbacks"]["all_attempts_categorized"] = False
        self.assertIn(
            "report_drops_attempts", shadow.gate(report, self.criteria)["reasons"]
        )

    def test_gate_blocks_on_a_false_allow(self):
        report = passing_report()
        report["independent_labels"]["allow_error_rate"] = 0.02
        self.assertIn(
            "false_allow_rate_unknown_or_high",
            shadow.gate(report, self.criteria)["reasons"],
        )

    def test_gate_blocks_on_unmeasured_latency(self):
        report = passing_report()
        report["latency_ms"] = {"source": "unmeasured", "p95": None}
        self.assertIn(
            "latency_unmeasured", shadow.gate(report, self.criteria)["reasons"]
        )

    def test_gate_blocks_on_too_few_labeled_pairs(self):
        report = passing_report()
        report["independent_labels"]["paired_labeled"] = 1
        self.assertIn(
            "insufficient_labeled_pairs", shadow.gate(report, self.criteria)["reasons"]
        )

    def test_switch_state_reads_the_declared_environment_keys(self):
        off = shadow.switch_state(shadow.load_manifest(), env={})
        self.assertFalse(off["shadow_enabled"])
        self.assertFalse(off["enforce_switch_set"])
        self.assertFalse(off["enforcement_active"])
        on = shadow.switch_state(
            shadow.load_manifest(),
            env={
                "JEV_SWITCH_APPROVAL_PREFLIGHT": "1",
                "JEV_SWITCH_APPROVAL_ENFORCEMENT": "1",
            },
        )
        self.assertTrue(on["shadow_enabled"])
        self.assertTrue(on["enforce_switch_set"])
        # The gate never writes a switch, so enforcement is never reported active here.
        self.assertFalse(on["enforcement_active"])

    def test_switch_key_matches_the_declared_contract(self):
        self.assertEqual(
            shadow.switch_key("approval.enforcement"),
            "JEV_SWITCH_APPROVAL_ENFORCEMENT",
        )


class EvaluationSplitTests(unittest.TestCase):
    def test_split_refuses_rows_without_a_scenario_family(self):
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.freeze_split([{"request_id": "rev-1", "label": "allow"}])
        self.assertEqual(caught.exception.code, shadow.E_SPLIT_FAMILY)

    def test_split_is_deterministic_and_keeps_families_together(self):
        rows = [
            {
                "request_id": f"rev-{index}",
                "label": "allow",
                "scenario_family": f"f{index}",
            }
            for index in range(10)
        ]
        first = shadow.freeze_split(rows, seed=3)
        second = shadow.freeze_split(rows, seed=3)
        self.assertEqual(first["holdout_families"], second["holdout_families"])
        self.assertTrue(
            set(first["calibration_families"]).isdisjoint(first["holdout_families"])
        )

    def test_split_records_the_frozen_pins(self):
        rows = [{"request_id": "rev-1", "label": "allow", "scenario_family": "f1"}]
        split = shadow.freeze_split(rows, freeze={"model": "jev-1.13.0"})
        self.assertEqual(split["freeze"], {"model": "jev-1.13.0"})


def split_rows(count=10):
    return [
        {
            "request_id": f"rev-{index}",
            "label": "allow" if index % 2 else "deny",
            "scenario_family": f"fam-{index}",
        }
        for index in range(1, count + 1)
    ]


def holdout_report(split, *, members=None, label_digest=None):
    """A report that already satisfies every declared criterion, measured on ``split``."""
    report = passing_report()
    report["independent_labels"].update(
        {
            "families": sorted(split["holdout_families"]),
            "label_records_digest": (
                split["labelled_rows_digest"] if label_digest is None else label_digest
            ),
            "measured_members": sorted(
                split["holdout_members"] if members is None else members
            ),
        }
    )
    return report


class HoldoutBindingTests(unittest.TestCase):
    """#85: a permission has to be reproducible from the declared holdout alone."""

    def setUp(self):
        self.split = shadow.freeze_split(split_rows(), holdout_fraction=0.4, seed=3)
        self.criteria = shadow.declared_criteria(shadow.load_manifest())

    def test_a_frozen_split_verifies_against_its_own_digest(self):
        self.assertEqual(shadow.split_digest(self.split), self.split["split_digest"])
        self.assertTrue(self.split["holdout_members"])
        self.assertEqual(
            len(self.split["holdout_members"]), self.split["holdout_count"]
        )
        # The row identities are digests, never the rows themselves.
        for identity in self.split["holdout_members"]:
            self.assertNotIn("rev-", identity)

    def test_a_permitted_verdict_names_the_split_its_seed_and_its_digest(self):
        verdict = shadow.gate(
            holdout_report(self.split), self.criteria, split=self.split
        )
        self.assertTrue(verdict["permitted"], verdict["reasons"])
        self.assertEqual(verdict["split"]["seed"], 3)
        self.assertEqual(verdict["split"]["split_digest"], self.split["split_digest"])
        self.assertEqual(
            verdict["split"]["labelled_rows_digest"],
            self.split["labelled_rows_digest"],
        )
        self.assertEqual(
            verdict["split"]["holdout_families"], self.split["holdout_families"]
        )
        self.assertTrue(verdict["holdout"]["checked"])
        self.assertEqual(verdict["holdout"]["unmeasured"], 0)
        self.assertFalse(verdict["enforcement_enabled"])

    def test_a_report_measured_on_the_calibration_split_is_refused(self):
        calibration = [
            shadow.member(
                {
                    "request_id": f"rev-{index}",
                    "scenario_family": f"fam-{index}",
                }
            )
            for index in range(1, 11)
            if f"fam-{index}" in set(self.split["calibration_families"])
        ]
        self.assertTrue(calibration)
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.gate(
                holdout_report(self.split, members=calibration),
                self.criteria,
                split=self.split,
            )
        # A refusal, not a verdict that merely says "not permitted": the numbers
        # came from the split that was tuned on.
        self.assertEqual(caught.exception.code, shadow.E_HOLDOUT_REQUIRED)
        self.assertIn("outside the declared holdout", caught.exception.detail)

    def test_a_split_mutated_after_freezing_stops_permitting_enforcement(self):
        report = holdout_report(self.split)
        self.assertTrue(
            shadow.gate(report, self.criteria, split=self.split)["permitted"]
        )
        mutated = json.loads(json.dumps(self.split))
        moved = mutated["holdout_families"].pop()
        mutated["calibration_families"] = sorted(
            set(mutated["calibration_families"]) | {moved}
        )
        verdict = shadow.gate(report, self.criteria, split=mutated)
        # Drift is reported, not raised: "this split changed" is a finding.
        self.assertFalse(verdict["permitted"])
        self.assertIn("split_drift", verdict["reasons"])
        self.assertFalse(verdict["enforcement_enabled"])
        self.assertEqual(verdict["drift"]["observed"], mutated["split_digest"])
        self.assertNotEqual(verdict["drift"]["expected"], verdict["drift"]["observed"])
        self.assertFalse(verdict["holdout"]["checked"])

    def test_a_split_substituted_from_other_rows_is_refused(self):
        other = shadow.freeze_split(split_rows(count=4), holdout_fraction=0.4, seed=3)
        self.assertNotEqual(
            other["labelled_rows_digest"], self.split["labelled_rows_digest"]
        )
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.gate(holdout_report(self.split), self.criteria, split=other)
        self.assertEqual(caught.exception.code, shadow.E_HOLDOUT_REQUIRED)
        self.assertIn("different labelled rows", caught.exception.detail)

    def test_an_incomplete_holdout_does_not_permit_and_names_the_reason(self):
        members = self.split["holdout_members"][:-1]
        verdict = shadow.gate(
            holdout_report(self.split, members=members),
            self.criteria,
            split=self.split,
        )
        self.assertFalse(verdict["permitted"])
        self.assertIn("holdout_incomplete", verdict["reasons"])
        self.assertTrue(verdict["holdout"]["checked"])
        self.assertEqual(verdict["holdout"]["unmeasured"], 1)

    def test_a_report_with_no_measured_rows_cannot_be_bound_to_a_holdout(self):
        report = passing_report()
        report["independent_labels"].pop("scenario_families", None)
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.gate(report, self.criteria, split=self.split)
        self.assertEqual(caught.exception.code, shadow.E_HOLDOUT_REQUIRED)

    def test_a_split_without_its_binding_fields_is_a_usage_error(self):
        broken = json.loads(json.dumps(self.split))
        del broken["split_digest"]
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.gate(holdout_report(self.split), self.criteria, split=broken)
        self.assertEqual(caught.exception.code, shadow.E_HOLDOUT_SPLIT)

    def test_a_gate_without_a_split_still_works_and_says_so(self):
        plain = shadow.gate(holdout_report(self.split, members=[]), self.criteria)
        self.assertTrue(plain["permitted"])
        self.assertFalse(plain["holdout"]["checked"])
        self.assertFalse(plain["enforcement_enabled"])
        # The pre-#85 verdict surface is unchanged for callers that pass no split.
        self.assertEqual(plain["gate_schema"], shadow.GATE_SCHEMA)
        self.assertEqual(plain["shadow_switch"], shadow.SHADOW_SWITCH)


class RepositoryFixtureTests(unittest.TestCase):
    def test_the_shipped_fixture_is_not_permitted(self):
        report = shadow.correlate(
            (FIXTURES / "jev-audit.jsonl").read_text(encoding="utf-8"),
            (FIXTURES / "guardian.jsonl").read_text(encoding="utf-8"),
            (FIXTURES / "labels.jsonl").read_text(encoding="utf-8"),
        )
        verdict = shadow.gate(report, shadow.declared_criteria(shadow.load_manifest()))
        # A handful of hand-authored rows must not reach the declared threshold.
        self.assertFalse(verdict["permitted"])
        self.assertFalse(verdict["enforcement_enabled"])
        self.assertTrue(report["fallbacks"]["all_attempts_categorized"])


if __name__ == "__main__":
    unittest.main()
