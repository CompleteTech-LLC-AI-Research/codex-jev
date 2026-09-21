#!/usr/bin/env python3
"""Required-CI coverage for the phase-6.3 packaging workflow (#26).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the release-gate and
candidate-artifact claims are pinned here rather than only in `jev/tests`. The
properties that make the workflow trustworthy - rather than merely green - are
that a claim can never pass a gate, that a missing platform is reported instead
of assumed, that a credential-shaped file refuses the candidate, and that the
artifact is byte-identical across builds while carrying no credential bytes and
no isolated-environment state.

Everything here is offline: checked-in inputs, shipped modules, and a scratch
directory outside the repository. No provider is contacted and no paid inference
is used.
"""

import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import release_readiness  # noqa: E402

#: The credential shapes are assembled at run time so this test file does not
#: itself carry a literal that the candidate scanner would refuse - and so the
#: assertions below exercise the scanner rather than a coincidence of this
#: file's own source text.
PLACEHOLDER_KEY = "sk" + "-FIXTURE-NOT-A-REAL-KEY"
PLACEHOLDER_TOKEN = "ghp_" + "FIXTURENOTAREALTOKEN0000000"


class GateTests(unittest.TestCase):
    """One gate evaluation for the class; the evaluation is hermetic."""

    @classmethod
    def setUpClass(cls):
        cls.document = release_readiness.evaluate_gates(REPO_ROOT)
        cls.manifest = release_readiness.load_manifest(REPO_ROOT)

    def test_every_gate_is_typed_and_named(self):
        gates = self.document["gates"]
        self.assertGreaterEqual(len(gates), 10)
        seen = set()
        for gate in gates:
            self.assertIn(gate["status"], release_readiness.GATE_STATUSES)
            self.assertIn(gate["evidence"], release_readiness.GATE_EVIDENCE)
            self.assertTrue(gate["requirement"])
            self.assertTrue(gate["detail"])
            self.assertNotIn(gate["id"], seen)
            seen.add(gate["id"])

    def test_a_claim_is_never_a_pass(self):
        claimed = {
            "id": "example",
            "requirement": "example",
            "status": "pass",
            "evidence": "claimed",
            "tier": None,
            "detail": "the tracking issue is closed",
        }
        outcome = release_readiness.summarize_gates([claimed])
        self.assertFalse(outcome["release_ready"])
        self.assertEqual(outcome["failed"], ["example"])

    def test_readiness_requires_every_gate_to_pass(self):
        document = self.document
        expected = not document["failed"] and not document["not_run"]
        self.assertEqual(document["release_ready"], expected)
        if not document["release_ready"]:
            self.assertTrue(document["blocking"])
            for name in document["failed"] + document["not_run"]:
                self.assertIn(name, document["blocking"])

    def test_manifest_and_control_gates_hold(self):
        by_id = {gate["id"]: gate for gate in self.document["gates"]}
        for gate_id in (
            "manifest.pins",
            "host.patch.applied",
            "host.patch.rollback-detected",
            "host.adapter.applied",
            "host.adapter.rollback-detected",
            "approval.enforcement.disabled",
            "remote_inference.disabled",
            "candidate.exclusions",
        ):
            self.assertEqual(by_id[gate_id]["status"], "pass", by_id[gate_id]["detail"])
            self.assertEqual(by_id[gate_id]["evidence"], "verified-here")

    def test_rollback_checks_discriminate(self):
        document = self.document
        patch_gate = next(
            gate
            for gate in document["gates"]
            if gate["id"] == "host.patch.rollback-detected"
        )
        self.assertIn("error(s)", patch_gate["detail"])
        adapter_gate = next(
            gate
            for gate in document["gates"]
            if gate["id"] == "host.adapter.rollback-detected"
        )
        self.assertIn("error(s)", adapter_gate["detail"])

    def test_isolated_setup_reproduces_and_rolls_back(self):
        roundtrip = self.document["isolated_roundtrip"]
        self.assertIsNotNone(roundtrip)
        plan = roundtrip["plan"]
        self.assertEqual(plan["profile"], release_readiness.SAFE_PROFILE)
        self.assertEqual(plan["optional_features_enabled"], [])
        self.assertFalse(plan["features"]["remote_inference.enabled"])
        self.assertTrue(plan["binary_sha256"])
        self.assertEqual(
            sorted(roundtrip["profile_digests"]),
            sorted(
                path.stem for path in (REPO_ROOT / "jev" / "profiles").glob("*.json")
            ),
        )
        for digest in roundtrip["profile_digests"].values():
            self.assertEqual(len(digest), 64)
        self.assertEqual(len(roundtrip["runs"]), 2)
        for run in roundtrip["runs"]:
            self.assertEqual(run["state"], "moved")
            self.assertTrue(run["source_absent"])
            self.assertTrue(run["plan_preserved"])
            self.assertTrue(run["binary_matched_plan"])
            self.assertEqual(run["optional_features_enabled"], [])
            self.assertFalse(run["remote_inference_enabled"])

    def test_platform_gate_reports_missing_platforms(self):
        gate = next(
            gate for gate in self.document["gates"] if gate["id"] == "platform.matrix"
        )
        self.assertEqual(gate["evidence"], "recorded")
        self.assertIn(gate["status"], ("pass", "not-run", "fail"))
        supported = self.manifest["host"]["platforms"]["supported"]
        self.assertEqual(
            sorted(row["platform"] for row in self.document["platforms"]),
            sorted(supported),
        )
        evidence = release_readiness.load_platform_evidence(REPO_ROOT)
        for row in self.document["platforms"]:
            if row["status"] != "verified":
                self.assertIn(row["status"], ("not-run", "stale", "fail"))
                continue
            record = evidence["platforms"][row["platform"]]
            self.assertTrue(record["revision"])
            self.assertTrue(record["harness"])
            self.assertEqual(
                record["pinned_inputs_digest"],
                release_readiness.derive_pinned_inputs(REPO_ROOT)["digest"],
            )
            for entry in record["harness"].values():
                self.assertTrue(entry["ok"])
                self.assertEqual(entry["tier"], release_readiness.TIER_REAL_HOST)

    def test_platform_gate_refuses_a_live_provider_claim(self):
        root = Path(tempfile.mkdtemp(prefix="jev-release-live-"))
        try:
            evidence_dir = root / "jev" / "evidence"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "platform-matrix.json").write_text(
                json.dumps(
                    {
                        "schema": "jev-platform-matrix.v1",
                        "platforms": {
                            name: {
                                "revision": "0" * 40,
                                "harness": {
                                    "plaintext-collaboration": {
                                        "ok": True,
                                        "exit_code": 0,
                                        "tier": release_readiness.TIER_LIVE,
                                    }
                                },
                            }
                            for name in self.manifest["host"]["platforms"]["supported"]
                        },
                    }
                ),
                encoding="utf-8",
            )
            record = release_readiness.evaluate_platform_gate(root, self.manifest)
            self.assertEqual(record["gate"]["status"], "fail")
            self.assertTrue(all(row["status"] == "fail" for row in record["platforms"]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_platform_gate_treats_a_changed_pin_as_stale(self):
        root = Path(tempfile.mkdtemp(prefix="jev-release-stale-"))
        try:
            evidence_dir = root / "jev" / "evidence"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "platform-matrix.json").write_text(
                json.dumps(
                    {
                        "schema": "jev-platform-matrix.v1",
                        "platforms": {
                            "linux-x86_64": {
                                "revision": "0" * 40,
                                "pinned_inputs_digest": "not-the-current-digest",
                                "harness": {
                                    "plaintext-collaboration": {
                                        "ok": True,
                                        "exit_code": 0,
                                        "tier": release_readiness.TIER_REAL_HOST,
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            record = release_readiness.evaluate_platform_gate(root, self.manifest)
            row = next(
                row for row in record["platforms"] if row["platform"] == "linux-x86_64"
            )
            self.assertEqual(row["status"], "stale")
            self.assertEqual(record["gate"]["status"], "not-run")
            self.assertIn("pinned inputs", record["gate"]["detail"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class CandidateTests(unittest.TestCase):
    """Two candidate builds in one scratch directory; the builds are offline."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="jev-release-candidate-"))
        cls.first = release_readiness.build_candidate(
            REPO_ROOT, cls.scratch / "first.tar.gz"
        )
        cls.second = release_readiness.build_candidate(
            REPO_ROOT, cls.scratch / "second.tar.gz"
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_candidate_builds_without_refusal(self):
        self.assertTrue(self.first["built"])
        self.assertEqual(self.first["refusals"], [])
        self.assertEqual(self.first["denied"], [])
        self.assertGreater(self.first["included_count"], 100)
        self.assertTrue(self.first["bundle_digest"])

    def test_bundle_and_archive_are_byte_identical_across_builds(self):
        self.assertEqual(self.first["bundle_digest"], self.second["bundle_digest"])
        self.assertEqual(
            self.first["archive"]["sha256"], self.second["archive"]["sha256"]
        )
        first_bytes = Path(self.first["archive"]["path"]).read_bytes()
        second_bytes = Path(self.second["archive"]["path"]).read_bytes()
        self.assertEqual(first_bytes, second_bytes)

    def test_archive_carries_the_pins_and_the_host_boundary(self):
        with tarfile.open(self.first["archive"]["path"], "r:gz") as tar:
            names = tar.getnames()
            payload = b"".join(
                tar.extractfile(member).read()
                for member in tar.getmembers()
                if member.isfile()
            )
        self.assertIn("jev/compatibility-manifest.json", names)
        self.assertIn("codex-rs/core/src/jev_bus.rs", names)
        self.assertIn("jev/patches/0002-jev-bus-boundary.patch", names)
        self.assertEqual(names, sorted(names))
        for name in names:
            self.assertNotIn(".jev/", name)
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.endswith((".env", ".pem", ".key", ".log")))
        self.assertNotIn(PLACEHOLDER_KEY.encode(), payload)
        self.assertNotIn(PLACEHOLDER_TOKEN.encode(), payload)

    def test_credential_shaped_fixtures_are_withheld_and_declared(self):
        declared = release_readiness.SYNTHETIC_FIXTURE_EXEMPTIONS
        self.assertEqual(
            set(declared),
            {
                ".github/scripts/test_jev_capture.py",
                ".github/scripts/test_jev_screening.py",
                "jev/tests/capture_fixtures/04-secret-bearing.jsonl",
                "jev/tests/capture_fixtures/README.md",
            },
        )
        for reason in declared.values():
            self.assertTrue(reason)
        exempted = {entry["path"] for entry in self.first["exemptions"]}
        self.assertEqual(exempted, set(declared))
        with tarfile.open(self.first["archive"]["path"], "r:gz") as tar:
            names = set(tar.getnames())
        self.assertFalse(names & exempted)

    def test_candidate_refuses_credentials_and_private_state(self):
        root = Path(tempfile.mkdtemp(prefix="jev-release-refuse-"))
        try:
            (root / "jev" / "deploy").mkdir(parents=True)
            (root / "jev" / "notes.md").write_text(
                "synthetic key "
                + "sk-"
                + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                + "\n",
                encoding="utf-8",
            )
            (root / "jev" / "deploy" / "service.key").write_text("x", encoding="utf-8")
            document = release_readiness.build_candidate(
                root, root / "candidate.tar.gz"
            )
            self.assertFalse(document["built"])
            self.assertIsNone(document["archive"])
            self.assertFalse((root / "candidate.tar.gz").exists())
            codes = [refusal["code"] for refusal in document["refusals"]]
            self.assertTrue(any("E_CANDIDATE_CONTENT" in code for code in codes))
            self.assertEqual(
                [entry["path"] for entry in document["denied"]],
                ["jev/deploy/service.key"],
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


class RecordTests(unittest.TestCase):
    def test_pinned_inputs_digest_is_deterministic(self):
        first = release_readiness.derive_pinned_inputs(REPO_ROOT)
        second = release_readiness.derive_pinned_inputs(REPO_ROOT)
        self.assertEqual(first["digest"], second["digest"])
        self.assertTrue(first["files"])
        paths = [entry["path"] for entry in first["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("jev/compatibility-manifest.json", paths)

    def test_record_platform_requires_a_real_binary(self):
        missing = Path(tempfile.gettempdir()) / "jev-release-missing-codex"
        with self.assertRaises(release_readiness.ReadinessError):
            release_readiness.record_platform(REPO_ROOT, missing)


if __name__ == "__main__":
    unittest.main()
