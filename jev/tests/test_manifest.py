"""Offline tests for the compatibility manifest and its refusal rules.

These tests run against fixtures and a temporary manifest; they exercise the
resolution logic, not a running Codex host or a live provider. Passing here
certifies the manifest's shape and failure behaviour only.
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "jev" / "tools" / "verify_manifest.py"
MANIFEST_PATH = REPO_ROOT / "jev" / "manifest.json"
EVENTS_PATH = REPO_ROOT / "jev" / "contracts" / "events.v1.json"
ARCHITECTURE_PATH = REPO_ROOT / "jev" / "contracts" / "architecture.md"


def load_tool():
    spec = importlib.util.spec_from_file_location("verify_manifest", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def request(overrides: dict) -> dict:
    data = manifest()
    base = {
        "manifest": data,
        "flags": TOOL.default_flags(data),
        "env": {},
        "state": TOOL.default_state(),
        "provider": "openai",
        "platform": "linux-x86_64-gnu",
        "python_version": "3.11.2",
        "host_revision": data["host"]["revision"],
        "component_revisions": {},
    }
    if "flags" in overrides:
        merged = dict(base["flags"])
        merged.update(overrides["flags"])
        overrides = dict(overrides, flags=merged)
    base.update(overrides)
    return base


def violations(**overrides) -> list:
    return TOOL.evaluate(manifest(), request(overrides))


class PinnedResolutionTests(unittest.TestCase):
    def test_defaults_resolve(self):
        self.assertEqual(violations(), [])

    def test_every_switch_defaults_to_disabled(self):
        for switch_id, value in TOOL.default_flags(manifest()).items():
            self.assertIn(
                value, (False, "off"), "{} defaults to {}".format(switch_id, value)
            )

    def test_component_pins_are_full_revisions(self):
        for component in manifest()["components"]:
            revision = component["revision"]
            self.assertRegex(revision, r"^[0-9a-f]{40}$", component["name"])

    def test_patch_order_is_strictly_increasing(self):
        orders = [entry["order"] for entry in manifest()["patch_order"]]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(orders), len(set(orders)))

    def test_declared_patch_targets_exist(self):
        for component in manifest()["components"]:
            patch = component.get("patch")
            if not patch:
                continue
            for relative in patch["touches"]:
                self.assertTrue((REPO_ROOT / relative).is_file(), relative)


class RefusalTests(unittest.TestCase):
    def test_host_revision_drift_is_refused(self):
        found = violations(host_revision="0" * 40)
        self.assertTrue(any("host revision mismatch" in item for item in found), found)

    def test_component_revision_drift_is_refused(self):
        found = violations(
            component_revisions={"jev-sentinel": "0" * 40},
        )
        self.assertTrue(any("jev-sentinel" in item for item in found), found)

    def test_unknown_component_is_refused(self):
        found = violations(component_revisions={"jev-unknown": "1" * 40})
        self.assertTrue(any("unknown component" in item for item in found), found)

    def test_excluded_component_is_refused(self):
        found = violations(component_revisions={"omniroute-codex-docker": "1" * 40})
        self.assertTrue(any("excluded component" in item for item in found), found)

    def test_omniroute_provider_is_refused(self):
        found = violations(provider="omniroute")
        self.assertTrue(any("omniroute-provider" in item for item in found), found)

    def test_view_without_dedup_is_refused(self):
        found = violations(
            flags={
                "jev.enabled": True,
                "jev.projection.enabled": True,
                "jev.projection.view.enabled": True,
            }
        )
        self.assertTrue(any("view-without-dedup" in item for item in found), found)

    def test_view_with_dedup_is_allowed(self):
        found = violations(
            flags={
                "jev.enabled": True,
                "jev.projection.enabled": True,
                "jev.projection.dedup.enabled": True,
                "jev.projection.view.enabled": True,
            }
        )
        self.assertEqual(found, [])

    def test_projection_without_master_switch_is_refused(self):
        found = violations(flags={"jev.projection.enabled": True})
        self.assertTrue(
            any(
                "jev.projection.enabled is enabled but its requirement" in item
                for item in found
            ),
            found,
        )

    def test_remote_inference_without_consent_is_refused(self):
        found = violations(flags={"jev.remote.inference.enabled": True})
        self.assertTrue(any("remote-without-consent" in item for item in found), found)

    def test_remote_inference_with_consent_is_allowed(self):
        found = violations(
            flags={"jev.remote.inference.enabled": True},
            env={"JEV_REMOTE_CONSENT": "1"},
        )
        self.assertEqual(found, [])

    def test_approval_enforcement_without_shadow_evidence_is_refused(self):
        found = violations(flags={"jev.approval.mode": "enforce"})
        self.assertTrue(
            any("approval-enforce-without-evidence" in item for item in found), found
        )

    def test_approval_enforcement_with_shadow_evidence_is_allowed(self):
        found = violations(
            flags={"jev.approval.mode": "enforce"},
            state={"jev.approval.shadow_evaluated": True},
        )
        self.assertEqual(found, [])

    def test_sentinel_enforcement_without_coverage_is_refused(self):
        found = violations(flags={"jev.sentinel.mode": "enforce"})
        self.assertTrue(
            any("sentinel-enforce-without-coverage" in item for item in found), found
        )

    def test_unknown_sentinel_mode_is_refused(self):
        found = violations(flags={"jev.sentinel.mode": "aggressive"})
        self.assertTrue(any("is not one of" in item for item in found), found)

    def test_old_python_is_refused(self):
        found = violations(python_version="3.10.4")
        self.assertTrue(any("python 3.10.4" in item for item in found), found)
        self.assertTrue(any("runtime-too-old" in item for item in found), found)

    def test_unsupported_platform_is_refused(self):
        found = violations(platform="plan9")
        self.assertTrue(any("unsupported-platform" in item for item in found), found)

    def test_documented_but_unvalidated_platform_resolves_with_its_status(self):
        found = violations(platform="darwin-arm64")
        self.assertEqual(found, [])
        statuses = {
            entry["id"]: entry["status"] for entry in manifest()["supported_platforms"]
        }
        self.assertEqual(statuses["darwin-arm64"], "documented-unvalidated")
        self.assertEqual(statuses["linux-x86_64-gnu"], "validated")


class ExitCodeTests(unittest.TestCase):
    def test_ok_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TOOL.main(["--checkout", str(REPO_ROOT), "--json"]), 0)

    def test_refusal_exit_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                TOOL.main(["--provider", "omniroute"]), TOOL.EXIT_UNSUPPORTED
            )

    def test_manifest_error_exit_code(self):
        missing = pathlib.Path("/nonexistent/jev/manifest.json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                TOOL.main(["--manifest", str(missing)]), TOOL.EXIT_MANIFEST_ERROR
            )

    def test_unknown_switch_is_a_manifest_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                TOOL.main(["--enable", "jev.not-a-switch=true"]),
                TOOL.EXIT_MANIFEST_ERROR,
            )

    def test_non_boolean_value_for_a_boolean_switch_is_a_manifest_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                TOOL.main(["--enable", "jev.enabled=perhaps"]), TOOL.EXIT_MANIFEST_ERROR
            )


class ComponentCheckoutTests(unittest.TestCase):
    """Resolve installed component checkouts when they are present on this machine."""

    COMPONENTS_ROOT = pathlib.Path("/home/agent/jev/work")

    def test_installed_components_match_their_pins(self):
        data = manifest()
        present = {
            component["name"]: self.COMPONENTS_ROOT / component["name"]
            for component in data["components"]
            if (self.COMPONENTS_ROOT / component["name"]).is_dir()
        }
        if not present:
            self.skipTest(
                "no component checkouts available at {}".format(self.COMPONENTS_ROOT)
            )
        revisions = {name: TOOL.git_revision(path) for name, path in present.items()}
        self.assertEqual(
            TOOL.evaluate(data, request({"component_revisions": revisions})), []
        )


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        self.data = manifest()

    def test_stage_lists_agree(self):
        self.assertEqual(self.events["stages"], self.data["event_contract"]["stages"])

    def test_sources_match_components(self):
        self.assertEqual(
            sorted(self.events["sources"]),
            sorted(["codex-jev"] + [c["name"] for c in self.data["components"]]),
        )

    def test_identity_derivation_is_shared(self):
        self.assertEqual(
            self.events["identity_derivation"],
            self.data["event_contract"]["identity"]["derivation"],
        )
        self.assertEqual(
            self.events["dedup_key"],
            self.data["event_contract"]["identity"]["dedup_key"],
        )

    def test_every_stage_is_stated_once(self):
        self.assertEqual(len(self.events["stages"]), len(set(self.events["stages"])))

    def test_required_fields_are_present_in_the_narrative_contract(self):
        narrative = (
            REPO_ROOT / "jev" / "contracts" / "event-identifiers.md"
        ).read_text(encoding="utf-8")
        for field in self.events["required_fields"]:
            self.assertIn("`{}`".format(field), narrative)


class DocumentedExclusionTests(unittest.TestCase):
    def test_architecture_excludes_omniroute(self):
        text = ARCHITECTURE_PATH.read_text(encoding="utf-8")
        self.assertIn("omniroute-codex-docker", text)
        self.assertIn("Excluded", text)

    def test_manifest_records_the_exclusion(self):
        excluded = manifest()["excluded"]
        self.assertEqual([entry["id"] for entry in excluded], ["omniroute"])
        self.assertEqual(
            [entry["behavior"] for entry in excluded], ["explicit-failure"]
        )

    def test_no_manifest_file_references_the_excluded_repository_as_a_dependency(self):
        for component in manifest()["components"]:
            self.assertNotIn("omniroute", component["repository"])


if __name__ == "__main__":
    unittest.main()
