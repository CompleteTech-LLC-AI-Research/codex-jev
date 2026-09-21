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
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
JEV_ROOT = REPO_ROOT / "jev"

SPEC = importlib.util.spec_from_file_location(
    "jev_manifest", JEV_ROOT / "scripts" / "jev_manifest.py"
)
jev_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jev_manifest)


def load_manifest():
    return json.loads((JEV_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8"))


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
            self.assertEqual(patch["sha256"], jev_manifest.sha256_file(path), patch["id"])


class UnsupportedCombinationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
