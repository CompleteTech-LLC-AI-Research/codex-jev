#!/usr/bin/env python3
"""Behavioral tests for the manifest verifier's explicit-failure contract.

The acceptance criterion for phase 1.1 is that a clean checkout resolves exact
inputs and that unsupported combinations fail explicitly. These tests prove the
second half against real git repositories and real artifact bytes rather than
mocking the failure path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPTS = HERE.parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_manifest as vm  # noqa: E402


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ExplicitFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "jev-example-component"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "config", "user.email", "test@example.invalid")
        run_git(self.repo, "config", "user.name", "Test")
        self.artifact = self.repo / "artifact.txt"
        self.artifact.write_text("canonical-bytes\n", encoding="utf-8")
        run_git(self.repo, "add", "artifact.txt")
        run_git(self.repo, "commit", "-q", "-m", "pin")
        self.commit = run_git(self.repo, "rev-parse", "HEAD")
        self.digest = vm.sha256_file(self.artifact)
        self.manifest = {
            "component_pins": [
                {
                    "name": "jev-example-component",
                    "repository": "example/invalid",
                    "commit": self.commit,
                    "role": "test",
                    "delivery": "package",
                    "artifacts": [{"path": "artifact.txt", "sha256": self.digest}],
                    "status": "declared",
                }
            ]
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pinned_component_resolves(self) -> None:
        notes = vm.check_component_pins(self.manifest, self.root)
        self.assertEqual(len(notes), 1)
        self.assertIn("jev-example-component", notes[0])

    def test_drifted_artifact_is_refused(self) -> None:
        self.artifact.write_text("tampered-bytes\n", encoding="utf-8")
        with self.assertRaises(vm.UnsupportedCombination) as ctx:
            vm.check_component_pins(self.manifest, self.root)
        self.assertIn("sha256", str(ctx.exception))

    def test_moved_pin_is_refused(self) -> None:
        self.manifest["component_pins"][0]["commit"] = "0" * 40
        with self.assertRaises(vm.UnsupportedCombination) as ctx:
            vm.check_component_pins(self.manifest, self.root)
        self.assertIn("pinned", str(ctx.exception))

    def test_missing_checkout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(vm.UnsupportedCombination) as ctx:
                vm.check_component_pins(self.manifest, Path(empty))
        self.assertIn("missing", str(ctx.exception))


class BlobAnchorTests(unittest.TestCase):
    def test_blob_anchor_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "codex-jev"
            repo.mkdir()
            run_git(repo, "init", "-q")
            run_git(repo, "config", "user.email", "test@example.invalid")
            run_git(repo, "config", "user.name", "Test")
            source = repo / "file.rs"
            source.write_text("fn main() {}\n", encoding="utf-8")
            run_git(repo, "add", "file.rs")
            run_git(repo, "commit", "-q", "-m", "pin")
            commit = run_git(repo, "rev-parse", "HEAD")
            manifest = {
                "integration_host": {"codex_pin": {"commit": commit}},
                "patch_order": [
                    {
                        "order": 1,
                        "name": "example",
                        "verified_blobs_before": {"file.rs": "0" * 40},
                    }
                ],
            }
            with self.assertRaises(vm.UnsupportedCombination) as ctx:
                vm.check_codex_pin(manifest, repo)
            self.assertIn("pinned against", str(ctx.exception))


class ExclusionTests(unittest.TestCase):
    def test_excluded_component_reference_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            integration = Path(tmp) / "integration"
            (integration / "contracts").mkdir(parents=True)
            (integration / "README.md").write_text("names it: omitted\n", encoding="utf-8")
            (integration / "contracts" / "dep.md").write_text(
                "depends on omniroute-codex-docker\n", encoding="utf-8"
            )
            manifest = {"excluded_components": [{"name": "omniroute-codex-docker"}]}
            with self.assertRaises(vm.UnsupportedCombination) as ctx:
                vm.check_exclusions(manifest, integration)
            self.assertIn("excluded component referenced", str(ctx.exception))

    def test_declaration_files_may_name_the_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            integration = Path(tmp) / "integration"
            integration.mkdir()
            (integration / "README.md").write_text("omniroute-codex-docker\n", encoding="utf-8")
            manifest = {"excluded_components": [{"name": "omniroute-codex-docker"}]}
            notes = vm.check_exclusions(manifest, integration)
            self.assertIn("omniroute-codex-docker", notes[0])


class PlatformTests(unittest.TestCase):
    def test_unlisted_platform_is_refused(self) -> None:
        manifest = {"supported_platforms": [{"platform": "plan9", "status": "declared", "notes": ""}]}
        with self.assertRaises(vm.UnsupportedCombination) as ctx:
            vm.check_platform(manifest)
        self.assertIn("not listed", str(ctx.exception))

    def test_unsupported_platform_is_refused(self) -> None:
        here = vm.current_platform()
        manifest = {"supported_platforms": [{"platform": here, "status": "unsupported", "notes": "off"}]}
        with self.assertRaises(vm.UnsupportedCombination):
            vm.check_platform(manifest)


class ManifestShapeTests(unittest.TestCase):
    def test_checked_in_manifest_is_wellformed(self) -> None:
        manifest_path = HERE.parents[1] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "jev.codex.integration.manifest.v1")
        self.assertEqual(
            manifest["excluded_components"][0]["name"], "omniroute-codex-docker"
        )
        # The phase-1 pin is the fork's integration baseline.
        self.assertEqual(
            manifest["integration_host"]["codex_pin"]["commit"],
            "8198a91a4f46b01647bc6c0d8d63afafbf4c9180",
        )
        names = {pin["name"] for pin in manifest["component_pins"]}
        self.assertEqual(
            names,
            {
                "codex-plaintext-collab",
                "jev-context-fabric",
                "jev-prune-kit",
                "jev-sentinel",
                "jev-codex-approval",
            },
        )


if __name__ == "__main__":
    unittest.main()
