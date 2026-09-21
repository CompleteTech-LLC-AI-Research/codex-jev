#!/usr/bin/env python3
"""Required-CI coverage for the composed phase-6 release validation (#8).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the phase 6/6
acceptance criteria are pinned here as well as in the harness's own document.
Phase 6/6 asks for a correlated offline trace across every stage with the
authority boundaries preserved, evidence that keeps offline, real-host and
live-provider coverage distinct, and a fresh isolated setup that reproduces and
rolls back - with release readiness following evidence rather than issue
completion.

The properties that make that trustworthy rather than merely green are that the
stage partition loses nothing, that the authority invariants are named and hold,
that a live-provider claim is refused and never implied, and that the isolated
round trip actually moves and restores state. Everything here is offline:
checked-in fixtures, shipped modules, and scratch outside the repository. No
provider is contacted and no paid inference is used.
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import e2e_regression  # noqa: E402
import perf_validation  # noqa: E402
import phase6_release  # noqa: E402
import release_readiness  # noqa: E402


class ComposedReleaseTests(unittest.TestCase):
    """One composed run for the class; the scratch lives outside the repo tree."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="jev-phase6-ci-")
        scratch = Path(cls.work.name)
        cls.document = phase6_release.compose(REPO_ROOT, scratch=scratch / "scratch")
        e2e, _ = e2e_regression.run(scratch=scratch / "inputs-e2e", root=REPO_ROOT)
        cls.inputs = (
            e2e,
            perf_validation.measure(repetitions=2),
            release_readiness.evaluate_gates(
                REPO_ROOT, scratch=scratch / "inputs-gates"
            ),
        )

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_the_composed_validation_holds(self):
        self.assertTrue(self.document["phase_validated"])
        self.assertEqual(
            self.document["criteria"],
            {
                "offline_correlated_trace": True,
                "tier_evidence_distinct": True,
                "reproducible_and_rollback": True,
                "readiness_tracks_evidence": True,
            },
        )

    def test_every_stage_is_present_in_order_and_passes(self):
        stages = self.document["trace"]["stages"]
        self.assertEqual(
            [stage["stage"] for stage in stages], list(phase6_release.STAGE_ORDER)
        )
        for stage in stages:
            with self.subTest(stage=stage["stage"]):
                self.assertGreater(stage["assertions"], 0)
                self.assertTrue(stage["ok"], msg=stage["failed"])
        self.assertEqual(
            self.document["trace"]["order"], list(phase6_release.STAGE_ORDER)
        )
        self.assertEqual(self.document["trace"]["counts"]["failed"], 0)

    def test_the_stage_partition_loses_no_assertion(self):
        trace = self.document["trace"]
        self.assertEqual(trace["unassigned"], [])
        accounted = (
            sum(stage["assertions"] for stage in trace["stages"])
            + trace["cross_cutting"]["assertions"]
        )
        self.assertEqual(accounted, trace["counts"]["checks"])
        self.assertTrue(trace["cross_cutting"]["ok"])

    def test_the_authority_invariants_are_named_and_hold(self):
        authority = {
            entry["invariant"]: entry for entry in self.document["trace"]["authority"]
        }
        self.assertEqual(set(authority), set(phase6_release.AUTHORITY_INVARIANTS))
        for invariant, entry in authority.items():
            with self.subTest(invariant=invariant):
                self.assertTrue(entry["present"])
                self.assertTrue(entry["ok"], msg=entry["check"])

    def test_no_tier_implies_a_host_or_provider_run(self):
        tiers = self.document["tiers"]
        self.assertTrue(tiers["ok"], msg=tiers["overclaims"])
        self.assertEqual(tiers["overclaims"], [])
        self.assertEqual(tiers["coverage"]["live_provider"]["status"], "not-run")
        self.assertEqual(tiers["provider_requests"], 0)
        self.assertEqual(tiers["budget_spent_usd"], 0.0)

    def test_a_live_provider_claim_is_refused(self):
        # A run that labels a live-provider tier, contacts a provider, or spends
        # budget must fail the tier review instead of being reported as coverage.
        def claim_live_tier(e2e, perf, gates):
            perf["tier_labels"]["live_provider"] = ["invented"]

        def contact_a_provider(e2e, perf, gates):
            perf["summary"]["service_usage"]["provider_requests"] = 1

        def record_a_live_platform(e2e, perf, gates):
            gates["platforms"][0]["status"] = "fail"
            gates["platforms"][0]["reason"] = (
                "claims a live-provider tier or records no harness result"
            )

        for mutate in (claim_live_tier, contact_a_provider, record_a_live_platform):
            with self.subTest(mutation=mutate):
                e2e, perf, gates = copy.deepcopy(self.inputs)
                mutate(e2e, perf, gates)
                review = phase6_release.review_tiers(e2e, perf, gates)
                self.assertFalse(review["ok"])
                self.assertTrue(review["overclaims"])

    def test_the_isolated_setup_reproduces_and_rolls_back(self):
        readiness = self.document["readiness"]
        self.assertTrue(readiness["reproduced_and_rolled_back"])
        self.assertEqual(len(readiness["failed"]), 0)

    def test_readiness_mirrors_the_gate_evidence(self):
        readiness = self.document["readiness"]
        self.assertTrue(readiness["tracks_evidence"])
        self.assertTrue(readiness["gates_typed"])
        self.assertTrue(readiness["passes_are_evidence"])
        self.assertEqual(
            readiness["release_ready"],
            not readiness["failed"] and not readiness["not_run"],
        )

    def test_validation_is_reported_separately_from_release_readiness(self):
        # This host can validate the lifecycle while release readiness stays
        # false because a supported platform has no run - so the document must
        # expose both, and `phase_validated` must not be a restatement of
        # `release_ready`.
        self.assertEqual(
            self.document["phase_validated"],
            all(self.document["criteria"].values()),
        )
        self.assertIn("release_ready", self.document)
        self.assertIsInstance(self.document["release_ready"], bool)
        self.assertEqual(
            self.document["blocking"], self.document["readiness"]["blocking"]
        )


if __name__ == "__main__":
    unittest.main()
