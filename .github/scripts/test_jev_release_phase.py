#!/usr/bin/env python3
"""Phase-level composed claim for phase 6: validating and releasing the stack (#8).

Phase 6's sub-issues each ship their own tests and their own tier:

* ``#24`` the composed end-to-end regression harness (``test_jev_e2e.py``);
* ``#25`` the offline / real-host / live coverage record (``test_jev_validation.py``);
* ``#26`` the release gates and the candidate artifact (``test_jev_release.py``).

What none of them asserts is that the three *compose into one release verdict*.
That is what this module checks, against #8's acceptance criteria:

1. offline CI passes with a correlated trace demonstrating all stages and
   preserved authority, with fixtures distinguished from live evidence;
2. reproducible evidence distinguishes offline, real-host, and live-provider
   coverage, and no live test runs implicitly or in an existing user session;
3. a fresh isolated setup can reproduce the validated configuration and roll
   back, and release readiness follows actual evidence rather than issue
   completion alone.

The claim is deliberately not "the gates are green". At the revision under test
``release_ready`` may be **false**, and the tests below assert *why* it is false
and that the false verdict is the evidence-driven one: a phase test that could
only pass when every gate is green would be satisfied by closing issues, which
is exactly the failure mode criterion 3 names.

Criterion 1's trace is produced by the same required-CI lane as this file
(``repo-checks`` discovers ``.github/scripts/test_jev_*.py``), so the composition
is proven where required CI looks, not only in ``jev/tests``.

Everything here is offline: local subprocesses and checked-in files, no
provider, no paid inference. The one recorded input it reads - the platform
matrix - is labelled ``real-host-binary`` and is never presented as live
evidence.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import build_provenance  # noqa: E402
import e2e_regression  # noqa: E402
import release_readiness  # noqa: E402

VALIDATION_DOC = REPO_ROOT / "jev" / "VALIDATION.md"
PLATFORM_EVIDENCE = REPO_ROOT / "jev" / "evidence" / "platform-matrix.json"

#: The tier vocabulary the phase's three artifacts draw from. A tier outside
#: this set would be an undeclared claim about where evidence came from.
TIER_VOCABULARY = {
    "rollout-fixture",
    "offline-fixture",
    "bus-stage-stub",
    "component-stub",
    "real-component",
    "real-host-binary",
    "live-provider",
}

#: The composed lifecycle every stage name in the trace must follow.
LIFECYCLE = [
    "capture",
    "retrieval",
    "screening",
    "projection",
    "collab",
    "sentinel_and_veto",
    "approval",
    "execution",
]


def gate_from(gates, gate_id):
    for gate in gates:
        if gate["id"] == gate_id:
            return gate
    raise AssertionError(f"gate {gate_id!r} is absent from {[g['id'] for g in gates]}")


class ReleasePhaseClaimTests(unittest.TestCase):
    """One composed evaluation and one composed trace, shared by the class."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="jev-release-phase-")
        scratch = Path(cls.work.name) / "scratch"
        cls.document = release_readiness.evaluate_gates(REPO_ROOT, scratch=scratch)
        cls.trace, _ = e2e_regression.run(
            scratch=Path(cls.work.name) / "trace", root=REPO_ROOT
        )

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def gates(self):
        return {gate["id"]: gate for gate in self.document["gates"]}

    # -- criterion 3: the verdict is about this revision, and derived ------

    def test_the_verdict_is_bound_to_this_checkout(self):
        revision = build_provenance.git_revision(REPO_ROOT)
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertEqual(self.document["revision"], revision)
        self.assertEqual(self.document["schema"], release_readiness.SCHEMA)

    def test_every_gate_uses_the_declared_vocabulary(self):
        for gate in self.document["gates"]:
            with self.subTest(gate=gate["id"]):
                self.assertIn(gate["status"], release_readiness.GATE_STATUSES)
                self.assertIn(gate["evidence"], release_readiness.GATE_EVIDENCE)
                self.assertTrue(gate["detail"], msg="a gate must say what it saw")

    def test_an_asserted_gate_is_never_a_pass(self):
        synthetic = release_readiness.summarize_gates(
            [
                {
                    "id": "asserted",
                    "requirement": "proven by saying so",
                    "status": "pass",
                    "evidence": "claimed",
                    "tier": "offline-fixture",
                    "detail": "a closed issue is not evidence",
                },
                {
                    "id": "recorded",
                    "requirement": "proven by a run",
                    "status": "pass",
                    "evidence": "recorded",
                    "tier": "real-host-binary",
                    "detail": "a checked-in run record",
                },
            ]
        )
        statuses = {gate["id"]: gate["status"] for gate in synthetic["gates"]}
        self.assertEqual(statuses["asserted"], "fail")
        self.assertEqual(statuses["recorded"], "pass")
        self.assertFalse(synthetic["release_ready"])
        self.assertIn("asserted", synthetic["failed"])

    def test_readiness_is_the_exact_conjunction_of_the_gates(self):
        failed = [g["id"] for g in self.document["gates"] if g["status"] == "fail"]
        not_run = [g["id"] for g in self.document["gates"] if g["status"] == "not-run"]
        self.assertEqual(self.document["failed"], failed)
        self.assertEqual(self.document["not_run"], not_run)
        self.assertEqual(self.document["release_ready"], not failed and not not_run)

    def test_readiness_would_hold_if_every_gate_carried_evidence(self):
        synthetic = release_readiness.summarize_gates(
            [
                {
                    "id": f"gate.{index}",
                    "requirement": "proven here",
                    "status": "pass",
                    "evidence": "verified-here",
                    "tier": "offline-fixture",
                    "detail": "derived from this checkout",
                }
                for index in range(3)
            ]
        )
        self.assertTrue(synthetic["release_ready"])
        self.assertEqual(synthetic["failed"], [])
        self.assertEqual(synthetic["not_run"], [])

    def test_an_unready_verdict_always_names_what_is_missing(self):
        if self.document["release_ready"]:
            self.assertEqual(self.document["blocking"], [])
            return
        self.assertTrue(
            self.document["blocking"],
            msg="an unready verdict must name the missing evidence",
        )
        for gate_id in self.document["failed"] + self.document["not_run"]:
            with self.subTest(gate=gate_id):
                self.assertIn(gate_id, self.document["blocking"])

    # -- criterion 3: a fresh isolated setup reproduces and rolls back -----

    def test_a_fresh_isolated_setup_reproduces_and_rolls_back(self):
        gate = gate_from(self.document["gates"], "isolated.roundtrip")
        self.assertEqual(gate["status"], "pass", msg=gate["detail"])
        self.assertEqual(gate["evidence"], "verified-here")
        roundtrip = self.document["isolated_roundtrip"]
        self.assertIsNotNone(roundtrip, msg="the round trip must be recorded")
        runs = roundtrip["runs"]
        self.assertEqual(len(runs), 2, msg="two fresh environments, not one reused")
        for run in runs:
            with self.subTest(env=run["env_dir"]):
                self.assertEqual(run["state"], "moved")
                self.assertTrue(run["source_absent"])
                self.assertTrue(run["plan_preserved"])
                self.assertTrue(run["binary_matched_plan"])
                self.assertFalse(run["optional_features_enabled"])
                self.assertFalse(run["remote_inference_enabled"])
        # "Reproducible" is the gate's own claim that both setups resolved to
        # the same plan; here the plan must also bind to a declared profile by
        # digest, so a passing gate is about this configuration and not a stub.
        digests = roundtrip["profile_digests"]
        self.assertTrue(digests, msg="the validated configuration is a profile set")
        profile = roundtrip["plan"]["profile"]
        self.assertIn(profile, digests)
        self.assertEqual(digests[profile], roundtrip["plan"]["profile_digest"])
        self.assertTrue(roundtrip["plan"]["host_commit"])

    def test_the_roundtrip_is_never_credited_unless_it_ran(self):
        # Skipping the round trip must not credit it. NOTE: today the skipped
        # gate is *absent* rather than `not-run`, which is the weaker property -
        # not credited, but also not blocking. That gap is filed as issue #98
        # and is stated in evidence/release-phase-claim.md; it is not asserted
        # here as if it were the intended shape.
        skipped = release_readiness.evaluate_gates(REPO_ROOT, run_roundtrip=False)
        ids = {gate["id"] for gate in skipped["gates"]}
        self.assertNotIn("isolated.roundtrip", ids)
        self.assertIsNone(skipped["isolated_roundtrip"])
        for gate in skipped["gates"]:
            with self.subTest(gate=gate["id"]):
                self.assertNotEqual(gate.get("tier"), "live-provider")

    # -- criterion 2: the tiers are distinguished, and live is never claimed

    def test_no_gate_claims_live_provider_coverage(self):
        tiers = {gate.get("tier") for gate in self.document["gates"]}
        self.assertTrue(tiers <= TIER_VOCABULARY, msg=f"undeclared tier in {tiers}")
        self.assertNotIn("live-provider", tiers)
        self.assertEqual(
            gate_from(self.document["gates"], "platform.matrix")["tier"],
            "real-host-binary",
        )
        self.assertEqual(
            gate_from(self.document["gates"], "candidate.exclusions")["tier"],
            "offline-fixture",
        )

    def test_the_validation_record_leaves_the_live_tier_not_run(self):
        row = None
        for line in VALIDATION_DOC.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and "live provider" in cells[0].lower():
                row = cells
                break
        self.assertIsNotNone(row, msg="VALIDATION.md must declare a live tier")
        self.assertIn("not run", row[1].lower())

    def test_the_recorded_platform_evidence_is_labelled_real_host(self):
        evidence = json.loads(PLATFORM_EVIDENCE.read_text(encoding="utf-8"))
        self.assertTrue(evidence["platforms"], msg="a record must exist to be read")
        for name, record in sorted(evidence["platforms"].items()):
            with self.subTest(platform=name):
                self.assertTrue(record.get("revision"))
                self.assertTrue(record.get("binary_sha256"))
                for harness, result in sorted(record["harness"].items()):
                    self.assertEqual(result["tier"], "real-host-binary")
                    self.assertNotEqual(result["tier"], "live-provider")

    def test_the_composed_trace_covers_every_stage_in_order(self):
        self.assertTrue(
            self.trace["ok"],
            msg="failed: "
            + json.dumps(
                [c["id"] for c in self.trace["checks"] if not c["ok"]], indent=2
            ),
        )
        self.assertEqual(self.trace["counts"]["failed"], 0)
        self.assertEqual(
            [scenario["name"] for scenario in self.trace["scenarios"]], LIFECYCLE
        )
        self.assertEqual(self.trace["tier_labels"]["real_host"], [])
        self.assertEqual(self.trace["tier_labels"]["live_provider"], [])

    def test_the_composed_trace_and_the_gates_share_one_tier_vocabulary(self):
        tiers = {check["tier"] for check in self.trace["checks"]}
        self.assertTrue(tiers <= TIER_VOCABULARY, msg=f"undeclared tier in {tiers}")
        self.assertNotIn("live-provider", tiers)

    # -- criterion 1: required CI runs the whole composition ---------------

    def test_the_required_ci_lane_discovers_the_whole_composition(self):
        discovered = {
            path.name
            for path in (REPO_ROOT / ".github" / "scripts").glob("test_jev_*.py")
        }
        for name in (
            "test_jev_e2e.py",
            "test_jev_validation.py",
            "test_jev_release.py",
            "test_jev_release_phase.py",
        ):
            with self.subTest(module=name):
                self.assertIn(name, discovered)


if __name__ == "__main__":
    unittest.main()
