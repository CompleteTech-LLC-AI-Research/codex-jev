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
        for name in ("baseline", "integrated-offline", "enforcement-eval"):
            with self.subTest(profile=name):
                self.assertEqual(
                    jev_manifest.validate_manifest(
                        manifest, profile=load_profile(name)
                    ),
                    [],
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
        self.assertIn(
            "E_MANIFEST_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_unknown_top_level_key_is_rejected(self):
        manifest = load_manifest()
        manifest["omniroute"] = {"enabled": True}
        self.assertIn(
            "E_MANIFEST_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_wrong_manifest_version_is_rejected(self):
        manifest = load_manifest()
        manifest["manifest_version"] = 2
        self.assertIn(
            "E_MANIFEST_VERSION", codes(jev_manifest.validate_manifest(manifest))
        )

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
        self.assertIn(
            "E_EXCLUDED_COMPONENT", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_duplicate_component_revision_target_is_rejected(self):
        manifest = load_manifest()
        clone = copy.deepcopy(component(manifest, "jev-sentinel"))
        clone["id"] = "jev-sentinel-copy"
        manifest["components"].append(clone)
        self.assertIn(
            "E_DUPLICATE_COMPONENT", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_mutable_revision_is_rejected(self):
        manifest = load_manifest()
        component(manifest, "jev-prune-kit")["revision"] = "main"
        self.assertIn(
            "E_COMPONENT_REVISION", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_interface_version_mismatch_is_rejected(self):
        manifest = load_manifest()
        manifest["interfaces"]["jev_bus"] = 2
        self.assertIn(
            "E_INTERFACE_VERSION", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_two_writable_owners_for_one_path_in_one_repository_are_rejected(self):
        manifest = load_manifest()
        component(manifest, "codex-jev")["owns"].append(
            "codex-rs/core/src/tools/router.rs"
        )
        self.assertIn(
            "E_OWNERSHIP_OVERLAP", codes(jev_manifest.validate_manifest(manifest))
        )

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
        self.assertIn(
            "E_OWNERSHIP_HOST", codes(jev_manifest.validate_manifest(manifest))
        )


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
        self.assertIn(
            "E_PATCH_OWNERSHIP", codes(jev_manifest.validate_manifest(manifest))
        )

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
            "E_FEATURE_UNKNOWN_REQUIREMENT",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_unknown_component_or_patch_reference_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["approval.preflight"]["components"] = [
            "jev-approval-missing"
        ]
        manifest["features"]["collab.plaintext_messages"]["requires_patches"] = [
            "0009-missing"
        ]
        found = codes(jev_manifest.validate_manifest(manifest))
        self.assertIn("E_FEATURE_UNKNOWN_COMPONENT", found)
        self.assertIn("E_FEATURE_UNKNOWN_PATCH", found)

    def test_build_time_feature_must_name_its_patches(self):
        manifest = load_manifest()
        del manifest["features"]["collab.plaintext_messages"]["requires_patches"]
        self.assertIn(
            "E_FEATURE_BUILD_TIME_PATCH",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_default_enablement_conflict_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["projection.fabric_views"]["default"] = True
        self.assertIn(
            "E_FEATURE_DEFAULT_CONFLICT",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_remote_inference_enabled_by_default_without_consent_is_rejected(self):
        manifest = load_manifest()
        manifest["features"]["remote_inference.enabled"]["default"] = True
        self.assertIn(
            "E_REMOTE_INFERENCE_UNAUTHORIZED",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_remote_inference_enabled_without_budget_is_rejected(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["consent"] = True
        profile = {
            "profile_version": 1,
            "id": "remote",
            "features": {"remote_inference.enabled": True},
        }
        self.assertIn(
            "E_REMOTE_INFERENCE_UNAUTHORIZED",
            codes(jev_manifest.validate_manifest(manifest, profile=profile)),
        )

    def test_remote_inference_enabled_with_consent_and_budget_is_supported(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["consent"] = True
        manifest["credentials"]["remote_inference"]["budget_usd_max"] = 5
        profile = {
            "profile_version": 1,
            "id": "remote",
            "features": {"remote_inference.enabled": True},
        }
        self.assertEqual(jev_manifest.validate_manifest(manifest, profile=profile), [])

    def test_negative_budget_is_rejected(self):
        manifest = load_manifest()
        manifest["credentials"]["remote_inference"]["budget_usd_max"] = -1
        self.assertIn(
            "E_CREDENTIAL_BUDGET", codes(jev_manifest.validate_manifest(manifest))
        )


class ProfileTests(unittest.TestCase):
    def test_profile_cannot_enable_a_feature_without_its_dependency(self):
        manifest = load_manifest()
        profile = {
            "profile_version": 1,
            "id": "views-without-dedup",
            "features": {
                "projection.fabric_views": True,
                "projection.dedup_receipts": False,
            },
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
            self.assertEqual(
                jev_manifest.check_patch_state(manifest, root, "absent"), []
            )
            self.assertIn(
                "E_PATCH_STATE",
                codes(jev_manifest.check_patch_state(manifest, root, "applied")),
            )

            target.write_text("patched\n", encoding="utf-8")
            self.assertEqual(
                jev_manifest.check_patch_state(manifest, root, "applied"), []
            )
            self.assertIn(
                "E_PATCH_STATE",
                codes(jev_manifest.check_patch_state(manifest, root, "absent")),
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
                "E_CHECKOUT_MISMATCH",
                codes(jev_manifest.check_checkout(manifest, root)),
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


class NativeAdapterTests(unittest.TestCase):
    """The declared native source adapter must be present, wired, and guarded."""

    INSTALLED = "codex-rs/core/src/guardian/jev.rs"
    MODULE = "codex-rs/core/src/guardian/mod.rs"
    CALL_SITE = "codex-rs/core/src/guardian/review_request.rs"

    def _write_tree(self, root, installed=True, wired=True):
        spec = component(load_manifest(), "jev-codex-approval")["native_source_adapter"]
        if not installed:
            return spec
        body = "//! ported adapter\n" if wired else "//! unrelated\n"
        if wired:
            body += "\n".join(spec["installed_anchors"]) + "\n"
            # A port is only wired if it binds the answers it accepts, so the
            # fixture performs the same lookups the declaration names.
            body += (
                "\n".join(
                    jev_manifest._answer_binding_lookup(field)
                    for field in spec["answer_binding_fields"]
                )
                + "\n"
            )
        target = root / self.INSTALLED
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        module_anchor = spec["module_declaration"]["anchor"] if wired else "mod other;"
        (root / self.MODULE).write_text(f"{module_anchor}\n", encoding="utf-8")
        call_site = "\n".join(spec["call_site"]["anchors"]) if wired else "review();"
        (root / self.CALL_SITE).write_text(f"{call_site}\n", encoding="utf-8")
        return spec

    def test_shipped_adapter_is_declared_and_wired(self):
        manifest = load_manifest()
        self.assertEqual(jev_manifest.validate_manifest(manifest), [])
        self.assertEqual(
            jev_manifest.check_native_adapter(manifest, REPO_ROOT, "applied"), []
        )

    def test_applied_adapter_requires_every_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_tree(root, wired=False)
            errors = jev_manifest.check_native_adapter(load_manifest(), root, "applied")
            self.assertIn("E_NATIVE_ADAPTER_ANCHOR", codes(errors))
            self._write_tree(root)
            self.assertEqual(
                jev_manifest.check_native_adapter(load_manifest(), root, "applied"),
                [],
            )

    def test_missing_installed_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_tree(root, installed=False)
            errors = jev_manifest.check_native_adapter(load_manifest(), root, "applied")
            self.assertIn("E_NATIVE_ADAPTER_STATE", codes(errors))

    def test_absent_adapter_must_not_carry_any_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_tree(root)
            self.assertIn(
                "E_NATIVE_ADAPTER_STATE",
                codes(
                    jev_manifest.check_native_adapter(load_manifest(), root, "absent")
                ),
            )
            (root / self.INSTALLED).unlink()
            (root / self.MODULE).write_text("mod review;\n", encoding="utf-8")
            (root / self.CALL_SITE).write_text("review();\n", encoding="utf-8")
            self.assertEqual(
                jev_manifest.check_native_adapter(load_manifest(), root, "absent"), []
            )

    def test_guarded_blobs_are_compared_against_the_pinned_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git_init(root)
            for relative in (self.MODULE, self.CALL_SITE):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("guarded\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "guarded base")
            revision = self._git(root, "rev-parse", "HEAD").stdout.strip()
            self._write_tree(root)
            manifest = load_manifest()
            spec = component(manifest, "jev-codex-approval")["native_source_adapter"]
            manifest["host"]["base_commit"] = revision
            spec["guarded_host_revision"] = revision
            spec["guarded_host_blobs"] = {
                path: self._git(root, "rev-parse", f"HEAD:{path}").stdout.strip()
                for path in (self.MODULE, self.CALL_SITE)
            }
            self.assertEqual(
                jev_manifest.check_native_adapter(manifest, root, "applied"), []
            )
            spec["guarded_host_blobs"][self.MODULE] = "0" * 40
            self.assertIn(
                "E_NATIVE_ADAPTER_BLOB",
                codes(jev_manifest.check_native_adapter(manifest, root, "applied")),
            )
            # An unavailable revision is reported as not-checkable, never as a pass.
            spec["guarded_host_revision"] = "1" * 40
            self.assertEqual(
                jev_manifest.check_native_adapter(manifest, root, "applied"), []
            )

    def test_declaration_must_guard_the_pinned_host_revision(self):
        manifest = load_manifest()
        component(manifest, "jev-codex-approval")["native_source_adapter"][
            "guarded_host_revision"
        ] = "1" * 40
        self.assertIn(
            "E_NATIVE_ADAPTER_GUARD_REV",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_declaration_must_name_its_owning_feature(self):
        manifest = load_manifest()
        manifest["features"]["approval.preflight"]["components"] = ["codex-jev"]
        self.assertIn(
            "E_NATIVE_ADAPTER_FEATURE", codes(jev_manifest.validate_manifest(manifest))
        )
        manifest = load_manifest()
        component(manifest, "jev-codex-approval")["native_source_adapter"][
            "feature"
        ] = "sentinel.shadow"
        self.assertIn(
            "E_NATIVE_ADAPTER_FEATURE", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_declaration_must_sit_behind_a_default_off_switch(self):
        manifest = load_manifest()
        manifest["features"]["approval.preflight"]["default"] = True
        self.assertIn(
            "E_NATIVE_ADAPTER_FEATURE", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_declaration_requires_its_provided_interface(self):
        manifest = load_manifest()
        component(manifest, "jev-codex-approval")["provides"] = ["event_envelope"]
        self.assertIn(
            "E_NATIVE_ADAPTER_INTERFACE",
            codes(jev_manifest.validate_manifest(manifest)),
        )
        manifest = load_manifest()
        component(manifest, "jev-codex-approval")["native_source_adapter"][
            "implements"
        ] = "jev_bus"
        self.assertIn(
            "E_NATIVE_ADAPTER_INTERFACE",
            codes(jev_manifest.validate_manifest(manifest)),
        )

    def test_incomplete_declaration_is_rejected(self):
        manifest = load_manifest()
        del component(manifest, "jev-codex-approval")["native_source_adapter"][
            "guarded_host_blobs"
        ]
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_digest_and_path_rules_are_enforced(self):
        manifest = load_manifest()
        spec = component(manifest, "jev-codex-approval")["native_source_adapter"]
        spec["source_sha256"] = "not-a-digest"
        spec["installed"] = "/etc/passwd"
        self.assertIn(
            "E_NATIVE_ADAPTER_HASH", codes(jev_manifest.validate_manifest(manifest))
        )
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_declaration_must_bind_every_answer_field_the_component_checks(self):
        # The component's own transport refuses an answer whose request id,
        # snapshot hash, policy hash or question hash does not match, so a port
        # that binds fewer fields is weaker than the component it ports (#22).
        for field in sorted(jev_manifest.REQUIRED_ANSWER_BINDING_FIELDS):
            with self.subTest(field=field):
                manifest = load_manifest()
                spec = component(manifest, "jev-codex-approval")[
                    "native_source_adapter"
                ]
                spec["answer_binding_fields"] = [
                    declared
                    for declared in spec["answer_binding_fields"]
                    if declared != field
                ]
                self.assertIn(
                    "E_NATIVE_ADAPTER_BINDING",
                    codes(jev_manifest.validate_manifest(manifest)),
                )

    def test_installed_file_must_perform_each_declared_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_tree(root)
            installed = root / self.INSTALLED
            installed.write_text(
                installed.read_text(encoding="utf-8").replace(
                    jev_manifest._answer_binding_lookup("snapshot_hash"), "response.get"
                ),
                encoding="utf-8",
            )
            errors = jev_manifest.check_native_adapter(load_manifest(), root, "applied")
            self.assertIn("E_NATIVE_ADAPTER_BINDING", codes(errors))

    def test_declaration_must_pin_a_wellformed_question_set_digest(self):
        manifest = load_manifest()
        spec = component(manifest, "jev-codex-approval")["native_source_adapter"]
        spec["question_hash"] = "not-a-digest"
        self.assertIn(
            "E_NATIVE_ADAPTER_HASH", codes(jev_manifest.validate_manifest(manifest))
        )
        del spec["question_hash"]
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def test_declaration_must_expose_the_binding_it_requires(self):
        manifest = load_manifest()
        spec = component(manifest, "jev-codex-approval")["native_source_adapter"]
        spec["answer_binding_fields"] = "request_id"
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )
        spec["answer_binding_fields"] = []
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )
        spec["answer_binding_fields"] = ["request_id", "request_id"]
        self.assertIn(
            "E_NATIVE_ADAPTER_SCHEMA", codes(jev_manifest.validate_manifest(manifest))
        )

    def _git_init(self, root):
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "tests@example.invalid")
        self._git(root, "config", "user.name", "Integration Tests")

    def _git(self, root, *args):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        )


if __name__ == "__main__":
    unittest.main()
