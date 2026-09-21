#!/usr/bin/env python3
"""Required-CI coverage for the ported native approval adapter (C6 / #21).

The Python suites cannot compile Rust, so they cannot prove the port builds.
What they can prove, and what this file checks on the real tree, is that the
declared port is installed exactly where the manifest says it is, that it keeps
the single module declaration and the single call site the installer creates,
that it reads only the declared switch, and that it adds no permission-mutating
surface to the host. It also proves the port binds the answers it accepts: the
component's own transport refuses an answer whose `request_id`, `snapshot_hash`,
`policy_hash` or `question_hash` does not match what it sent, so a port that
checks less than that is weaker than the component it ports (#22).
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import jev_manifest  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "jev" / "compatibility-manifest.json"

# Tokens that would mean the adapter started deciding permissions itself
# instead of handing the decision to the existing reviewer.
FORBIDDEN_TOKENS = (
    "ApprovalCache",
    "approval_cache",
    "set_approval_policy",
    "set_permissions(",
    ".grant(",
    "permission_profile(",
)


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def adapter_spec(manifest):
    for component in manifest["components"]:
        spec = component.get("native_source_adapter")
        if spec is not None:
            return component["id"], spec
    raise AssertionError("the manifest declares no native source adapter")


def read(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


class NativeAdapterPortTests(unittest.TestCase):
    def test_port_is_applied_and_wired_on_this_tree(self):
        manifest = load_manifest()
        self.assertEqual(jev_manifest.validate_manifest(manifest), [])
        self.assertEqual(
            jev_manifest.check_native_adapter(manifest, REPO_ROOT, "applied"), []
        )

    def test_one_module_declaration_and_one_call_site(self):
        _, spec = adapter_spec(load_manifest())
        module = read(spec["module_declaration"]["file"])
        anchor = spec["module_declaration"]["anchor"]
        declarations = [
            line for line in module.splitlines() if line.strip() == anchor.strip()
        ]
        self.assertEqual(len(declarations), 1, declarations)
        call_site = read(spec["call_site"]["file"])
        self.assertEqual(call_site.count("super::super::jev::review("), 1)

    def test_installed_file_is_ascii(self):
        _, spec = adapter_spec(load_manifest())
        text = read(spec["installed"])
        self.assertEqual(text, text.encode("ascii", "replace").decode("ascii"))

    def test_adapter_adds_no_permission_mutating_surface(self):
        _, spec = adapter_spec(load_manifest())
        text = read(spec["installed"])
        for token in FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_the_only_switch_read_is_a_declared_default_off_feature(self):
        manifest = load_manifest()
        _, spec = adapter_spec(manifest)
        text = read(spec["installed"])
        declared = {
            line.split('"')[1]
            for line in text.splitlines()
            if line.strip().startswith("const PREFLIGHT_SWITCH")
        }
        self.assertEqual(len(declared), 1, declared)
        feature = declared.pop()
        self.assertIn(feature, manifest["features"])
        self.assertIs(manifest["features"][feature]["default"], False)
        # The declared switch key is the only environment contract phases 2-6
        # may read, so the port must spell it exactly as CONTRACTS.md C10 does.
        self.assertIn(
            'format!("JEV_SWITCH_{}", feature.to_uppercase().replace(\'.\', "_"))',
            text,
        )
        self.assertEqual(text.count("std::env::var("), 1)
        self.assertEqual(text.count("std::env::var_os("), 1)

    def test_the_isolated_profile_starts_inert(self):
        profile = json.loads(
            (REPO_ROOT / "jev" / "profiles" / "isolated-offline.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(profile["features"]["approval.preflight"], False)
        self.assertIs(profile["features"]["approval.enforcement"], False)

    def test_the_port_binds_every_answer_field_the_component_checks(self):
        _, spec = adapter_spec(load_manifest())
        text = read(spec["installed"])
        declared = spec["answer_binding_fields"]
        for field in jev_manifest.REQUIRED_ANSWER_BINDING_FIELDS:
            with self.subTest(field=field):
                # The declaration must cover it, and the port must actually read it.
                self.assertIn(field, declared)
                self.assertIn(jev_manifest._answer_binding_lookup(field), text)
        # One lookup per field, so a field cannot be "bound" by an unrelated read.
        for field in declared:
            with self.subTest(lookup=field):
                self.assertEqual(
                    text.count(jev_manifest._answer_binding_lookup(field)), 1
                )

    def test_the_approved_question_set_is_a_pinned_digest(self):
        manifest = load_manifest()
        _, spec = adapter_spec(manifest)
        text = read(spec["installed"])
        self.assertRegex(spec["question_hash"], jev_manifest.SHA256_RE)
        # The host cannot recompute a digest over question text it does not carry,
        # so the declared pin and the compiled constant must be the same value.
        self.assertIn(f'"{spec["question_hash"]}"', text)
        self.assertIn("const APPROVED_QUESTION_HASH", text)

    def test_the_canonical_form_the_digests_use_is_declared(self):
        _, spec = adapter_spec(load_manifest())
        text = read(spec["installed"])
        # The engine hashes `json.dumps(..., sort_keys=True, separators=(',',':'))`
        # bytes, so the port must canonicalize keys and must not rely on the order
        # or spacing of the JSON it happens to serialize.
        self.assertIn("fn canonical_json(value: &Value, out: &mut String)", text)
        self.assertIn("keys.sort_unstable();", text)
        self.assertIn("sha2::Sha256", text)
        # A digest is compared, never fabricated or defaulted to something that matches.
        self.assertIn("expected_snapshot_hash", text)
        self.assertIn("expected_policy_hash", text)


if __name__ == "__main__":
    unittest.main()
