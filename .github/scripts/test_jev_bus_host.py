#!/usr/bin/env python3
"""Required-CI coverage for the host side of the jev-bus boundary.

The host owns one projection boundary (`jev_bus_call_site` in the manifest):
the outgoing request copy is handed to the repository's adapter once, and the
canonical transcript is never touched. These tests are stdlib-only and never
launch a Codex binary, so they run on every pull request; they pin the half a
future change could silently break:

* the switch names the launcher exports are the ones the native adapter reads,
  so a profile cannot be silently ignored at runtime;
* the adapter's documented CLI, driven over the real subprocess transport with
  a labelled offline-fixture stage, reduces the outgoing view only;
* a disabled switch is byte-identical passthrough, and a stage that declines,
  fails, or times out falls back to that stage's own input.

The tier here is `offline-fixture` plus the real adapter process. It proves host
wiring and chain behaviour, never a component's semantics and never a live
model.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import isolated_env  # noqa: E402
import launch_isolated  # noqa: E402

ADAPTER = REPO_ROOT / "jev" / "scripts" / "bus_boundary.py"
STAGE_FIXTURE = REPO_ROOT / "jev" / "scripts" / "bus_stage_fixture.py"
RUST_ADAPTER = REPO_ROOT / "codex-rs" / "core" / "src" / "jev_bus.rs"

DEDUP_STAGE = f"{sys.executable} {STAGE_FIXTURE} --mode dedup"
VIEW_STAGE = f"{sys.executable} {STAGE_FIXTURE} --mode view"
DECLINE_STAGE = f"{sys.executable} {STAGE_FIXTURE} --mode decline"
FAIL_STAGE = f"{sys.executable} {STAGE_FIXTURE} --mode fail"

# The names the native adapter reads. Every one of them must be a name this
# repository's profiles or launcher controls, or the host reads a variable
# nothing sets.
RUST_ENV_NAMES = {
    "JEV_BUS_ADAPTER",
    "JEV_BUS_PYTHON",
    "JEV_BUS_STAGE_",
    "JEV_BUS_TIMEOUT_MS",
    "JEV_BUS_WORKSPACE",
    "JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS",
    "JEV_SWITCH_PROJECTION_FABRIC_VIEWS",
}


def sample_request():
    """One outgoing request with a repeated read, prose, and an opaque item."""
    return {
        "model": "koffing",
        "instructions": "system",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "read a, then read a"}],
            },
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"a"}',
                "call_id": "call_1",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "FILE A BODY",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "a says hello"}],
            },
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"a"}',
                "call_id": "call_2",
            },
            {
                "type": "function_call_output",
                "call_id": "call_2",
                "output": "FILE A BODY",
            },
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [{"type": "summary_text", "text": "think"}],
                "encrypted_content": None,
            },
        ],
    }


def run_boundary(request, stages, switches):
    """Drive the adapter the way the host does: same argv, stdin, and env."""
    env = dict(os.environ)
    for feature, value in switches.items():
        env[isolated_env.switch_name(feature)] = "1" if value else "0"
    argv = [
        sys.executable,
        str(ADAPTER),
        "apply",
        "--request",
        "-",
        "--json",
        "--session",
        "session-1",
        "--turn",
        "turn-1",
        "--workspace",
        "/workspace",
    ]
    for stage_id, command in stages.items():
        argv += ["--stage", f"{stage_id}={command}"]
    return subprocess.run(
        argv,
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
        env=env,
        check=False,
    )


def outputs(result):
    return json.loads(result.stdout.decode("utf-8"))


class SwitchContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = isolated_env.resolve_plan(root=REPO_ROOT)
        self.switch = isolated_env.switch_env(self.plan)

    def test_switches_carry_the_documented_prefix(self):
        self.assertTrue(self.switch)
        for name in self.switch:
            self.assertTrue(name.startswith("JEV_SWITCH_"), name)
        self.assertEqual(
            set(self.switch),
            {isolated_env.switch_name(feature) for feature in self.plan["features"]},
        )
        for feature, enabled in self.plan["features"].items():
            self.assertEqual(self.switch[isolated_env.switch_name(feature)],
                             "1" if enabled else "0")

    def test_the_projection_switches_are_readable_by_name(self):
        for feature in ("projection.dedup_receipts", "projection.fabric_views"):
            name = isolated_env.switch_name(feature)
            self.assertIn(name, self.switch)
            self.assertIn(name, RUST_ENV_NAMES)

    def test_the_native_adapter_reads_only_contract_names(self):
        source = RUST_ADAPTER.read_text(encoding="utf-8")
        found = set(re.findall(r'"(JEV_[A-Z0-9_]+)"', source))
        self.assertEqual(found, RUST_ENV_NAMES)

    def test_the_launcher_exports_the_boundary_contract(self):
        with tempfile.TemporaryDirectory() as work:
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            plan = isolated_env.read_env(env_dir)
            bus = isolated_env.bus_env(plan)
            self.assertEqual(bus["JEV_BUS_ADAPTER"], str(ADAPTER))
            self.assertTrue(bus["JEV_BUS_PYTHON"])
            self.assertEqual(bus["JEV_BUS_WORKSPACE"], plan["home"])
            result = launch_isolated.run(env_dir, ["exec", "hello"], dry_run=True)
            self.assertEqual(result["bus_env"], bus)
            self.assertEqual(result["switch_env"], isolated_env.switch_env(plan))


class BoundaryChainTests(unittest.TestCase):
    def test_ordered_chain_reduces_the_outgoing_view_only(self):
        request = sample_request()
        canonical = json.loads(json.dumps(request))
        result = run_boundary(
            request,
            {"dedup": DEDUP_STAGE, "fabric_view": VIEW_STAGE},
            {"projection.dedup_receipts": True, "projection.fabric_views": True},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = outputs(result)
        report = payload["report"]
        self.assertEqual(
            report["invoked"], ["jev-prune.dedup", "jev-context-fabric.view"]
        )
        self.assertEqual(
            report["applied"], ["jev-prune.dedup", "jev-context-fabric.view"]
        )
        outgoing = payload["request"]["input"]
        # The earlier duplicate read is replaced; the retained copy is not.
        self.assertTrue(outgoing[2]["output"].startswith("[jev-fixture]"))
        self.assertEqual(outgoing[5]["output"], "FILE A BODY")
        # Assistant prose carries the view marker; the opaque item is untouched.
        self.assertTrue(outgoing[3]["content"][0]["text"].startswith("[jev-fixture view]"))
        self.assertEqual(outgoing[6], canonical["input"][6])
        # Order and membership are preserved, and the source request is not.
        self.assertEqual(len(outgoing), len(canonical["input"]))
        self.assertEqual(request, canonical)

    def test_each_applied_stage_writes_one_receipt(self):
        request = sample_request()
        result = run_boundary(
            request,
            {"dedup": DEDUP_STAGE, "fabric_view": VIEW_STAGE},
            {"projection.dedup_receipts": True, "projection.fabric_views": True},
        )
        receipts = outputs(result)["report"]["receipts"]
        self.assertEqual([receipt["kind"] for receipt in receipts],
                         ["projection_receipt", "projection_receipt"])
        self.assertEqual([receipt["stage"] for receipt in receipts], [100, 200])
        self.assertEqual(
            [receipt["component"] for receipt in receipts],
            ["jev-prune-kit", "jev-context-fabric"],
        )
        self.assertEqual([receipt["session_id"] for receipt in receipts],
                         ["session-1", "session-1"])

    def test_disabled_switches_never_register_a_stage(self):
        request = sample_request()
        result = run_boundary(
            request,
            {"dedup": FAIL_STAGE},
            {"projection.dedup_receipts": False, "projection.fabric_views": False},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = outputs(result)
        self.assertEqual(payload["report"]["invoked"], [])
        self.assertEqual(payload["report"]["receipts"], [])
        self.assertEqual(payload["request"], request)

    def test_a_declining_stage_keeps_its_own_input(self):
        request = sample_request()
        result = run_boundary(
            request,
            {"dedup": DECLINE_STAGE},
            {"projection.dedup_receipts": True, "projection.fabric_views": False},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = outputs(result)
        self.assertEqual(payload["request"], request)
        self.assertEqual(payload["report"]["applied"], [])
        self.assertEqual(
            [note["action"] for note in payload["report"]["notes"]], ["passthrough"]
        )

    def test_a_failing_stage_keeps_its_own_input(self):
        request = sample_request()
        result = run_boundary(
            request,
            {"dedup": FAIL_STAGE},
            {"projection.dedup_receipts": True, "projection.fabric_views": False},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = outputs(result)
        self.assertEqual(payload["request"], request)
        self.assertEqual(payload["report"]["applied"], [])

    def test_views_without_dedup_are_refused(self):
        result = run_boundary(
            sample_request(),
            {"fabric_view": VIEW_STAGE},
            {"projection.dedup_receipts": False, "projection.fabric_views": True},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"E_SWITCH_ORDER", result.stderr)


if __name__ == "__main__":
    unittest.main()
