#!/usr/bin/env python3
"""Real-component tests for the Codex Sentinel boundary (#18).

The required-CI suite drives the host boundary with a stub component and with
translation goldens. These tests drive the *pinned* ``jev-sentinel`` checkout
through ``launch.py``, so the evaluator, its policy handling, and its audit store
are the real ones. Tier: ``real-component`` (local execution, no network; the
policy is created per test as ``backend=local``, ``mode=shadow`` unless a test
asks for enforcement).

When no checkout is resolvable the whole module skips with the reason, rather
than passing quietly: an unproven boundary must not look verified.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402

GOLDEN = (
    Path(__file__).resolve().parent / "sentinel_fixtures" / "codex-translation.json"
)
DEFAULT_CHECKOUTS = Path("/home/agent/jev/checkouts")


def find_component():
    for candidate in (
        os.environ.get("JEV_SENTINEL_ROOT", ""),
        str(DEFAULT_CHECKOUTS / "jev-sentinel"),
    ):
        if candidate and (Path(candidate) / "launch.py").is_file():
            return Path(candidate)
    return None


COMPONENT = find_component()


@unittest.skipUnless(
    COMPONENT,
    "no pinned jev-sentinel checkout; set JEV_SENTINEL_ROOT to run the real-component tier",
)
class RealComponentTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(
            json.dumps({"schema_version": 1, "mode": "shadow", "backend": "local"}),
            encoding="utf-8",
        )
        self.identity = {
            "profile": "codex-real",
            "session_id": "real-session-1",
            "turn_id": "real-turn-1",
            "tool_call_id": "",
            "workspace": "/workspace",
            "parent_event_id": "",
        }
        self.profile_root = self.root / "profile"
        self.profile_root.mkdir()

    def tearDown(self):
        self.work.cleanup()

    def policy(self, mode="shadow"):
        self.policy_path.write_text(
            json.dumps({"schema_version": 1, "mode": mode, "backend": "local"}),
            encoding="utf-8",
        )
        return sb.load_policy(self.policy_path)

    def write_hooks(self, *, events=None):
        hooks = {}
        for event_name, stage in adapter.EVENTS.items():
            if events is not None and stage not in events:
                continue
            argv = [
                sys.executable,
                "-I",
                str(COMPONENT / "launch.py"),
                "hook",
                "--policy",
                str(self.policy_path),
                "--harness",
                "codex",
                "--event",
                event_name,
                "--profile",
                self.identity["profile"],
            ]
            entry = {
                "hooks": [
                    {"type": "command", "command": shlex.join(argv), "timeout": 12}
                ]
            }
            if stage != "ingress":
                entry["matcher"] = ".*"
            hooks[event_name] = [entry]
        (self.profile_root / "hooks.json").write_text(
            json.dumps({"hooks": hooks}, indent=2), encoding="utf-8"
        )

    def run_canary(self, mode="shadow"):
        return sb.canary(
            component=COMPONENT,
            policy_path=self.policy_path,
            policy=self.policy(mode),
            identity=self.identity,
            state_dir=self.root,
            enabled=True,
        )


class RealTranslationTests(RealComponentTestCase):
    """The goldens must still describe the checkout that is pinned."""

    def test_checkout_is_the_pinned_revision(self):
        self.assertEqual(adapter.REVISION, sb.component_revision(COMPONENT))

    def test_goldens_match_the_live_component(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for case in golden["normalize"]:
            self.assertEqual(
                case["result"],
                adapter.normalize(case["event"], case["payload"], case["profile"]),
                case["event"],
            )
        for case in golden["render"]:
            self.assertEqual(
                case["result"],
                adapter.render(case["event"], case["verdict"], case["raw"]),
            )


class RealCanaryTests(RealComponentTestCase):
    def test_shadow_canary_is_recorded_and_never_vetoes(self):
        result = self.run_canary()
        self.assertTrue(result["complete"])
        for canary in result["canaries"]:
            self.assertEqual("BLOCK", canary["decision"])
            self.assertEqual(["installation_test_canary"], canary["reason_codes"])
            self.assertFalse(canary["enforced"])
            self.assertFalse(canary["host_vetoed"])
        records = sb.read_incidents(sb.incident_path(self.root))
        self.assertEqual(3, len(records))
        self.assertEqual(1, len({record["session_id"] for record in records}))
        self.assertEqual(1, len({record["turn_id"] for record in records}))

    def test_enforce_canary_vetoes_each_stage(self):
        result = self.run_canary(mode="enforce")
        responses = {
            canary["stage"]: canary["host_response_keys"]
            for canary in result["canaries"]
        }
        self.assertEqual(["decision", "reason"], responses["ingress"])
        self.assertEqual(["hookSpecificOutput"], responses["tool_before"])
        self.assertEqual(
            ["decision", "hookSpecificOutput", "reason"], responses["tool_after"]
        )

    def test_oversize_prompt_refuses_before_the_component_runs(self):
        observed = sb.observe(
            {"prompt": "z" * 70000},
            event_name="UserPromptSubmit",
            profile=self.identity["profile"],
            identity=self.identity,
            component=COMPONENT,
            policy_path=self.policy_path,
            policy=self.policy(),
            enforced=False,
            state_dir=self.root,
        )
        self.assertEqual(sb.E_PAYLOAD_BOUND, observed["refused"])
        self.assertEqual(
            "host_payload_bound", observed["incident"]["sentinel"]["reason_codes"][0]
        )
        self.assertEqual("REVIEW", observed["incident"]["sentinel"]["decision"])


class RealCoverageTests(RealComponentTestCase):
    def report(self, *, probe=True, nonce="real-1"):
        return sb.coverage_report(
            manifest=sb.load_manifest(),
            component=COMPONENT,
            profile_root=self.profile_root,
            policy_path=self.policy_path,
            hooks_path=None,
            switches={"sentinel.shadow": True, "sentinel.enforcement": False},
            probe=probe,
            nonce=nonce,
        )

    def test_probe_finds_a_correlated_audit_row_per_path(self):
        self.write_hooks()
        report = self.report()
        self.assertTrue(report["component"]["revision_match"])
        self.assertTrue(report["activation"]["activated"])
        self.assertEqual("probe_canary", report["activation"]["basis"])
        for stage, item in report["activation"]["evidence"].items():
            self.assertGreaterEqual(item["audit_rows"], 1, stage)
            self.assertEqual(["installation_test_canary"], item["reason_codes"], stage)
            self.assertEqual([], item["responses"][0]["replacement_fields"], stage)

    def test_installation_alone_is_not_activation(self):
        self.write_hooks()
        report = self.report(probe=False)
        self.assertFalse(report["activation"]["activated"])
        self.assertEqual(
            ["ingress", "tool_after", "tool_before"], report["wired_stages"]
        )

    def test_uncovered_path_is_named(self):
        self.write_hooks(events={"ingress"})
        report = self.report(probe=False)
        ids = [surface["id"] for surface in report["bypass_surfaces"]]
        self.assertIn("hook_not_wired.tool_before", ids)
        self.assertIn("hook_not_wired.tool_after", ids)
        self.assertFalse(report["activation"]["activated"])


class RealCarrierTests(RealComponentTestCase):
    """#61: the wired carrier drives the real component and the host journal."""

    def install(self, *, remove=False):
        return sb.install_hooks(
            self.profile_root,
            profile=self.identity["profile"],
            policy_path=self.policy_path,
            state_dir=self.root,
            component=COMPONENT,
            switches={"sentinel.shadow": True, "sentinel.enforcement": False},
            python=sys.executable,
            remove=remove,
        )

    def carrier_command(self, event_name):
        document = json.loads(
            (self.profile_root / "hooks.json").read_text(encoding="utf-8")
        )
        for entry in document["hooks"][event_name]:
            for item in entry["hooks"]:
                if sb.is_carrier_command(item["command"]):
                    return item["command"]
        self.fail(f"no carrier wired for {event_name}")

    def run_carrier(self, event_name, payload, *, enforce=True):
        active = dict(os.environ)
        active.pop("JEV_SENTINEL_ROOT", None)
        active.pop("JEV_COMPONENTS_ROOT", None)
        active["JEV_SWITCH_SENTINEL_SHADOW"] = "1"
        active["JEV_SWITCH_SENTINEL_ENFORCEMENT"] = "1" if enforce else "0"
        return subprocess.run(
            shlex.split(self.carrier_command(event_name)),
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=30,
            env=active,
        )

    def test_install_wires_the_carrier_and_keeps_removal_reversible(self):
        self.install()
        for event_name in adapter.EVENTS:
            self.assertTrue(
                sb.is_carrier_command(self.carrier_command(event_name)), event_name
            )
        record = self.install(remove=True)
        self.assertEqual(3, record["removed"])
        document = json.loads(
            (self.profile_root / "hooks.json").read_text(encoding="utf-8")
        )
        for event_name in adapter.EVENTS:
            self.assertNotIn(event_name, document["hooks"], event_name)

    def test_the_carrier_vetoes_and_records_both_journals(self):
        self.install()
        self.policy("enforce")
        result = self.run_carrier(
            "PreToolUse",
            {
                "session_id": "real-carrier-1",
                "tool_name": "shell",
                "tool_input": {"cmd": adapter.CANARY},
            },
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        response = json.loads(result.stdout)
        self.assertEqual("deny", response["hookSpecificOutput"]["permissionDecision"])
        incidents = sb.read_incidents(sb.incident_path(self.root))
        self.assertEqual(1, len(incidents))
        self.assertEqual(
            ["installation_test_canary"], incidents[0]["sentinel"]["reason_codes"]
        )
        rows = sb.outbox(COMPONENT, self.policy_path)
        self.assertEqual(1, len(rows), "the component wrote its own audit row")
        self.assertEqual(
            incidents[0]["sentinel"]["content_sha256"], rows[0]["content_sha256"]
        )
        self.assertEqual(
            incidents[0]["sentinel"]["session_ref"], rows[0]["session_ref"]
        )

    def test_a_shadow_carrier_records_but_returns_no_judgement(self):
        self.install()
        result = self.run_carrier(
            "PreToolUse",
            {
                "session_id": "real-carrier-2",
                "tool_name": "shell",
                "tool_input": {"cmd": adapter.CANARY},
            },
            enforce=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual({}, json.loads(result.stdout))
        self.assertEqual(1, len(sb.read_incidents(sb.incident_path(self.root))))

    def test_an_oversize_payload_through_the_carrier_never_reaches_the_component(self):
        self.install()
        self.policy("enforce")
        result = self.run_carrier(
            "UserPromptSubmit", {"prompt": "z" * (adapter.MAX_INPUT + 1)}
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual("block", json.loads(result.stdout)["decision"])
        self.assertEqual([], sb.read_incidents(sb.incident_path(self.root)))
        self.assertEqual([], sb.outbox(COMPONENT, self.policy_path))

    def test_the_probe_activates_only_with_the_correlated_host_incident(self):
        self.install()
        report = sb.coverage_report(
            manifest=sb.load_manifest(),
            component=COMPONENT,
            profile_root=self.profile_root,
            policy_path=self.policy_path,
            hooks_path=None,
            switches={"sentinel.shadow": True, "sentinel.enforcement": False},
            probe=True,
            nonce="real-carrier",
        )
        self.assertTrue(report["activation"]["activated"])
        self.assertEqual("probe_canary", report["activation"]["basis"])
        for stage, item in report["activation"]["evidence"].items():
            self.assertTrue(item["host_carrier"], stage)
            self.assertGreaterEqual(item["audit_rows"], 1, stage)
            self.assertGreaterEqual(item["host_incidents"], 1, stage)
            self.assertEqual(1, len(item["host_incident_event_ids"]), stage)
