#!/usr/bin/env python3
"""Required-CI gate for the approval shadow comparison and enforcement gate (#23).

These checks run from ``just test-github-scripts`` alongside the other
repository-level Python checks. They pin the contract CONTRACTS.md C6 asks for:
a shadow report correlates typed judgments with the host's final decisions and
end-to-end timing without carrying raw commands or secrets, the report accounts
for every failure and deferral, and the gate reads readiness without ever
flipping the enforcement switch.

The evidence tier here is ``offline-fixture``: the streams are the checked-in
illustrative evaluation set under ``jev/tests/approval_fixtures`` and the
criteria are read from the shipped manifest. No live provider, model or
component run is exercised, so these tests certify bookkeeping and refusal
behaviour, not accuracy.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
JEV_ROOT = REPO_ROOT / "jev"
FIXTURES = JEV_ROOT / "tests" / "approval_fixtures"
SHADOW_SCRIPT = JEV_ROOT / "scripts" / "approval_shadow.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shadow = _load("approval_shadow", SHADOW_SCRIPT)
jev_manifest = _load("jev_manifest", JEV_ROOT / "scripts" / "jev_manifest.py")


def load_manifest():
    return json.loads(
        (JEV_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8")
    )


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SHADOW_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class ShippedFixtureTests(unittest.TestCase):
    def test_checked_in_streams_correlate_by_review_id(self):
        report = shadow.correlate(
            fixture("jev-audit.jsonl"),
            fixture("guardian.jsonl"),
            fixture("labels.jsonl"),
        )
        self.assertEqual(report["report_schema"], "jev-approval.shadow.v1")
        self.assertEqual(report["pairing"]["paired"], 6)
        self.assertEqual(report["pairing"]["only_jev"], [])
        self.assertEqual(report["pairing"]["only_guardian"], [])

    def test_report_never_claims_a_speedup_or_a_ground_truth(self):
        report = shadow.correlate(fixture("jev-audit.jsonl"), fixture("guardian.jsonl"))
        self.assertFalse(report["speedup_claimed"])
        self.assertFalse(report["guardian_is_ground_truth"])
        # Latency is only reported from observed elapsed_ms values.
        self.assertEqual(report["latency_ms"]["source"], "observed")
        self.assertIsNotNone(report["latency_ms"]["p95"])

    def test_report_accounts_for_every_attempt_including_deferrals(self):
        report = shadow.correlate(
            fixture("jev-audit.jsonl"),
            fixture("guardian.jsonl"),
            fixture("labels.jsonl"),
        )
        self.assertTrue(report["fallbacks"]["all_attempts_categorized"])
        self.assertEqual(report["fallbacks"]["total_attempts"], 6)
        # Five deferrals plus one typed error are retained, not dropped.
        self.assertEqual(report["fallbacks"]["jev_deferrals"], 5)
        self.assertEqual(report["fallbacks"]["jev_failures"], 1)

    def test_false_allow_survives_into_the_report(self):
        report = shadow.correlate(
            fixture("jev-audit.jsonl"),
            fixture("guardian.jsonl"),
            fixture("labels.jsonl"),
        )
        labels = report["independent_labels"]
        self.assertEqual(labels["paired_labeled"], 6)
        self.assertEqual(labels["false_allow_count"], 1)
        self.assertIsNotNone(labels["allow_error_rate"])


class RawContentRefusalTests(unittest.TestCase):
    def test_module_refuses_raw_command_fields(self):
        stream = (
            '{"request_id": "rev-1", "candidate": "allow", "decision": "defer",'
            ' "command": "rm -rf /"}\n'
        )
        with self.assertRaises(shadow.ShadowError) as caught:
            shadow.correlate(stream, fixture("guardian.jsonl"))
        self.assertEqual(caught.exception.code, shadow.E_SHADOW_RAW_FIELD)

    def test_cli_refuses_a_stream_carrying_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.jsonl"
            raw.write_text(
                '{"request_id": "rev-1", "candidate": "allow", "decision": "defer",'
                ' "environment": {"API_KEY": "x"}}\n',
                encoding="utf-8",
            )
            result = run_cli(
                "correlate",
                "--jev",
                str(raw),
                "--guardian",
                str(FIXTURES / "guardian.jsonl"),
                "--out",
                "-",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raw", result.stderr.lower())


class EnforcementGateTests(unittest.TestCase):
    def test_manifest_declares_the_gated_switches(self):
        features = load_manifest()["features"]
        self.assertIs(features["approval.preflight"]["default"], False)
        self.assertIs(features["approval.enforcement"]["default"], False)
        self.assertIn(
            "approval.preflight", features["approval.enforcement"]["requires"]
        )

    def test_disabled_state_check_passes(self):
        self.assertEqual(
            jev_manifest.check_approval_enforcement(load_manifest(), "disabled"), []
        )

    def test_the_fixture_is_not_permitted_and_the_gate_reports_a_switch(self):
        report = shadow.correlate(
            fixture("jev-audit.jsonl"),
            fixture("guardian.jsonl"),
            fixture("labels.jsonl"),
        )
        verdict = shadow.gate(report, shadow.declared_criteria(load_manifest()))
        self.assertFalse(verdict["permitted"])
        self.assertFalse(verdict["enforcement_enabled"])
        self.assertEqual(verdict["enforcement_switch"], "approval.enforcement")
        self.assertIn("insufficient_labeled_pairs", verdict["reasons"])

    def test_cli_gate_exits_nonzero_while_enforcement_stays_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            produced = run_cli(
                "correlate",
                "--jev",
                str(FIXTURES / "jev-audit.jsonl"),
                "--guardian",
                str(FIXTURES / "guardian.jsonl"),
                "--labels",
                str(FIXTURES / "labels.jsonl"),
                "--out",
                str(report),
            )
            self.assertEqual(produced.returncode, 0, produced.stderr)
            gated = run_cli("gate", "--report", str(report))
        self.assertEqual(gated.returncode, 1)
        verdict = json.loads(gated.stdout)
        self.assertFalse(verdict["enforcement_enabled"])

    def test_criteria_cli_reports_the_declared_criteria_and_switch_state(self):
        result = run_cli("criteria")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state["criteria"]["min_labeled_pairs"], 200)
        self.assertEqual(
            state["opt_in_action_categories"], ["exec_command", "apply_patch"]
        )
        self.assertEqual(
            state["switch_state"]["enforce_switch"], "approval.enforcement"
        )
        self.assertFalse(state["switch_state"]["enforcement_active"])

    def test_enforcement_eval_profile_still_reports_the_gate_closed(self):
        manifest = load_manifest()
        profile = json.loads(
            (JEV_ROOT / "profiles" / "enforcement-eval.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(jev_manifest.validate_manifest(manifest, profile=profile), [])
        # A profile may enable the feature for a controlled evaluation, but the
        # recorded default the gate reads stays false.
        self.assertIs(manifest["features"]["approval.enforcement"]["default"], False)


if __name__ == "__main__":
    unittest.main()
