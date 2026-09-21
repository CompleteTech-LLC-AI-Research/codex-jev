#!/usr/bin/env python3
"""Required-CI coverage for shadow comparison and the enforcement gate (#23).

These tests pin the three things #23 asks for, and they are written so that the
cheap ways of faking them fail a check:

* correlation joins a typed judgment to the host's own final decision without
  carrying raw command text or a secret, and a deferral or a failure is never
  counted as an agreement;
* the report is re-derivable from itself, so a case cannot be counted without
  also being listed, and a deferral or a failure cannot be dropped;
* the enforcement gate fails closed - it is computed, not hard-wired, so every
  single input is turned off in turn and the gate must go dark for each one.

Everything here is the ``offline-fixture`` tier. No model was called, no
provider answered, and the only timings are the fixture's own wall-clock spans.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import shadow_comparison as sc  # noqa: E402

MODULE = SCRIPTS / "shadow_comparison.py"
FIXTURES = REPO_ROOT / "jev" / "fixtures" / "shadow"
CALIBRATION = FIXTURES / "calibration.json"
HOLDOUT = FIXTURES / "holdout.json"
CRITERIA = FIXTURES / "criteria.json"
FROZEN = FIXTURES / "frozen.json"

# Text that would be a leak if it ever reached a record or a report. None of it
# is real; it exists so ``redaction_audit`` is asked about *something*.
PLAUSIBLE_RAW_TEXT = (
    "rm -rf /",
    "sudo -i",
    "curl -s https://example.invalid/deadbeef",
    "-----BEGIN PRIVATE KEY-----",
    "AWS_SECRET_ACCESS_KEY",
)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_cli(*args, env=None):
    """Run the module as a script with a controlled environment."""
    environment = {"PATH": os.environ.get("PATH", "")}
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(MODULE), *args],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=REPO_ROOT,
    )


def holdout_report():
    return sc.run_split(sc.load_set(HOLDOUT))


def satisfied_conditions():
    """The environment every check below switches off one at a time."""
    return {
        sc.GATE_SWITCH: "1",
        sc.ENFORCEMENT_SWITCH: "1",
        sc.CONSENT_ENV: "1",
        sc.MODEL_AVAILABLE_ENV: "1",
    }


MEASURABLE_CRITERIA = [
    {"name": "agreement", "metric": "agreement_rate", "op": ">=", "value": 0.95},
    {"name": "deferrals", "metric": "deferral_rate", "op": "<=", "value": 0.1},
    {"name": "failures", "metric": "failure_rate", "op": "<=", "value": 0.05},
    {"name": "disagreements", "metric": "disagreement_rate", "op": "<=", "value": 0.02},
    {
        "name": "label_mismatch",
        "metric": "label_mismatch_rate",
        "op": "<=",
        "value": 0.0,
    },
]


def clean_split(commands):
    """A holdout whose every case agrees, so a bounded criteria set is met."""
    cases = []
    for index, command in enumerate(commands, start=1):
        cases.append(
            {
                "case_id": f"clean-{index:03d}",
                "category": "shell_command",
                "command_sha256": sc.hash_command(command),
                "component_ms": 1.0,
                "confidence": 0.99,
                "host_ms": 2.0,
                "host_outcome": "approved",
                "judgment_decision": "allow",
                "label_decision": "allow",
                "session_ref": f"fixture-session-{index:03d}",
            }
        )
    return {
        "schema": sc.SET_SCHEMA,
        "split": "holdout",
        "labeling_procedure": "Synthetic agreement set built by the test itself.",
        "cases": cases,
    }


class CheckedInFixtureTests(unittest.TestCase):
    def test_shipped_splits_and_criteria_load(self):
        calibration = sc.load_set(CALIBRATION)
        holdout = sc.load_set(HOLDOUT)
        self.assertEqual("calibration", calibration["split"])
        self.assertEqual("holdout", holdout["split"])
        criteria = sc._criteria_document(CRITERIA)
        self.assertEqual(sc.CRITERIA_SCHEMA, criteria["schema"])
        self.assertEqual("holdout", criteria["evaluated_on"])
        self.assertTrue(criteria["criteria"])

    def test_splits_share_no_case(self):
        calibration = {row["case_id"] for row in sc.load_set(CALIBRATION)["cases"]}
        holdout = {row["case_id"] for row in sc.load_set(HOLDOUT)["cases"]}
        self.assertFalse(calibration & holdout, "a case may not be in both splits")

    def test_the_holdout_exercises_every_kind_the_report_must_keep(self):
        topline = holdout_report()["topline"]
        # A set with no disagreement, no deferral and no failure would make the
        # completeness checks below pass for the wrong reason.
        self.assertGreaterEqual(topline["disagreements"], 1)
        self.assertGreaterEqual(topline["deferrals"], 1)
        self.assertGreaterEqual(topline["failures"], 1)
        self.assertGreaterEqual(topline["agreements"], 1)

    def test_the_holdout_is_frozen_by_digest(self):
        self.assertEqual(
            [],
            sc.verify_freeze(FROZEN, {"calibration": CALIBRATION, "holdout": HOLDOUT}),
        )

    def test_no_shipped_fixture_carries_raw_command_text(self):
        documents = [
            sc.load_set(CALIBRATION),
            sc.load_set(HOLDOUT),
            sc._criteria_document(CRITERIA),
        ]
        audit = sc.redaction_audit(
            [], raw_commands=PLAUSIBLE_RAW_TEXT, documents=documents
        )
        self.assertTrue(audit["clean"], audit["leaks"])
        self.assertGreater(audit["documents_scanned"], 0)
        for document in (sc.load_set(CALIBRATION), sc.load_set(HOLDOUT)):
            for row in document["cases"]:
                self.assertRegex(row["command_sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn("command", row)


class CorrelationTests(unittest.TestCase):
    def record(self, decision, outcome, **kwargs):
        digest = sc.hash_command("echo hello")
        return sc.judgment(
            "case-1", "shell_command", decision, 0.9, 3.0, **kwargs
        ), sc.host_decision("case-1", outcome, 7.0, digest, "session-1")

    def test_an_allow_that_the_host_approves_is_an_agreement(self):
        case = sc.correlate(*self.record("allow", "approved"))
        self.assertEqual("agreement", case["kind"])
        self.assertIsNone(case["reason"])

    def test_a_deny_that_the_host_approves_is_a_disagreement_that_names_both(self):
        case = sc.correlate(*self.record("deny", "approved"))
        self.assertEqual("disagreement", case["kind"])
        self.assertEqual("judgment said deny, host did approved", case["detail"])

    def test_a_deferral_cannot_carry_a_comparable_decision(self):
        # Otherwise a deferral could be mapped onto an outcome and counted as an
        # agreement, which is exactly what the acceptance criteria forbid.
        with self.assertRaises(sc.ShadowError) as caught:
            sc.judgment(
                "case-1",
                "shell_command",
                "allow",
                0.9,
                3.0,
                deferral="model_unavailable",
            )
        self.assertIn("E_DEFERRAL_DECISION", str(caught.exception))

    def test_a_deferral_is_reported_as_a_deferral_not_an_agreement(self):
        case = sc.correlate(
            *self.record("abstain", "abstained", deferral="deadline_exceeded")
        )
        self.assertEqual("deferral", case["kind"])
        self.assertEqual("deadline_exceeded", case["reason"])

    def test_a_failure_is_reported_as_a_failure_not_an_agreement(self):
        case = sc.correlate(
            *self.record("abstain", "abstained", failure="component_error")
        )
        self.assertEqual("failure", case["kind"])
        self.assertEqual("component_error", case["reason"])

    def test_an_undeclared_reason_is_refused(self):
        with self.assertRaises(sc.ShadowError) as caught:
            sc.judgment(
                "case-1", "shell_command", "abstain", 0.5, 1.0, deferral="because"
            )
        self.assertIn("E_UNKNOWN_DEFERRAL", str(caught.exception))

    def test_a_record_that_carries_an_undeclared_field_is_refused(self):
        judgment, host = self.record("allow", "approved")
        host["command"] = "echo hello"
        with self.assertRaises(sc.ShadowError) as caught:
            sc.correlate(judgment, host)
        self.assertIn("E_HOST_UNSUPPORTED_FIELD", str(caught.exception))

    def test_mismatched_case_ids_are_refused(self):
        judgment, _ = self.record("allow", "approved")
        wrong = sc.host_decision(
            "case-2", "approved", 7.0, sc.hash_command("echo hello"), "s"
        )
        with self.assertRaises(sc.ShadowError) as caught:
            sc.correlate(judgment, wrong)
        self.assertIn("E_CASE_MISMATCH", str(caught.exception))

    def test_a_raw_command_is_replaced_by_its_digest(self):
        raw = "terraform destroy -auto-approve"
        case = sc.correlate(
            sc.judgment("case-1", "destructive", "deny", 0.99, 4.0),
            sc.host_decision(
                "case-1", "denied", 8.0, sc.hash_command(raw), "session-1"
            ),
        )
        self.assertEqual(
            hashlib.sha256(raw.encode()).hexdigest(), case["command_sha256"]
        )
        audit = sc.redaction_audit([case], raw_commands=[raw])
        self.assertTrue(audit["clean"])
        self.assertEqual(1, audit["raw_commands_checked"])

    def test_the_audit_catches_a_leak_instead_of_reporting_clean(self):
        raw = "terraform destroy -auto-approve"
        case = sc.correlate(
            sc.judgment("case-1", "destructive", "deny", 0.99, 4.0),
            sc.host_decision(
                "case-1", "denied", 8.0, sc.hash_command(raw), "session-1"
            ),
        )
        leaked = dict(case, detail=raw)
        audit = sc.redaction_audit([leaked], raw_commands=[raw])
        self.assertFalse(audit["clean"])
        self.assertEqual(1, audit["raw_commands_present"])

    def test_a_leak_in_an_attached_document_is_caught(self):
        audit = sc.redaction_audit(
            [], raw_commands=["sudo -i"], documents=[{"note": "sudo -i"}]
        )
        self.assertFalse(audit["clean"])
        self.assertEqual(
            [{"document": 1, "command_sha256": sc.hash_command("sudo -i")}],
            audit["leaks"],
        )

    def test_the_report_records_end_to_end_timing_and_says_what_it_is(self):
        report = holdout_report()
        self.assertEqual("ms", report["timings"]["unit"])
        self.assertIn("not a provider measurement", report["timings"]["measured"])
        self.assertEqual(
            report["topline"]["cases"], report["timings"]["component"]["count"]
        )
        self.assertEqual(report["topline"]["cases"], report["timings"]["host"]["count"])
        self.assertIsNotNone(report["timings"]["component"]["p95_ms"])

    def test_the_label_mismatch_rate_divides_by_comparable_cases_only(self):
        # 3 comparable cases, 2 of them labelled differently from the judgment,
        # beside 2 deferrals that must not enter the denominator.
        rows = [
            ("allow", "approved", "deny"),
            ("allow", "approved", "deny"),
            ("deny", "denied", "deny"),
        ]
        cases = [
            sc.correlate(
                sc.judgment(f"case-{index}", "shell_command", decision, 0.9, 1.0),
                sc.host_decision(
                    f"case-{index}", outcome, 1.0, sc.hash_command(f"c{index}"), "s"
                ),
            )
            for index, (decision, outcome, _label) in enumerate(rows, start=1)
        ]
        report = sc.shadow_report(cases)
        report["label_mismatches"] = [
            {"case_id": f"case-{index}"}
            for index, (decision, _outcome, label) in enumerate(rows, start=1)
            if decision != label
        ]
        self.assertEqual(2, len(report["label_mismatches"]))
        self.assertEqual(round(2 / 3, 4), sc.metrics(report)["label_mismatch_rate"])


class ReportCompletenessTests(unittest.TestCase):
    def test_the_shipped_holdout_report_is_consistent(self):
        self.assertEqual([], sc.verify_report(holdout_report()))

    def test_every_deferral_and_failure_is_named_in_the_rendered_report(self):
        report = holdout_report()
        marked = sc.render_markdown(report)
        for kind in ("deferrals", "failures"):
            self.assertTrue(report[kind], kind)
            for case in report[kind]:
                self.assertIn(f"`{case['case_id']}`", marked)

    def test_the_report_states_what_it_did_not_measure(self):
        report = holdout_report()
        self.assertEqual(list(sc.UNMEASURED), report["unmeasured"])
        self.assertIn("safety_in_the_wild", sc.render_markdown(report))

    def test_a_dropped_deferral_is_detected(self):
        report = holdout_report()
        report["deferrals"] = report["deferrals"][:-1]
        self.assertTrue(
            any("deferrals" in problem for problem in sc.verify_report(report))
        )

    def test_a_case_counted_but_not_listed_is_detected(self):
        report = holdout_report()
        report["topline"]["failures"] += 1
        self.assertTrue(
            any("failures" in problem for problem in sc.verify_report(report))
        )

    def test_a_report_that_hides_its_unmeasured_list_is_rejected(self):
        report = holdout_report()
        report["unmeasured"] = []
        self.assertTrue(
            any("unmeasured" in problem for problem in sc.verify_report(report))
        )

    def test_an_unmeasured_speed_or_safety_claim_is_rejected(self):
        report = holdout_report()
        report["summary"] = {
            "latency_saved": 575,
            "nested": {"safety_improvement": "2x"},
        }
        problems = sc.verify_report(report)
        self.assertTrue(any("latency_saved" in problem for problem in problems))
        self.assertTrue(any("safety_improvement" in problem for problem in problems))

    def test_a_report_with_a_dirty_redaction_audit_is_rejected(self):
        report = holdout_report()
        report["redaction"]["clean"] = False
        self.assertTrue(
            any("redaction" in problem for problem in sc.verify_report(report))
        )

    def test_the_cli_verifies_the_shipped_holdout(self):
        result = run_cli("verify", "--set", str(HOLDOUT))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout)["problems"])


class FreezeTests(unittest.TestCase):
    def test_freezing_then_verifying_a_split_is_clean(self):
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "frozen.json"
            document = sc.freeze({"holdout": HOLDOUT}, out)
            self.assertEqual(sc.FREEZE_SCHEMA, document["schema"])
            self.assertEqual([], sc.verify_freeze(out, {"holdout": HOLDOUT}))

    def test_an_edited_split_is_reported_as_drift(self):
        with tempfile.TemporaryDirectory() as work:
            edited = Path(work) / "holdout.json"
            document = load(HOLDOUT)
            document["cases"][0]["host_outcome"] = "approved"
            edited.write_text(json.dumps(document), encoding="utf-8")
            drift = sc.verify_freeze(FROZEN, {"holdout": edited})
            self.assertEqual(1, len(drift))
            self.assertEqual("holdout", drift[0]["split"])
            self.assertNotEqual(drift[0]["expected"], drift[0]["actual"])

    def test_a_split_that_is_not_frozen_at_all_is_drift(self):
        drift = sc.verify_freeze(FROZEN, {"holdout": HOLDOUT, "extra": CALIBRATION})
        self.assertEqual(["extra"], [entry["split"] for entry in drift])

    def test_a_non_freeze_document_is_refused(self):
        with self.assertRaises(sc.ShadowError) as caught:
            sc.verify_freeze(CRITERIA, {"holdout": HOLDOUT})
        self.assertIn("E_FREEZE_SCHEMA", str(caught.exception))

    def test_drift_leaves_enforcement_disabled(self):
        with tempfile.TemporaryDirectory() as work:
            edited = Path(work) / "holdout.json"
            document = load(HOLDOUT)
            document["cases"][0]["label_decision"] = document["cases"][0][
                "judgment_decision"
            ]
            edited.write_text(json.dumps(document), encoding="utf-8")
            result = run_cli(
                "gate",
                "--set",
                str(edited),
                "--criteria",
                str(CRITERIA),
                "--freeze",
                str(FROZEN),
                "--category",
                "shell_command",
                env=satisfied_conditions(),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            state = json.loads(result.stdout)
            self.assertFalse(state["enabled"])
            self.assertIn("evaluation_set_frozen", state["blocked_by"])


class GateTests(unittest.TestCase):
    def test_the_shipped_state_keeps_enforcement_disabled(self):
        result = run_cli(
            "gate",
            "--set",
            str(HOLDOUT),
            "--criteria",
            str(CRITERIA),
            "--freeze",
            str(FROZEN),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        self.assertFalse(state["enabled"])
        self.assertTrue(state["guardian_only"])
        for name in (
            "gate_declared",
            "switch_on",
            "remote_consent_declared",
            "model_available",
            "categories_opted_in",
            "criteria_met",
        ):
            self.assertIn(name, state["blocked_by"])
        self.assertIn("Guardian", state["fallback"])
        self.assertEqual(0, state["exit_code"])

    def test_the_shipped_criteria_are_not_met_on_the_shipped_holdout(self):
        result = run_cli(
            "gate",
            "--set",
            str(HOLDOUT),
            "--criteria",
            str(CRITERIA),
            "--freeze",
            str(FROZEN),
        )
        state = json.loads(result.stdout)
        self.assertFalse(state["criteria"]["met"])
        reasons = {
            entry["name"]: entry["reason"] for entry in state["criteria"]["unmet"]
        }
        self.assertEqual("unmeasured", reasons["availability_probed_on_a_live_model"])
        self.assertEqual("unmeasured", reasons["no_unmeasured_safety_claim"])
        self.assertEqual("requirement_not_met", reasons["holdout_agreement_rate"])

    def test_every_condition_satisfied_turns_enforcement_on(self):
        # The gate must be computed rather than pinned off, or "it stayed
        # disabled" would be evidence of nothing.
        with tempfile.TemporaryDirectory() as work:
            split = Path(work) / "holdout.json"
            split.write_text(
                json.dumps(clean_split(["echo one", "echo two", "echo three"])),
                encoding="utf-8",
            )
            criteria = Path(work) / "criteria.json"
            criteria.write_text(
                json.dumps(
                    {
                        "schema": sc.CRITERIA_SCHEMA,
                        "evaluated_on": "holdout",
                        "declared_by": "test",
                        "criteria": MEASURABLE_CRITERIA,
                    }
                ),
                encoding="utf-8",
            )
            frozen = Path(work) / "frozen.json"
            sc.freeze({"holdout": split}, frozen)
            result = run_cli(
                "gate",
                "--set",
                str(split),
                "--criteria",
                str(criteria),
                "--freeze",
                str(frozen),
                "--category",
                "shell_command",
                env=satisfied_conditions(),
            )
            self.assertEqual(10, result.returncode, result.stderr)
            state = json.loads(result.stdout)
            self.assertTrue(state["enabled"])
            self.assertFalse(state["guardian_only"])
            self.assertEqual([], state["blocked_by"])
            self.assertEqual(10, state["exit_code"])

    def run_leave_one_out(self, drop, *, env=None, categories=("shell_command",)):
        with tempfile.TemporaryDirectory() as work:
            split = Path(work) / "holdout.json"
            split.write_text(
                json.dumps(clean_split(["echo one", "echo two", "echo three"])),
                encoding="utf-8",
            )
            criteria = Path(work) / "criteria.json"
            document = {
                "schema": sc.CRITERIA_SCHEMA,
                "evaluated_on": "holdout",
                "declared_by": "test",
                "criteria": MEASURABLE_CRITERIA,
            }
            criteria.write_text(json.dumps(document), encoding="utf-8")
            frozen = Path(work) / "frozen.json"
            sc.freeze({"holdout": split}, frozen)
            environment = satisfied_conditions()
            if env:
                environment.update(env)
            for name in drop:
                environment.pop(name, None)
            args = [
                "gate",
                "--set",
                str(split),
                "--criteria",
                str(criteria),
                "--freeze",
                str(frozen),
            ]
            for category in categories:
                args += ["--category", category]
            return run_cli(*args, env=environment)

    def test_no_single_input_is_decorative(self):
        cases = {
            "gate_declared": [sc.GATE_SWITCH],
            "switch_on": [sc.ENFORCEMENT_SWITCH],
            "remote_consent_declared": [sc.CONSENT_ENV],
            "model_available": [sc.MODEL_AVAILABLE_ENV],
            "categories_opted_in": [],
        }
        for blocker, drop in cases.items():
            with self.subTest(blocker=blocker):
                result = self.run_leave_one_out(
                    drop,
                    categories=()
                    if blocker == "categories_opted_in"
                    else ("shell_command",),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                state = json.loads(result.stdout)
                self.assertFalse(state["enabled"])
                self.assertIn(blocker, state["blocked_by"])

    def test_the_guardian_only_kill_switch_wins_over_everything_else(self):
        result = self.run_leave_one_out([], env={sc.GUARDIAN_ONLY_SWITCH: "1"})
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        self.assertFalse(state["enabled"])
        self.assertEqual(["guardian_only_kill_switch_off"], state["blocked_by"])
        self.assertTrue(state["guardian_only"])

    def test_an_unmeasured_criterion_is_never_met(self):
        result = sc.evaluate_criteria(
            [
                {
                    "name": "live",
                    "metric": "remote_availability_probes",
                    "op": ">=",
                    "value": 1,
                }
            ],
            {"agreement_rate": 1.0},
        )
        self.assertFalse(result["met"])
        self.assertEqual("unmeasured", result["unmet"][0]["reason"])

    def test_an_unsupported_operator_is_a_refusal_not_a_pass(self):
        result = sc.evaluate_criteria(
            [{"name": "odd", "metric": "agreement_rate", "op": "~=", "value": 1.0}],
            {"agreement_rate": 1.0},
        )
        self.assertFalse(result["met"])
        self.assertEqual("unsupported_operator", result["unmet"][0]["reason"])

    def test_a_criterion_just_missed_is_reported_with_both_numbers(self):
        result = sc.evaluate_criteria(
            [
                {
                    "name": "agreement",
                    "metric": "agreement_rate",
                    "op": ">=",
                    "value": 0.95,
                }
            ],
            {"agreement_rate": 0.9499},
        )
        self.assertFalse(result["met"])
        self.assertEqual(0.95, result["unmet"][0]["required"])
        self.assertEqual(0.9499, result["unmet"][0]["observed"])

    def test_the_calibration_split_may_not_be_evaluated(self):
        result = run_cli(
            "gate",
            "--set",
            str(CALIBRATION),
            "--criteria",
            str(CRITERIA),
            "--freeze",
            str(FROZEN),
            env=satisfied_conditions(),
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("E_HOLDOUT_REQUIRED", result.stderr)

    def test_criteria_declared_for_another_split_are_refused(self):
        with tempfile.TemporaryDirectory() as work:
            criteria = Path(work) / "criteria.json"
            document = sc._criteria_document(CRITERIA)
            document["evaluated_on"] = "calibration"
            criteria.write_text(json.dumps(document), encoding="utf-8")
            result = run_cli(
                "gate",
                "--set",
                str(HOLDOUT),
                "--criteria",
                str(criteria),
                "--freeze",
                str(FROZEN),
                env=satisfied_conditions(),
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("E_HOLDOUT_REQUIRED", result.stderr)

    def test_an_unknown_opted_in_category_is_refused(self):
        with self.assertRaises(sc.ShadowError) as caught:
            sc.enforcement_state(
                satisfied_conditions(),
                criteria=MEASURABLE_CRITERIA,
                observed={"agreement_rate": 1.0},
                set_digest_ok=True,
                opted_in_categories=("not_a_category",),
            )
        self.assertIn("E_UNKNOWN_CATEGORY", str(caught.exception))

    def test_the_gate_reports_its_own_exit_code_in_the_document(self):
        state = sc.enforcement_state(
            satisfied_conditions(),
            criteria=MEASURABLE_CRITERIA,
            observed={
                "agreement_rate": 1.0,
                "deferral_rate": 0.0,
                "failure_rate": 0.0,
                "disagreement_rate": 0.0,
                "label_mismatch_rate": 0.0,
            },
            set_digest_ok=True,
            opted_in_categories=("shell_command",),
        )
        self.assertTrue(state["enabled"])
        self.assertEqual(10, state["exit_code"])


class ManifestConfigurationTests(unittest.TestCase):
    """The manifest must not let a profile reach enforcement without the gate."""

    def manifest(self):
        return load(REPO_ROOT / "jev" / "compatibility-manifest.json")

    def test_the_new_features_are_declared_and_off_by_default(self):
        features = self.manifest()["features"]
        for name in ("approval.shadow_comparison", "approval.enforcement_gate"):
            with self.subTest(feature=name):
                self.assertIn(name, features)
                self.assertFalse(features[name]["default"])
                self.assertEqual(["jev-codex-approval"], features[name]["components"])

    def test_enforcement_requires_the_gate(self):
        features = self.manifest()["features"]
        self.assertIn(
            "approval.enforcement_gate", features["approval.enforcement"]["requires"]
        )

    def test_no_profile_enables_enforcement_without_the_gate(self):
        manifest = self.manifest()
        for path in sorted((REPO_ROOT / "jev" / "profiles").glob("*.json")):
            profile = load(path)
            features = profile.get("features", {})
            if not features.get("approval.enforcement"):
                continue
            with self.subTest(profile=path.name):
                self.assertTrue(features.get("approval.enforcement_gate"))
                self.assertTrue(features.get("approval.shadow_comparison"))
                self.assertTrue(features.get("approval.preflight"))
        self.assertTrue(manifest["features"]["approval.enforcement"]["requires"])


class CliSurfaceTests(unittest.TestCase):
    def test_the_module_is_standard_library_only(self):
        source = MODULE.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                module = line.split()[1].split(".")[0]
                self.assertIn(module, sys.stdlib_module_names, line)

    def test_report_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "report.json"
            marked = Path(work) / "report.md"
            result = run_cli(
                "report",
                "--set",
                str(HOLDOUT),
                "--out",
                str(out),
                "--markdown-out",
                str(marked),
                "--raw-command",
                "rm -rf /",
                "--raw-command",
                "sudo -i",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            report = load(out)
            self.assertEqual([], sc.verify_report(report))
            self.assertEqual(2, report["redaction"]["raw_commands_checked"])
            self.assertTrue(report["redaction"]["clean"])
            self.assertIn(
                "Shadow comparison report", marked.read_text(encoding="utf-8")
            )

    def test_a_raw_command_that_did_reach_a_record_is_reported(self):
        # A negative control for the operator-facing flag: the audit is not a
        # constant "clean".
        result = run_cli(
            "report",
            "--set",
            str(HOLDOUT),
            "--raw-command",
            "judgment said allow, host did denied",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["redaction"]["clean"])
        self.assertEqual(1, report["redaction"]["raw_commands_present"])

    def test_an_unknown_subcommand_is_a_usage_error(self):
        self.assertEqual(2, run_cli("compare").returncode)

    def test_a_refusal_exits_one_and_names_the_error(self):
        result = run_cli("report", "--set", str(CRITERIA))
        self.assertEqual(1, result.returncode)
        self.assertIn("refused:", result.stderr)
        self.assertIn("E_SET_SCHEMA", result.stderr)

    def test_the_shipped_state_is_reproducible_from_the_repository_root(self):
        # The gate reads only the checked-in fixtures, so this is what a
        # reviewer can re-run on a fresh checkout.
        first = holdout_report()
        second = holdout_report()
        self.assertEqual(first, second)
        self.assertEqual([], sc.verify_report(second))


if __name__ == "__main__":
    unittest.main()
