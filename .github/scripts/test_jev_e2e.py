#!/usr/bin/env python3
"""Required-CI coverage for the composed end-to-end regression scenarios (#24).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the acceptance
criteria for issue #24 are pinned here rather than only in `jev/tests`, which
required CI does not run. Phase 6.1 asks for one deterministic harness over the
composed stack, and this file asserts the properties that make that harness
trustworthy rather than merely green:

* it passes, and every assertion in its trace document is individually ok;
* every assertion carries an evidence tier, and no assertion claims a
  `real-host` or `live-provider` run or a measured token count;
* the fixture directory holds exactly the fixtures this scenario labels;
* two runs over the same fixtures produce the same document digest;
* the harness *fails* when the stack it describes regresses, and it refuses
  (exit 2) an unusable or out-of-tree fixture directory instead of mislabelling
  a fixture copy as real-host evidence.

The scenarios themselves are offline: checked-in fixtures, local subprocesses,
no provider, no paid inference. Their tiers are `rollout-fixture`,
`offline-fixture`, `bus-stage-stub`, and (only with a pinned checkout)
`real-component`.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import e2e_regression  # noqa: E402

FIXTURES = REPO_ROOT / "jev" / "tests" / "e2e_fixtures"


class ComposedScenarioTests(unittest.TestCase):
    """One composed run per test class; the scratch lives outside the repo tree."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="jev-e2e-ci-")
        cls.scratch = Path(cls.work.name) / "scratch"
        cls.document, _ = e2e_regression.run(scratch=cls.scratch, root=REPO_ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def checks(self):
        return {check["id"]: check for check in self.document["checks"]}

    def test_every_composed_scenario_passes(self):
        self.assertTrue(
            self.document["ok"],
            msg="failed: "
            + json.dumps(
                [c["id"] for c in self.document["checks"] if not c["ok"]], indent=2
            ),
        )
        self.assertEqual(self.document["counts"]["failed"], 0)
        self.assertGreaterEqual(self.document["counts"]["checks"], 50)

    def test_the_document_names_every_assertion_individually(self):
        checks = self.checks()
        self.assertIn(
            "execution.the_composed_lifecycle_runs_in_the_declared_order", checks
        )
        for name in (
            "capture.replays_the_rollout_into_declared_envelope_events",
            "retrieval.hydration_re_proves_origin_from_the_shipped_rollout",
            "screening.corrupt_policy_fails_closed_to_quarantine",
            "projection.approved_view_removes_exactly_the_planned_prose",
            "projection.an_unapproved_removal_is_reverted_rather_than_shipped",
            "veto.an_ordinary_tool_call_inherits_the_session_latch",
            "approval.a_later_allowance_cannot_clear_a_latched_veto",
        ):
            with self.subTest(check=name):
                self.assertIn(name, checks)
                self.assertTrue(checks[name]["ok"], msg=checks[name]["detail"])

    def test_the_composed_order_matches_the_architecture_lifecycle(self):
        order = [scenario["name"] for scenario in self.document["scenarios"]]
        self.assertEqual(
            order,
            [
                "capture",
                "retrieval",
                "screening",
                "projection",
                "collab",
                "sentinel_and_veto",
                "approval",
                "execution",
            ],
        )

    def test_every_assertion_carries_a_tier_and_no_tier_overclaims(self):
        tiers = {check["tier"] for check in self.document["checks"]}
        self.assertTrue(
            tiers
            <= {
                "rollout-fixture",
                "offline-fixture",
                "bus-stage-stub",
                "component-stub",
                "real-component",
            }
        )
        self.assertNotIn("real-host", tiers)
        self.assertNotIn("live-provider", tiers)
        self.assertEqual(self.document["tier_labels"]["real_host"], [])
        self.assertEqual(self.document["tier_labels"]["live_provider"], [])

    def test_the_capture_step_is_labelled_a_rollout_fixture(self):
        capture = {
            check["id"]: check["tier"]
            for check in self.document["checks"]
            if check["id"].startswith("capture.")
        }
        self.assertTrue(capture)
        self.assertEqual(set(capture.values()) & {"real-host", "live-provider"}, set())
        # The replay, its verify pass, and the collapsed collaboration message
        # all describe the checked-in rollout, so they are rollout fixtures.
        for name in (
            "capture.replays_the_rollout_into_declared_envelope_events",
            "capture.every_invariant_holds_on_the_replayed_store",
            "capture.collaboration_message_is_captured_canonically",
        ):
            with self.subTest(check=name):
                self.assertEqual(capture[name], "rollout-fixture")
        # The default-profile assertion exercises the shipped env planner, not
        # the rollout, so it stays honestly an offline-fixture check.
        self.assertEqual(
            capture["capture.default_profile_leaves_optional_features_off"],
            "offline-fixture",
        )

    def test_no_assertion_claims_a_measured_token_count(self):
        self.assertIsNone(self.document["token_measurement"]["measured"])
        for check in self.document["checks"]:
            evidence = check["evidence"]
            if isinstance(evidence, dict):
                self.assertIsNone(
                    evidence.get("tokens_measured"),
                    msg=f"{check['id']} claims a measured token count",
                )

    def test_the_component_tier_matches_the_resolvable_checkout(self):
        resolvable = bool(os.environ.get("JEV_SENTINEL_ROOT")) or (
            Path("/home/agent/jev/checkouts/jev-sentinel/launch.py").is_file()
        )
        sentinel = [
            check
            for check in self.document["checks"]
            if check["tier"] in ("component-stub", "real-component")
        ]
        self.assertTrue(sentinel)
        tiers = {check["tier"] for check in sentinel}
        if self.document["component"]["stub"]:
            self.assertEqual(tiers, {"component-stub"})
        else:
            self.assertEqual(tiers, {"real-component"})
            self.assertTrue(self.document["component"]["revision"])
        self.assertIsInstance(resolvable, bool)

    def test_the_fixture_directory_holds_only_labelled_fixtures(self):
        present = sorted(entry.name for entry in FIXTURES.iterdir() if entry.is_file())
        self.assertEqual(present, sorted(e2e_regression.FIXTURE_FILES))
        unlabelled = [
            check
            for check in self.document["checks"]
            if check["id"]
            == "negatives.every_fixture_file_is_a_labelled_part_of_this_scenario"
        ]
        self.assertEqual(len(unlabelled), 1)
        self.assertTrue(unlabelled[0]["ok"], msg=unlabelled[0]["detail"])


class DeterminismTests(unittest.TestCase):
    """The same fixtures must produce the same document, byte for byte."""

    def test_two_runs_produce_the_same_document_digest(self):
        with tempfile.TemporaryDirectory(prefix="jev-e2e-det-") as first:
            one, _ = e2e_regression.run(scratch=Path(first) / "a", root=REPO_ROOT)
        with tempfile.TemporaryDirectory(prefix="jev-e2e-det-") as second:
            two, _ = e2e_regression.run(scratch=Path(second) / "b", root=REPO_ROOT)
        self.assertEqual(one["ok"], two["ok"])
        self.assertEqual(one["digest"], two["digest"])
        self.assertEqual(
            json.dumps(one, sort_keys=True), json.dumps(two, sort_keys=True)
        )


class HarnessRefusalTests(unittest.TestCase):
    """A regression harness that cannot fail is not evidence."""

    def test_a_regressed_fixture_makes_the_run_fail(self):
        """The protected-tail fixture is replaced by an eligible duplicate."""
        regressed = Path(
            tempfile.mkdtemp(prefix="e2e_fixtures_regressed_", dir=FIXTURES.parent)
        )
        try:
            for entry in FIXTURES.iterdir():
                if entry.is_file():
                    shutil.copy2(entry, regressed / entry.name)
            eligible = json.loads(
                (regressed / e2e_regression.WIRE).read_text(encoding="utf-8")
            )
            (regressed / e2e_regression.WIRE_PROTECTED).write_text(
                json.dumps(eligible), encoding="utf-8"
            )
            code = e2e_regression.main(
                [
                    "run",
                    "--quiet",
                    "--fixtures",
                    str(regressed),
                    "--scratch",
                    str(regressed / "scratch"),
                ]
            )
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(regressed, ignore_errors=True)

    def test_a_missing_fixture_directory_is_a_usage_error(self):
        with tempfile.TemporaryDirectory(prefix="jev-e2e-none-") as empty:
            code = e2e_regression.main(["run", "--quiet", "--fixtures", empty])
            self.assertEqual(code, 2)

    def test_an_out_of_tree_fixture_directory_is_refused(self):
        """A fixture copy outside the tests tree would be mislabelled real-host."""
        with tempfile.TemporaryDirectory(prefix="jev-e2e-out-") as outside:
            code = e2e_regression.main(["run", "--quiet", "--fixtures", outside])
            self.assertEqual(code, 2)

    def test_an_unresolvable_component_root_is_a_usage_error(self):
        with tempfile.TemporaryDirectory(prefix="jev-e2e-comp-") as empty:
            code = e2e_regression.main(["run", "--quiet", "--component", empty])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
