#!/usr/bin/env python3
"""Required-CI coverage for binding the JEV context fabric to the isolated home.

The real `jev-context-fabric` installer lives in a separate repository that CI
does not check out, so these tests drive the binding with a checked-in test
double (`jev/tests/fabric_stub/install.py`). They pin the contract the real
binding depends on: every write is redirected into the isolated environment,
unrelated Codex settings survive install and uninstall, reinstall is a no-op,
and a report that escapes the environment fails closed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import fabric_env  # noqa: E402
import isolated_env  # noqa: E402

STUB = REPO_ROOT / "jev" / "tests" / "fabric_stub" / "install.py"
UNRELATED = 'model = "kestrel"\napproval_policy = "on-request"\n'


class FabricBindingTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.env_dir = Path(self.work.name) / "isolated"
        isolated_env.init_env(env_dir=self.env_dir, root=REPO_ROOT)
        self.home = Path(self.env_dir) / "home"
        self.config = self.home / "config.toml"
        self.config.write_text(UNRELATED, encoding="utf-8")

    def install(self, **kwargs):
        return fabric_env.install(
            self.env_dir, self.stub_dir(), root=REPO_ROOT, **kwargs
        )

    def stub_dir(self):
        # `install` takes a fabric root, and the stub lives beside its installer.
        return STUB.parent

    def test_install_stays_inside_the_environment(self):
        record = self.install()
        self.assertTrue(record["contained"])
        self.assertEqual(record["tier"], "real-fabric-installer")
        for path in record["changed_files"]:
            self.assertTrue(
                str(Path(path).resolve()).startswith(str(self.env_dir.resolve())),
                path,
            )
        self.assertTrue(fabric_env.record_path(self.env_dir).is_file())

    def test_install_preserves_unrelated_codex_settings(self):
        self.install()
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('model = "kestrel"', text)
        self.assertIn('approval_policy = "on-request"', text)
        self.assertIn("[mcp_servers.jev-context]", text)
        self.assertEqual(text.count("[mcp_servers.jev-context]"), 1)

    def test_reinstall_reports_no_changes(self):
        self.install()
        second = self.install()
        self.assertEqual(second["changed_files"], [])

    def test_dry_run_writes_no_record_and_no_files(self):
        record = self.install(dry_run=True)
        self.assertTrue(record["dry_run"])
        self.assertFalse(fabric_env.record_path(self.env_dir).is_file())
        self.assertNotIn("[mcp_servers.jev-context]", self.config.read_text("utf-8"))

    def test_report_that_escapes_the_environment_fails_closed(self):
        escaped = {
            "changes": [{"path": str(Path(self.work.name) / "outside.toml")}],
            "harnesses": [],
            "notes": [],
        }
        with mock.patch.object(fabric_env, "run_installer", return_value=escaped):
            with self.assertRaises(fabric_env.FabricError):
                self.install()
        self.assertFalse(fabric_env.record_path(self.env_dir).is_file())

    def test_ensure_contained_accepts_only_the_environment(self):
        inside = self.env_dir / "fabric" / "runner.py"
        fabric_env.ensure_contained([str(inside)], self.env_dir)
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.ensure_contained([str(Path(self.work.name) / "x")], self.env_dir)

    def test_uninstall_restores_settings_and_removes_hooks(self):
        self.install()
        hooks = self.home / "hooks.json"
        self.assertTrue(hooks.is_file())
        result = fabric_env.uninstall(self.env_dir, fabric_root=self.stub_dir())
        self.assertTrue(result["uninstalled"])
        self.assertEqual(result["conflicts"], [])
        self.assertFalse(hooks.is_file())
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn("[mcp_servers.jev-context]", text)
        self.assertIn('model = "kestrel"', text)

    def test_status_without_a_binding_fails_closed(self):
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.status(self.env_dir)

    def test_status_reports_the_bound_surfaces(self):
        self.install()
        report = fabric_env.status(self.env_dir)
        self.assertTrue(report["mcp_server_in_config"])
        self.assertTrue(report["hooks_present"])
        self.assertTrue(report["skill_present"])
        self.assertEqual(
            report["changed_files_present"], report["changed_files_recorded"]
        )
        self.assertEqual(report["profile"], "isolated-offline")
        self.assertTrue(report["runtime"]["interpreter_satisfies_requirement"])
        self.assertEqual(report["runtime"]["harness"], "codex")
        self.assertEqual(report["runtime"]["mcp_server"], "jev-context")

    def test_missing_fabric_installer_fails_closed(self):
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.install(
                self.env_dir, Path(self.work.name) / "absent", root=REPO_ROOT
            )

    def test_verify_requires_an_installed_runner(self):
        self.install()
        record = json.loads(fabric_env.record_path(self.env_dir).read_text("utf-8"))
        record["prefix"] = str(Path(self.work.name) / "empty-prefix")
        fabric_env.record_path(self.env_dir).write_text(
            json.dumps(record), encoding="utf-8"
        )
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.verify(self.env_dir)

    def test_verify_refuses_one_workspace_for_both_sides(self):
        self.install()
        workspace = Path(self.work.name) / "workspace-a"
        workspace.mkdir()
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.verify(
                self.env_dir, workspace_a=workspace, workspace_b=workspace
            )

    def test_workspace_flag_is_not_an_abbreviation(self):
        with self.assertRaises(SystemExit):
            fabric_env.parse_args(
                ["--env-dir", "/tmp/x", "verify", "--workspace", "/tmp/y"]
            )


class FabricPinTests(unittest.TestCase):
    def test_manifest_pins_a_fabric_revision(self):
        manifest = isolated_env.load_manifest(REPO_ROOT)
        pinned = {c["id"]: c for c in manifest["components"]}
        self.assertIn("jev-context-fabric", pinned)
        revision = fabric_env.component_revision(REPO_ROOT)
        self.assertEqual(revision, pinned["jev-context-fabric"]["revision"])
        self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_profile_pins_the_same_fabric_runtime(self):
        profile = isolated_env.load_profile("isolated-offline", REPO_ROOT)
        pin = profile["fabric"]
        component = {
            c["id"]: c for c in isolated_env.load_manifest(REPO_ROOT)["components"]
        }["jev-context-fabric"]
        self.assertEqual(pin["component"], "jev-context-fabric")
        self.assertEqual(pin["revision"], component["revision"])
        self.assertEqual(pin["python_requirement"], component["python_requirement"])
        self.assertEqual(pin["harness"], fabric_env.HARNESS)
        self.assertEqual(pin["mcp_server"], fabric_env.MCP_SERVER_NAME)

    def test_driver_refuses_a_profile_pin_that_drifts(self):
        with tempfile.TemporaryDirectory() as work:
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            with mock.patch.object(
                fabric_env, "component_revision", return_value="0" * 40
            ):
                with self.assertRaises(fabric_env.FabricError):
                    fabric_env.install(env_dir, STUB.parent, root=REPO_ROOT)
            self.assertFalse(fabric_env.record_path(env_dir).is_file())

    def test_python_requirement_is_enforced_for_the_pinned_fabric(self):
        self.assertTrue(fabric_env.satisfies_python_requirement(">=3.11", (3, 11, 2)))
        self.assertTrue(fabric_env.satisfies_python_requirement(">=3.10", (3, 12, 0)))
        self.assertFalse(fabric_env.satisfies_python_requirement(">=3.13", (3, 11, 2)))
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.satisfies_python_requirement("~=3.11", (3, 11, 2))


if __name__ == "__main__":
    unittest.main()
