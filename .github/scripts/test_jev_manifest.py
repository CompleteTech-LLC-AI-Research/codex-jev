"""Required-CI gate for the fork-local JEV compatibility manifest under ``jev/``.

These checks run from ``just test-github-scripts`` alongside the other
repository-level Python checks. They assert that the checked-in manifest and
every shipped profile resolve, and that unsupported combinations fail
explicitly instead of being silently accepted.
"""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
JEV_ROOT = REPO_ROOT / "jev"

SPEC = importlib.util.spec_from_file_location(
    "jev_manifest", JEV_ROOT / "scripts" / "jev_manifest.py"
)
jev_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jev_manifest)


def load_manifest():
    return json.loads(
        (JEV_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8")
    )


class CheckedInArtifactsTests(unittest.TestCase):
    def test_checked_in_manifest_validates(self):
        errors = jev_manifest.validate_manifest(load_manifest(), repo_root=REPO_ROOT)
        self.assertEqual([], errors)

    def test_every_shipped_profile_validates(self):
        profiles = sorted((JEV_ROOT / "profiles").glob("*.json"))
        self.assertTrue(profiles, "expected at least one shipped integration profile")
        for path in profiles:
            profile = json.loads(path.read_text(encoding="utf-8"))
            errors = jev_manifest.validate_manifest(
                load_manifest(), repo_root=REPO_ROOT, profile=profile
            )
            self.assertEqual([], errors, f"{path.name}: {errors}")

    def test_patch_digests_match_the_recorded_files(self):
        for patch in load_manifest()["patches"]:
            path = REPO_ROOT / patch["file"]
            self.assertTrue(path.is_file(), f"missing patch file {patch['file']}")
            self.assertEqual(
                patch["sha256"], jev_manifest.sha256_file(path), patch["id"]
            )


class UnsupportedCombinationTests(unittest.TestCase):
    def test_profile_that_pins_a_different_fabric_revision_is_refused(self):
        profile = json.loads(
            (JEV_ROOT / "profiles" / "isolated-offline.json").read_text("utf-8")
        )
        self.assertIn("fabric", profile)
        profile["fabric"]["revision"] = "0" * 40
        errors = jev_manifest.validate_manifest(
            load_manifest(), repo_root=REPO_ROOT, profile=profile
        )
        self.assertTrue(
            any("E_PROFILE_FABRIC_PIN" in error for error in errors), errors
        )

    def test_profile_that_pins_an_unknown_component_is_refused(self):
        profile = json.loads(
            (JEV_ROOT / "profiles" / "isolated-offline.json").read_text("utf-8")
        )
        profile["fabric"]["component"] = "jev-nonexistent"
        errors = jev_manifest.validate_manifest(
            load_manifest(), repo_root=REPO_ROOT, profile=profile
        )
        self.assertTrue(
            any("E_PROFILE_FABRIC_PIN" in error for error in errors), errors
        )

    def test_excluded_repository_may_not_appear_as_a_component(self):
        manifest = load_manifest()
        exclusion = manifest["integration"]["excluded_repositories"][0]
        manifest["components"].append(
            {
                "id": "omniroute",
                "kind": "python",
                "repository": exclusion,
                "revision": "0" * 40,
                "license": "Apache-2.0",
                "python_requirement": ">=3.11",
                "provides": [],
                "requires_interfaces": {},
                "owns": [],
            }
        )
        errors = jev_manifest.validate_manifest(manifest, repo_root=REPO_ROOT)
        self.assertTrue(
            any("E_" in error and "omni" in error.lower() for error in errors),
            errors,
        )

    def test_remote_inference_without_consent_is_refused(self):
        manifest = copy.deepcopy(load_manifest())
        manifest["features"]["remote_inference.enabled"]["default"] = True
        errors = jev_manifest.validate_manifest(manifest, repo_root=REPO_ROOT)
        self.assertTrue(
            any("E_REMOTE_INFERENCE_UNAUTHORIZED" in error for error in errors),
            errors,
        )

    def test_missing_patch_file_is_refused(self):
        manifest = copy.deepcopy(load_manifest())
        manifest["patches"][0]["file"] = "jev/patches/does-not-exist.patch"
        errors = jev_manifest.validate_manifest(manifest, repo_root=REPO_ROOT)
        self.assertTrue(errors)


class ApprovalEnforcementGateTests(unittest.TestCase):
    """Enforcement is gated on a declared evaluation record, in CI, fail closed.

    Issue #23's acceptance criteria ask that enforcement stays disabled until the
    declared evaluation criteria are met. These checks pin the *declarative* half
    of that claim at the lane level: the manifest declares the gate, the shipped
    state is disabled, and a configuration that reaches for enforcement without
    the record is refused by both the manifest validator and the shadow gate.
    """

    COMPONENT = "jev-codex-approval"
    SWITCH = "approval.enforcement"
    SCRIPT = JEV_ROOT / "scripts" / "verify-manifest.py"
    SHADOW = JEV_ROOT / "scripts" / "shadow_comparison.py"

    def component(self, manifest):
        return next(
            item for item in manifest["components"] if item["id"] == self.COMPONENT
        )

    def run_cmd(self, *args):
        return subprocess.run(
            [sys.executable, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_shipped_manifest_declares_and_validates_the_gate(self):
        manifest = load_manifest()
        record = self.component(manifest)["evaluation"]
        self.assertEqual(record["enforce_switch"], self.SWITCH)
        self.assertEqual(record["shadow_switch"], "approval.preflight")
        self.assertEqual(
            jev_manifest.check_approval_enforcement(manifest, "disabled"), []
        )
        result = self.run_cmd(str(self.SCRIPT), "--approval-enforcement", "disabled")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_enforcement_may_not_default_to_on(self):
        manifest = copy.deepcopy(load_manifest())
        manifest["features"][self.SWITCH]["default"] = True
        errors = jev_manifest.check_approval_enforcement(manifest, "disabled")
        self.assertTrue(any("E_EVALUATION_STATE" in error for error in errors), errors)
        self.assertTrue(
            any(
                "E_EVALUATION_SWITCH" in error
                for error in jev_manifest.validate_manifest(
                    manifest, repo_root=REPO_ROOT
                )
            )
        )

    def test_an_enforcement_owner_without_a_record_is_a_missing_gate(self):
        manifest = copy.deepcopy(load_manifest())
        del self.component(manifest)["evaluation"]
        errors = jev_manifest.validate_manifest(manifest, repo_root=REPO_ROOT)
        self.assertTrue(
            any("E_EVALUATION_MISSING" in error for error in errors), errors
        )
        state = jev_manifest.check_approval_enforcement(manifest, "disabled")
        self.assertTrue(any("E_EVALUATION_MISSING" in error for error in state), state)

    def test_shadow_gate_reads_the_declared_record(self):
        result = self.run_cmd(
            str(self.SHADOW),
            "gate",
            "--set",
            "jev/fixtures/shadow/holdout.json",
            "--criteria",
            "jev/fixtures/shadow/criteria.json",
            "--freeze",
            "jev/fixtures/shadow/frozen.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["declared_contract"]["component"], self.COMPONENT, state)
        self.assertEqual(
            state["declared_contract"]["enforce_switch"], self.SWITCH, state
        )

    def test_shadow_gate_refuses_a_manifest_without_the_record(self):
        manifest = copy.deepcopy(load_manifest())
        del self.component(manifest)["evaluation"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compatibility-manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_cmd(
                str(self.SHADOW),
                "gate",
                "--set",
                "jev/fixtures/shadow/holdout.json",
                "--criteria",
                "jev/fixtures/shadow/criteria.json",
                "--freeze",
                "jev/fixtures/shadow/frozen.json",
                "--manifest",
                str(path),
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("E_EVALUATION_MISSING", result.stderr)


if __name__ == "__main__":
    unittest.main()
