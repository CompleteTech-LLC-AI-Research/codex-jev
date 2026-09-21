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
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import fabric_env  # noqa: E402
import isolated_env  # noqa: E402

STUB = REPO_ROOT / "jev" / "tests" / "fabric_stub" / "install.py"
UNRELATED = 'model = "kestrel"\napproval_policy = "on-request"\n'


def load_stub():
    """Import the checked-in fabric stub to read the groups it installs."""
    spec = spec_from_file_location("jev_fabric_stub_install", STUB)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STUB_HOOKS = load_stub().FABRIC_HOOKS


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


class CaptureBindingTests(unittest.TestCase):
    """The host capture adapter is bound ahead of the fabric's own hooks."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.env_dir = Path(self.work.name) / "isolated"
        isolated_env.init_env(env_dir=self.env_dir, root=REPO_ROOT)
        self.home = Path(self.env_dir) / "home"
        (self.home / "config.toml").write_text(UNRELATED, encoding="utf-8")
        self.hooks = self.home / "hooks.json"

    def install(self, **kwargs):
        return fabric_env.install(self.env_dir, STUB.parent, root=REPO_ROOT, **kwargs)

    def hooks_document(self):
        return json.loads(self.hooks.read_text(encoding="utf-8"))["hooks"]

    def groups_for(self, event):
        return self.hooks_document().get(event, [])

    def is_capture_group(self, group):
        return any(
            fabric_env.CAPTURE_HOOK_NAME in str(handler.get("command", ""))
            for handler in group.get("hooks", [])
        )

    def test_capture_is_bound_ahead_of_the_fabric_hooks(self):
        self.install()
        for event in fabric_env.capture_events():
            with self.subTest(event=event):
                groups = self.groups_for(event)
                self.assertTrue(groups, event)
                self.assertTrue(self.is_capture_group(groups[0]), groups)
                self.assertEqual(
                    sum(1 for group in groups if self.is_capture_group(group)), 1
                )
        # The fabric's own groups are preserved exactly, just ordered after.
        for event, groups in STUB_HOOKS.items():
            with self.subTest(event=event):
                for group in groups:
                    self.assertIn(group, self.groups_for(event))
        self.assertTrue(self.is_capture_group(self.groups_for("PostToolUse")[0]))
        self.assertIn(STUB_HOOKS["PostToolUse"][0], self.groups_for("PostToolUse")[1:])

    def test_reinstall_keeps_one_capture_group_per_event(self):
        self.install()
        before = self.hooks.read_bytes()
        record = self.install()
        self.assertEqual(record["changed_files"], [])
        self.assertEqual(self.hooks.read_bytes(), before)
        for event in fabric_env.capture_events():
            groups = self.groups_for(event)
            self.assertEqual(
                sum(1 for group in groups if self.is_capture_group(group)), 1, event
            )

    def test_unbind_removes_only_the_capture_groups(self):
        self.install()
        plan = isolated_env.read_env(self.env_dir)
        removed = fabric_env.unbind_capture_hooks(plan)
        self.assertEqual(removed["events"], sorted(fabric_env.capture_events()))
        for event in fabric_env.capture_events():
            with self.subTest(event=event):
                self.assertFalse(
                    any(
                        self.is_capture_group(group) for group in self.groups_for(event)
                    ),
                    event,
                )
        for event, groups in STUB_HOOKS.items():
            with self.subTest(event=event):
                for group in groups:
                    self.assertIn(group, self.groups_for(event))
        self.assertEqual(fabric_env.unbind_capture_hooks(plan)["events"], [])

    def test_foreign_hooks_json_fails_closed_without_clobbering(self):
        for name, foreign in (
            ("not json", b"configuration\n"),
            ("not an object", b"[1, 2, 3]\n"),
        ):
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as work:
                    env_dir = Path(work) / "isolated"
                    isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
                    hooks = Path(env_dir) / "home" / "hooks.json"
                    hooks.write_bytes(foreign)
                    with self.assertRaises(fabric_env.FabricError):
                        fabric_env.install(env_dir, STUB.parent, root=REPO_ROOT)
                    self.assertEqual(hooks.read_bytes(), foreign)
                    self.assertFalse(fabric_env.record_path(env_dir).is_file())

    def test_bind_refuses_a_non_object_hooks_entry(self):
        self.hooks.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        plan = isolated_env.read_env(self.env_dir)
        with self.assertRaises(fabric_env.FabricError):
            fabric_env.bind_capture_hooks(plan)

    def test_status_reports_the_capture_binding(self):
        self.install()
        capture = fabric_env.status(self.env_dir)["capture"]
        self.assertIsNone(capture["hooks_error"])
        self.assertEqual(
            capture["hooks_bound"], [True] * len(fabric_env.capture_events())
        )
        self.assertEqual(capture["events"], list(fabric_env.capture_events()))

    def test_status_reports_a_foreign_hooks_json_instead_of_failing(self):
        self.install()
        self.hooks.write_text("configuration\n", encoding="utf-8")
        capture = fabric_env.status(self.env_dir)["capture"]
        self.assertIn("not valid JSON", capture["hooks_error"])
        self.assertEqual(
            capture["hooks_bound"], [False] * len(fabric_env.capture_events())
        )

    def test_bound_command_writes_inside_the_environment(self):
        self.install()
        handler = self.groups_for("UserPromptSubmit")[0]["hooks"][0]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("JEV_CAPTURE_DIR", "CODEX_HOME")
        }
        environment["CODEX_HOME"] = str(self.home)
        completed = subprocess.run(
            shlex.split(handler["command"]),
            input=json.dumps(
                {
                    "session_id": "sess-bound",
                    "turn_id": "turn-bound",
                    "cwd": str(self.work.name),
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "bound command prompt",
                }
            ),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        log = self.home / "capture" / "events.jsonl"
        self.assertTrue(log.is_file())
        body = [
            json.loads(line)
            for line in log.read_text("utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual([item["envelope"]["kind"] for item in body], ["user_message"])
        self.assertTrue(str(log.resolve()).startswith(str(self.env_dir.resolve())))

    def test_capture_verify_proves_order_dedup_and_correlation(self):
        self.install()
        plan = isolated_env.read_env(self.env_dir)
        report = fabric_env.capture_verify(plan, Path(self.work.name))
        self.assertEqual(
            report["captured_kinds"],
            ["user_message", "tool_call", "tool_result", "assistant_message"],
        )
        self.assertTrue(report["replay_stored_no_duplicate"])
        self.assertGreaterEqual(report["parents_resolved"], 2)
        self.assertEqual(report["capture_gaps"], [])
        self.assertEqual(report["stream_findings"], [])


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
