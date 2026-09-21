#!/usr/bin/env python3
"""Required-CI coverage for the phase-6 combined validation record (#8).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the roll-up claims
are pinned here rather than only in `jev/tests`. The properties that make the
record trustworthy - rather than merely long - are that every lifecycle stage is
present with a passing assertion, that the authority invariants are resolved
against named assertions instead of prose, that the tier census keeps
`live-provider` at `not-run`, and that an overclaimed tier, a missing stage, or a
missing authority assertion refuses the record instead of being summarized away.

Everything here is offline: the composed regression, the performance comparison,
and the release gates all run against checked-in fixtures and shipped modules in
a scratch directory outside the repository. No provider is contacted.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import stack_validation


def _check(ident, *, ok=True, tier=stack_validation.TIER_OFFLINE):
    return {"id": ident, "ok": ok, "tier": tier, "evidence": "observed", "detail": ""}


class ComposedRecordTests(unittest.TestCase):
    """One composed run for the class; the run is hermetic and offline."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="jev-stack-test-"))
        cls.document = stack_validation.compose(
            REPO_ROOT, scratch=cls.scratch / "first", repetitions=1
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_every_stage_is_present_and_passing(self):
        stages = self.document["stages"]
        self.assertEqual(
            [row["stage"] for row in stages],
            [name for name, _ in stack_validation.REQUIRED_STAGES],
        )
        for row in stages:
            self.assertEqual(row["status"], "pass", row)
            self.assertTrue(row["assertions"])
            self.assertEqual(row["failed"], 0)
            self.assertEqual(row["passed"], len(row["assertions"]))

    def test_authority_invariants_resolve_to_named_assertions(self):
        authority = self.document["authority"]
        self.assertEqual(
            set(authority),
            {ident for ident, *_ in stack_validation.AUTHORITY_ASSERTIONS},
        )
        for ident, row in authority.items():
            self.assertEqual(row["status"], "pass", ident)
            self.assertTrue(row["assertions"], ident)
        for ident, _, all_of, _ in stack_validation.AUTHORITY_ASSERTIONS:
            self.assertTrue(set(all_of) <= set(authority[ident]["assertions"]), ident)

    def test_tier_census_keeps_the_live_provider_row_unrun(self):
        tiers = self.document["tiers"]
        self.assertEqual(tiers["live_provider"], "not-run")
        self.assertNotIn(stack_validation.TIER_LIVE, tiers["counts"])
        self.assertTrue(tiers["counts"].get(stack_validation.TIER_OFFLINE))
        self.assertTrue(tiers["recorded_real_host_runs"])
        self.assertEqual(
            set(tiers["labels"]),
            {"offline", "real_component", "real_host", "live_provider"},
        )

    def test_the_record_composes_without_refusal(self):
        self.assertEqual(self.document["refusals"], [])
        self.assertTrue(self.document["e2e"]["ok"])
        self.assertEqual(self.document["e2e"]["counts"]["failed"], 0)
        self.assertTrue(self.document["perf"]["ok"])
        self.assertEqual(
            self.document["perf"]["summary"]["service_usage"]["provider_requests"], 0
        )

    def test_release_readiness_is_reported_not_assumed(self):
        gates = self.document["gates"]
        self.assertEqual(self.document["release_ready"], gates["release_ready"])
        blockers = self.document["release_blockers"]
        self.assertEqual(blockers, sorted(blockers))
        # A blocker is either a gate that is not run or a platform with no
        # current run, so the gate list is always a subset and the platform
        # names come from the platform rows.
        self.assertTrue(set(gates["not_run"]) <= set(blockers))
        platforms = {row["platform"] for row in gates["platforms"]}
        self.assertTrue(set(blockers) <= set(gates["not_run"]) | platforms)
        for gate in gates["gates"]:
            self.assertIn(gate["evidence"], ("verified-here", "recorded"))
        self.assertTrue(self.document["isolated_roundtrip"])

    def test_the_record_is_deterministic_across_runs(self):
        second = stack_validation.compose(
            REPO_ROOT, scratch=self.scratch / "second", repetitions=1
        )
        self.assertEqual(self.document["digest"], second["digest"])
        self.assertEqual(self.document["e2e"]["digest"], second["e2e"]["digest"])
        self.assertEqual(self.document["perf"]["digest"], second["perf"]["digest"])

    def test_combined_readiness_does_not_depend_on_a_closed_issue(self):
        """The gates answer from evidence; a claimed pass is rewritten to fail."""
        claimed = [
            {
                "id": "issue.closed",
                "requirement": "the tracking issue is closed",
                "status": "pass",
                "evidence": "claimed",
                "tier": None,
                "detail": "#8 is closed",
            }
        ]
        outcome = stack_validation.release_readiness.summarize_gates(claimed)
        self.assertEqual(outcome["gates"][0]["status"], "fail")
        self.assertFalse(outcome["release_ready"])


class RefusalTests(unittest.TestCase):
    """The negative controls: each of these must refuse, not summarize."""

    def test_a_missing_stage_is_refused(self):
        refusals = []
        rows = stack_validation.compose_stages(
            {"checks": [_check("capture.replays_nothing_important")]}, refusals
        )
        missing = [row for row in rows if row["stage"] == "execution"]
        self.assertEqual(missing[0]["status"], "fail")
        self.assertIn("E_STACK_STAGE_MISSING", [r["code"] for r in refusals])

    def test_a_failing_stage_assertion_is_refused(self):
        refusals = []
        rows = stack_validation.compose_stages(
            {
                "checks": [
                    _check("capture.ok"),
                    _check("capture.broken", ok=False),
                ]
            },
            refusals,
        )
        row = next(row for row in rows if row["stage"] == "capture")
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["failed"], 1)
        self.assertIn("E_STACK_STAGE_FAILED", [r["code"] for r in refusals])

    def test_a_missing_authority_assertion_is_refused(self):
        refusals = []
        stack_validation.compose_authority({"checks": []}, refusals)
        codes = {r["code"] for r in refusals}
        self.assertEqual(codes, {"E_STACK_AUTHORITY_MISSING"})

    def test_a_failing_authority_assertion_is_refused(self):
        identifier = stack_validation.AUTHORITY_ASSERTIONS[1][2][0]
        refusals = []
        outcome = stack_validation.compose_authority(
            {"checks": [_check(identifier, ok=False)]}, refusals
        )
        self.assertEqual(outcome["veto_not_cleared_by_approval"]["status"], "fail")
        self.assertIn("E_STACK_AUTHORITY_FAILED", [r["code"] for r in refusals])

    def test_an_overclaimed_live_provider_tier_is_refused(self):
        refusals = []
        documents = {
            "repo_root": str(REPO_ROOT),
            "e2e": {
                "checks": [_check("capture.live", tier=stack_validation.TIER_LIVE)]
            },
            "perf": {"cases": [{"tier": stack_validation.TIER_OFFLINE}]},
            "gates": {"gates": []},
        }
        census = stack_validation.census_tiers(documents, refusals)
        self.assertIn("E_STACK_TIER_OVERCLAIM", [r["code"] for r in refusals])
        self.assertEqual(census["live_provider"], "not-run")

    def test_an_empty_run_cannot_distinguish_offline_from_real_host(self):
        refusals = []
        documents = {
            "repo_root": str(Path(tempfile.gettempdir()) / "jev-stack-no-evidence"),
            "e2e": {"checks": []},
            "perf": {"cases": []},
            "gates": {"gates": []},
        }
        stack_validation.census_tiers(documents, refusals)
        self.assertIn("E_STACK_REAL_HOST_UNREPRESENTED", [r["code"] for r in refusals])


if __name__ == "__main__":
    unittest.main()
