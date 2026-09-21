"""Focused tests for the JEV compatibility manifest and integration profiles.

Run with: python3 -m unittest discover -s jev/tests -t .
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jev_manifest

REPO_ROOT = jev_manifest.repository_root()
MANIFEST_PATH = REPO_ROOT / "jev" / "compatibility-manifest.json"
PROFILES = REPO_ROOT / "jev" / "profiles"


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_profile(name):
    return json.loads((PROFILES / f"{name}.json").read_text(encoding="utf-8"))


def component(manifest, component_id):
    return next(c for c in manifest["components"] if c["id"] == component_id)


def codes(errors):
    return sorted({error.split(":", 1)[0] for error in errors})


class ManifestBaselineTests(unittest.TestCase):
    def test_shipped_manifest_and_profiles_are_supported(self):
        manifest = load_manifest()
        self.assertEqual(jev_manifest.validate_manifest(manifest), [])
        for name in (
            "isolated-build",
            "baseline",
            "integrated-offline",
            "enforcement-eval",
        ):
            with self.subTest(profile=name):
                self.assertEqual(
                    jev_manifest.validate_manifest(manifest, profile=load_profile(name)), []
                )

    def test_shipped_patch_matches_its_recorded_digest(self):
        manifest = load_manifest()
        patch = manifest["patches"][0]
        path = REPO_ROOT / patch["file"]
        self.assertTrue(path.is_file())
        self.assertEqual(jev_manifest.sha256_file(path), patch["sha256"])


class ManifestStructureTests(unittest.TestCase):
    def test_missing_top_level_key_is_rejected(self):
        manifest = load_manifest()
        del manifest["interfaces"]
        self.assertIn("E_MANIFEST_SCHEMA", codes(jev_manifest.validate_manifest(manifest)))

    def test_unknown_top_level_key_is_rejected(self):
        manifest = load_manifest()
        manifest["omniroute"] = {"enabled": True}
        self.assertIn("E_MANIFEST_SCHEMA", codes(jev_manifest.validate_manifest(manifest)))

    def test_wrong_manifest_version_is_rejected(self):
        manifest = load_manifest()
        manifest["manifest_version"] = 2
        self.assertIn("E_MANIFEST_VERSION", codes(jev_manifest.validate_manifest(manifest)))

    def test_excluded_repository_cannot_be_integrated(self):
        manifest = load_manifest()
        manifest["components"].append(
            {
                "id": "omniroute-codex-docker",
                "kind": "python",
                "repository": "CompleteTech-LLC-AI-Research/omniroute-codex-docker",
                "revision": "0" * 40,
                "license": "MIT",
                "python_requirement": ">=3.11",
                "provides": [],
                "requires_interfaces": {},
                "owns": ["omniroute/"],
            }
        )
        self.assertIn("E_EXCLUDED_COMPONENT", codes(jev_manifest.validate_manifest(manifest)))

    def test_duplicate_component_revision_target_is_rejected(self):
        manifest = load_manifest()
        clone = copy.deepcopy(component(manifest, "jev-sentinel"))
        clone["id"] = "jev-sentinel-copy"
        manifest["components"].append(clone)
        self.assertIn("E_DUPLICATE_COMPONENT", codes(jev_manifest.validate_manifest(manifest)))

    def test_mutable_revision_is_rejected(self):
        manifest = load_manifest()
        component(manifest, "jev-prune-kit")["revision"] = "main"
        self.assertIn("E_COMPONENT_REVISION", codes(jev_manifest.validate_manifest(manifest)))

    def test_interface_version_mismatch_is_rejected(self):
        manifest = load_manifest()
        manifest["interfaces"]["jev_bus"] = 2
        self.assertIn("E_INTERFACE_VERSION", codes(jev_manifest.validate_manifest(manifest)))

    def test_two_writable_owners_for_one_path_in_one_repository_are_rejected(self):
        manifest = load_manifest()
        component(manifest, "codex-jev")["owns"].append("codex-rs/core/src/tools/router.rs")
        self.assertIn("E_OWNERSHIP_OVERLAP", codes(jev_manifest.validate_manifest(manifest)))

    def test_shared_interface_needs_one_declared_owner(self):
        manifest = load_manifest()
        manifest["ownership"]["interface_owners"]["jev_bus"] = "jev-context-fabric"
        self.assertIn(
            "E_OWNERSHIP_INTERFACE", codes(jev_manifest.validate_manifest(manifest))
        )

        manifest = load_manifest()
        del manifest["ownership"]["interface_owners"]["sentinel_boundary"]
        self.assertIn(
            "E_OWNERSHIP_INTERFACE", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_host_must_own_the_jev_tree(self):
        manifest = load_manifest()
        manifest["components"][0]["owns"] = ["codex-rs/core/src/tools/router.rs"]
        self.assertIn("E_OWNERSHIP_HOST", codes(jev_manifest.validate_manifest(manifest)))


class ManifestPatchTests(unittest.TestCase):
    def test_changed_patch_bytes_are_rejected(self):
        manifest = load_manifest()
        manifest["patches"][0]["sha256"] = "0" * 64
        self.assertIn("E_PATCH_HASH", codes(jev_manifest.validate_manifest(manifest)))

    def test_patch_pinned_to_another_base_is_rejected(self):
        manifest = load_manifest()
        manifest["patches"][0]["applies_to_host_base"] = "1" * 40
        self.assertIn("E_PATCH_BASE", codes(jev_manifest.validate_manifest(manifest)))

    def test_patch_target_outside_host_ownership_is_rejected(self):
        manifest = load_manifest()
        manifest["patches"][0]["targets"] = ["codex-rs/core/src/guardian/mod.rs"]
        self.assertIn("E_PATCH_OWNERSHIP", codes(jev_manifest.validate_manifest(manifest)))

    def test_duplicate_patch_order_is_rejected(self):
        manifest = load_manifest()
        clone = copy.deepcopy(manifest["patches"][0])
        clone["id"] = "0002-second-patch"
        manifest["patches"].append(clone)
        self.assertIn("E_PATCH_ORDER", codes(jev_manifest.validate_manifest(manifest)))


class ManifestFeatureTests(unittest.TestCase):
    def test_unknown_requirement_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["sentinel.enforcement"]["requires"] = ["sentinel.missing"]
        self.assertIn(
            "E_FEATURE_UNKNOWN_REQUIREMENT", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_unknown_component_or_patch_reference_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["approval.preflight"]["components"] = ["jev-approval-missing"]
        manifest["features"]["collab.plaintext_messages"]["requires_patches"] = ["0009-missing"]
        found = codes(jev_manifest.validate_manifest(manifest))
        self.assertIn("E_FEATURE_UNKNOWN_COMPONENT", found)
        self.assertIn("E_FEATURE_UNKNOWN_PATCH", found)

    def test_build_time_feature_must_name_its_patches(self):
        manifest = load_manifest()
        del manifest["features"]["collab.plaintext_messages"]["requires_patches"]
        self.assertIn(
            "E_FEATURE_BUILD_TIME_PATCH", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_default_enablement_conflict_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["projection.fabric_views"]["default"] = True
        self.assertIn(
            "E_FEATURE_DEFAULT_CONFLICT", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_remote_inference_enabled_by_default_without_consent_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["remote_inference.enabled"]["default"] = True
        self.assertIn(
            "E_REMOTE_INFERENCE_UNAUTHORIZED", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_remote_inference_enabled_without_budget_is_rejected(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["consent"] = True
        profile = {"profile_version": 1, "id": "remote", "features": {"remote_inference.enabled": True}}
        self.assertIn(
            "E_REMOTE_INFERENCE_UNAUTHORIZED",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )

    def test_remote_inference_enabled_with_consent_and_budget_is_supported(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["consent"] = True
        manifest["credentials"]["remote_inference"]["budget_usd_max"] = 5
        profile = {"profile_version": 1, "id": "remote", "features": {"remote_inference.enabled": True}}
        self.assertEqual(
            jev_manifest.validate_manifest(manifest, profile=profile), []
        )

    def test_negative_budget_is_rejected(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["budget_usd_max"] = -1
        self.assertIn("E_CREDENTIAL_BUDGET", codes(jev_manifest.validate_manifest(manifest)))


class ProfileTests(unittest.TestCase):
    def test_profile_cannot_enable_a_feature_without_its_dependency(self):
        manifest = load_manifest()
        profile = {
            "profile_version": 1,
            "id": "views-without-dedup",
            "features": {"projection.fabric_views": True, "projection.dedup_receipts": False},
        }
        self.assertIn(
            "E_PROFILE_DEPENDENCY",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )

    def test_profile_cannot_disable_a_required_dependency(self):
        manifest = load_manifest()
        profile = {
            "profile_version": 1,
            "id": "retrieval-without-capture",
            "features": {"capture.canonical_evidence": False},
        }
        self.assertIn(
            "E_PROFILE_DEPENDENCY",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )

    def test_profile_unknown_feature_is_rejected(self):
        manifest = load_manifest()
        profile = {
            "profile_version": 1,
            "id": "unknown",
            "features": {"sentinel.telepathy": True},
        }
        self.assertIn(
            "E_PROFILE_UNKNOWN_FEATURE",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )

    def test_profile_version_is_checked(self):
        manifest = load_manifest()
        profile = {"profile_version": 2, "id": "old", "features": {}}
        self.assertIn(
            "E_PROFILE_VERSION",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )


class CheckoutTests(unittest.TestCase):
    def _git(self, repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )

    def _init_repo(self, root):
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "tests@example.invalid")
        self._git(root, "config", "user.name", "Integration Tests")

    def test_patch_state_detects_applied_and_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            target = root / "codex-rs" / "core" / "src" / "tools" / "router.rs"
            target.parent.mkdir(parents=True)
            target.write_text("original\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "base")

            target.write_text("patched\n", encoding="utf-8")
            diff = self._git(root, "diff").stdout
            patch_path = root / "jev" / "patches" / "test.patch"
            patch_path.parent.mkdir(parents=True)
            patch_path.write_text(diff, encoding="utf-8")
            self._git(root, "checkout", "--", ".")

            manifest = load_manifest()
            manifest["patches"] = [
                {
                    "id": "test-patch",
                    "order": 10,
                    "component": "codex-plaintext-collab",
                    "file": "jev/patches/test.patch",
                    "sha256": jev_manifest.sha256_file(patch_path),
                    "applies_to_host_base": manifest["host"]["base_commit"],
                    "targets": ["codex-rs/core/src/tools/router.rs"],
                    "behavior": "test",
                    "disable": "test",
                }
            ]
            self.assertEqual(jev_manifest.check_patch_state(manifest, root, "absent"), [])
            self.assertIn(
                "E_PATCH_STATE", codes(jev_manifest.check_patch_state(manifest, root, "applied"))
            )

            target.write_text("patched\n", encoding="utf-8")
            self.assertEqual(jev_manifest.check_patch_state(manifest, root, "applied"), [])
            self.assertIn(
                "E_PATCH_STATE", codes(jev_manifest.check_patch_state(manifest, root, "absent"))
            )

    def test_checkout_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            (root / "README.md").write_text("pin\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "base")
            manifest = load_manifest()
            self.assertIn(
                "E_CHECKOUT_MISMATCH", codes(jev_manifest.check_checkout(manifest, root))
            )
            head = self._git(root, "rev-parse", "HEAD").stdout.strip()
            manifest["host"]["base_commit"] = head
            self.assertEqual(jev_manifest.check_checkout(manifest, root), [])

    def test_component_revision_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            component_root = root / "jev-sentinel"
            component_root.mkdir()
            self._init_repo(component_root)
            (component_root / "README.md").write_text("component\n", encoding="utf-8")
            self._git(component_root, "add", "-A")
            self._git(component_root, "commit", "-qm", "component")
            manifest = load_manifest()
            self.assertIn(
                "E_COMPONENT_REVISION",
                codes(jev_manifest.check_component_revisions(manifest, root)),
            )
            manifest["components"] = [
                c
                for c in manifest["components"]
                if c["id"] in {"codex-jev", "jev-sentinel"}
            ]
            component(manifest, "jev-sentinel")["revision"] = self._git(
                component_root, "rev-parse", "HEAD"
            ).stdout.strip()
            self.assertEqual(jev_manifest.check_component_revisions(manifest, root), [])

class IsolatedBuildTests(unittest.TestCase):
    """The isolated profile and the deterministic offline fixtures stay honest."""

    def test_isolated_profile_disables_every_feature(self):
        manifest = load_manifest()
        profile = load_profile("isolated-build")
        self.assertEqual(set(profile["features"]), set(manifest["features"]))
        self.assertTrue(all(value is False for value in profile["features"].values()))
        self.assertEqual(jev_manifest.validate_manifest(manifest, profile=profile), [])

    def test_shipped_fixtures_match_their_recorded_digest(self):
        fixtures_root = REPO_ROOT / "jev" / "fixtures"
        registry = json.loads(
            (fixtures_root / "manifest.json").read_text(encoding="utf-8")
        )
        for entry in registry["fixtures"]:
            with self.subTest(fixture=entry["id"]):
                path = fixtures_root / entry["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(jev_manifest.sha256_file(path), entry["sha256"])
                self.assertEqual(entry["tier"], "offline fixture")

    def test_fixture_runner_accepts_the_shipped_fixtures(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run-offline-fixtures.py"), "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
