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
import subprocess
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

    def test_a_skipped_roundtrip_cannot_reach_release_ready(self):
        # #98 asked for this case in its own words: with the round trip skipped,
        # `release_ready` must be false even when every platform is recorded.
        # The skipped gate is emitted `not-run`, so it is part of the conjunction
        # `summarize_gates` evaluates rather than absent from it; before #106 the
        # gate was dropped and this same construction returned `True`.
        skipped = release_readiness.evaluate_gates(REPO_ROOT, run_roundtrip=False)
        by_id = {gate["id"]: gate for gate in skipped["gates"]}
        self.assertIn("isolated.roundtrip", by_id)
        roundtrip = by_id["isolated.roundtrip"]
        self.assertEqual(roundtrip["status"], "not-run")
        self.assertEqual(roundtrip["evidence"], "not-run")
        self.assertIn("isolated.roundtrip", skipped["not_run"])
        self.assertIn("isolated.roundtrip", skipped["blocking"])
        # The gate that ran and the gate that was skipped are the same
        # requirement, so a reviewer can compare the two evaluations directly.
        executed = {gate["id"]: gate for gate in self.document["gates"]}
        self.assertEqual(
            roundtrip["requirement"], executed["isolated.roundtrip"]["requirement"]
        )
        # Every other gate passing is still not enough to release.
        every_other_gate_passes = [
            {**gate, "status": "pass"} if gate["id"] != "isolated.roundtrip" else gate
            for gate in skipped["gates"]
        ]
        outcome = release_readiness.summarize_gates(every_other_gate_passes)
        self.assertEqual(outcome["failed"], [])
        self.assertFalse(outcome["release_ready"])
        self.assertEqual(outcome["not_run"], ["isolated.roundtrip"])

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
                self.assertIn(
                    row["status"], ("not-run", "stale", "unverifiable", "fail")
                )
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

    def test_an_unresolvable_revision_is_not_credited(self):
        # An invented or never-fetched SHA does not resolve in this clone. That
        # is "the binding cannot be checked", which is not a pass, so the record
        # must be refused rather than credited (issue #97).
        reachable = release_readiness.revision_reachable(REPO_ROOT, "0" * 40)
        self.assertIsNone(reachable)
        status, reason = release_readiness.classify_platform_record(
            self.record(revision="0" * 40), "digest", reachable
        )
        self.assertEqual(status, "unverifiable")
        self.assertIn("not resolvable", reason)
        self.assertEqual(
            release_readiness.classify_platform_record(
                self.record(revision="0" * 40), "digest", False
            )[0],
            "stale",
        )
        status, reason = release_readiness.classify_platform_record(
            self.record(revision="a" * 40), "digest", True
        )
        self.assertEqual(status, "verified")
        self.assertIn("reachable", reason)

    def test_a_dirty_worktree_cannot_produce_a_credited_record(self):
        # #136: `record_platform` pairs the committed revision with a digest
        # taken from the *working tree*, so before this fix a worktree carrying
        # pinned inputs its revision does not (e.g. the untracked script that
        # postdates the pre-#116 HEAD) could be recorded and later credited.
        # Reproduce that situation in a throwaway repository and assert the
        # incoherent pair is refused rather than verified.
        root = Path(tempfile.mkdtemp(prefix="jev-release-dirty-"))
        try:
            scripts = root / "jev" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "jev_diagnostics.py").write_text("x = 1\n", encoding="utf-8")
            self._git(root, "init")
            self._git(root, "add", "jev/scripts/jev_diagnostics.py")
            self._git(
                root,
                "-c",
                "user.email=t@example.com",
                "-c",
                "user.name=t",
                "commit",
                "-m",
                "base without the untracked script",
            )
            revision = self._git(root, "rev-parse", "HEAD").strip()
            # The working tree now carries a pinned input HEAD does not.
            (scripts / "untracked_pinned.py").write_text("y = 2\n", encoding="utf-8")
            worktree = release_readiness.derive_pinned_inputs(root)["digest"]
            committed = release_readiness.pinned_inputs_digest_at(root, revision)
            self.assertIsNotNone(committed)
            self.assertNotEqual(worktree, committed)
            status, reason = release_readiness.classify_platform_record(
                self.record(revision=revision, pinned_inputs_digest=worktree),
                worktree,
                True,
                committed_inputs=committed,
            )
            self.assertEqual(status, "fail")
            self.assertNotEqual(status, "verified")
            self.assertIn("revision", reason)
            # And the writer refuses to emit the incoherent pair at all.
            binary = root / "codex-stub"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            with self.assertRaises(release_readiness.ReadinessError):
                release_readiness.record_platform(root, binary)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    @staticmethod
    def record(revision="a" * 40, pinned_inputs_digest="digest", ok=True):
        return {
            "revision": revision,
            "pinned_inputs_digest": pinned_inputs_digest,
            "harness": {
                "plaintext-collaboration": {
                    "ok": ok,
                    "exit_code": 0 if ok else 1,
                    "tier": release_readiness.TIER_REAL_HOST,
                }
            },
        }

    def test_platform_gate_refuses_an_unresolvable_revision(self):
        root = Path(tempfile.mkdtemp(prefix="jev-release-unresolvable-"))
        try:
            evidence_dir = root / "jev" / "evidence"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "platform-matrix.json").write_text(
                json.dumps(
                    {
                        "schema": "jev-platform-matrix.v1",
                        "platforms": {
                            "linux-x86_64": self.record(
                                revision="0" * 40,
                                pinned_inputs_digest=release_readiness.derive_pinned_inputs(
                                    root
                                )["digest"],
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
            record = release_readiness.evaluate_platform_gate(root, self.manifest)
            row = next(
                row for row in record["platforms"] if row["platform"] == "linux-x86_64"
            )
            self.assertEqual(row["status"], "unverifiable")
            self.assertEqual(record["gate"]["status"], "not-run")
            self.assertIn("not resolvable", record["gate"]["detail"])
            self.assertIn("linux-x86_64", record["blockers"])
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
