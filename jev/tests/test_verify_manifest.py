#!/usr/bin/env python3
"""Focused tests for jev/verify_manifest.py.

Every negative case must fail explicitly rather than being ignored: an accepted
unsupported combination is the failure this verifier exists to prevent.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_manifest as vm  # noqa: E402

MANIFEST = vm.default_manifest_path()


def base_document() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class ValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = base_document()

    def test_recorded_manifest_is_valid(self) -> None:
        summary = vm.validate(self.document)
        self.assertEqual(summary["integration_revision"], self.document["integration_host"]["revision"])
        self.assertLess(summary["stage_order"].index("jev-prune.dedup"), summary["stage_order"].index("jev-context.view"))

    def test_unknown_schema_is_refused(self) -> None:
        self.document["schema"] = "something-else.v2"
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_duplicate_component_revision_is_refused(self) -> None:
        duplicate = copy.deepcopy(self.document["components"][0])
        duplicate["feature_switch"] = "another_switch"
        self.document["components"].append(duplicate)
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_short_revision_is_refused(self) -> None:
        self.document["components"][0]["revision"] = "073b3a9"
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_excluded_component_as_component_is_refused(self) -> None:
        self.document["components"][0]["id"] = vm.EXCLUDED_ID
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_excluded_component_referenced_elsewhere_is_refused(self) -> None:
        self.document["components"][0]["host_surfaces"].append(f"{vm.EXCLUDED_ID} proxy")
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_missing_exclusion_record_is_refused(self) -> None:
        self.document["exclusions"] = [
            {"id": "some-other-repo", "repository": "example/some-other-repo", "scope": "x", "reason": "y"}
        ]
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_stage_priority_inversion_is_refused(self) -> None:
        stages = self.document["bus"]["stages"]
        dedup = next(stage for stage in stages if stage["package"] == "jev-prune-kit")
        view = next(stage for stage in stages if stage["package"] == "jev-context-fabric")
        dedup["priority"], view["priority"] = 200, 100
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_overlapping_cross_package_claim_is_refused(self) -> None:
        stages = self.document["bus"]["stages"]
        view = next(stage for stage in stages if stage["package"] == "jev-context-fabric")
        view["claims"] = list(view["claims"]) + ["tool-result:read"]
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_package_carrier_claim_for_codex_is_refused(self) -> None:
        self.document["bus"]["package_carrier_hosts"].append("codex")
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_non_single_invocation_carrier_is_refused(self) -> None:
        self.document["bus"]["native_carrier"]["invocations_per_turn"] = 2
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_ownership_conflict_is_refused(self) -> None:
        entries = self.document["ownership"]["entries"]
        owned = next(entry for entry in entries if entry["mode"] == "owned-file")
        entries.append(copy.deepcopy(owned))
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_patch_order_dependency_inversion_is_refused(self) -> None:
        step = next(entry for entry in self.document["patch_order"] if entry["order"] == 2)
        step["depends_on_order"] = 3
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_patch_order_base_revision_drift_is_refused(self) -> None:
        self.document["patch_order"][0]["base_revision"] = "0" * 40
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_artifact_base_revision_drift_is_refused(self) -> None:
        patch_component = next(c for c in self.document["components"] if "artifact" in c)
        patch_component["artifact"]["base_revision"] = "0" * 40
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_missing_feature_switch_disable_path_is_refused(self) -> None:
        self.document["feature_switches"][0].pop("disable")
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)

    def test_unknown_event_producer_is_refused(self) -> None:
        self.document["event_model"]["produced_by"].append({"component": "not-a-component", "events": ["x"]})
        with self.assertRaises(vm.UnsupportedCombination):
            vm.validate(self.document)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MANIFEST.parent / "verify_manifest.py"), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_check_command_succeeds(self) -> None:
        result = self.run_cli("check", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_unknown_platform_fails_explicitly(self) -> None:
        result = self.run_cli("check", "--platform", "plan9-mips")
        self.assertEqual(result.returncode, 2)
        self.assertIn("plan9-mips", result.stderr)

    def test_recorded_platform_is_accepted(self) -> None:
        result = self.run_cli("check", "--platform", "linux-x86_64")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_component_argument_shape_is_enforced(self) -> None:
        result = self.run_cli("resolve", "--component", "jev-prune-kit")
        self.assertEqual(result.returncode, 2)

    def test_missing_manifest_fails_explicitly(self) -> None:
        result = self.run_cli("check", "--manifest", "/nonexistent/manifest.json")
        self.assertEqual(result.returncode, 2)


class ResolveTests(unittest.TestCase):
    def test_revision_mismatch_reports_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "file.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"], check=True)
            document = base_document()
            with self.assertRaises(vm.Mismatch):
                vm.resolve(document, repo, {})

    def test_matching_revision_binds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "file.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"], check=True)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
            document = base_document()
            document["integration_host"]["revision"] = head
            resolution = vm.resolve(document, repo, {})
            self.assertEqual(resolution["integration_revision"], head)

    def test_vendored_bus_drift_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "src" / "jev_context").mkdir(parents=True)
            (directory / "adapters").mkdir(parents=True)
            (directory / "src" / "jev_context" / "bus.py").write_text("drifted\n", encoding="utf-8")
            (directory / "adapters" / "bus.mjs").write_text("drifted\n", encoding="utf-8")
            with self.assertRaises(vm.Mismatch):
                vm.resolve(base_document(), None, {"jev-context-fabric": directory})


if __name__ == "__main__":
    unittest.main()
