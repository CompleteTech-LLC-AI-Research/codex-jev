#!/usr/bin/env python3
"""Required-CI coverage for the Codex Sentinel boundary (#18).

These tests pin the contract issue #18 asks for: prompt, pre-tool, and post-tool
events reach the evaluator as bounded normalized payloads; the integration starts
in local shadow mode and reports exactly which tools and event paths are covered;
events carry host identity so a running session produces correlated incidents for
deterministic canaries; and installation alone is never reported as activation.

Two evidence tiers are combined here, both offline:

* ``component-fixture`` - the vendored envelope translation is compared against
  goldens captured from the pinned ``jev-sentinel`` revision.
* ``component-stub`` - a tiny ``launch.py`` implements only the component's
  documented wire (``hook``/``check``/``outbox``) so the host boundary can be
  driven without the component checkout or a network. The stub is not a
  detection oracle; it exists to prove the host's own bookkeeping.

The real-component run is in ``jev/tests/test_sentinel_boundary.py``.
"""

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402

GOLDEN = REPO_ROOT / "jev" / "tests" / "sentinel_fixtures" / "codex-translation.json"

# A stand-in for the pinned component. It implements only the documented wire
# (hook/check/outbox) and the two digests the component's audit row carries.
STUB_LAUNCH = '''
import hashlib, json, sys
from pathlib import Path

EVENTS = {"UserPromptSubmit": "ingress", "PreToolUse": "tool_before", "PostToolUse": "tool_after"}
CANARY = "JEV_SENTINEL_TEST_BLOCK"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def arg(name, default=""):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def content_of(stage, data):
    if stage == "ingress":
        return data.get("prompt", "")
    if stage == "tool_after":
        for key in ("tool_response", "toolResult", "tool_output", "tool_result"):
            if key in data:
                value = data[key]
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        return ""
    return ""


def main():
    command = sys.argv[1]
    policy_path = Path(arg("--policy", str(Path.home() / ".jev-sentinel/policy.json")))
    policy = json.loads(policy_path.read_text()) if policy_path.is_file() else {}
    audit = policy_path.parent / "stub-audit.jsonl"
    if command == "outbox":
        rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()] if audit.is_file() else []
        print(json.dumps(rows[: int(arg("--limit", "20"))]))
        return 0
    raw = json.loads(sys.stdin.read() or "{}")
    if command == "check":
        stage, harness, profile = raw["stage"], raw["harness"], raw["profile"]
        content, args, session_id = raw["content"], raw["tool_input"], raw["session_id"]
        event_name = stage
    else:
        event_name = arg("--event")
        harness, profile = arg("--harness", "codex"), arg("--profile", "default")
        stage = EVENTS[event_name]
        content = content_of(stage, raw)
        args = raw.get("tool_input", raw.get("toolArgs", {})) or {}
        if isinstance(args, str):
            args = json.loads(args)
        session_id = raw.get("session_id", "")
    hits = [CANARY] if CANARY in content + "\\n" + canonical(args) else []
    decision, enforced = ("BLOCK" if hits else "DEFER"), policy.get("mode", "shadow") == "enforce"
    verdict = {
        "id": hashlib.sha256((event_name + session_id + content).encode()).hexdigest()[:32],
        "decision": decision, "enforced": enforced, "reason_codes": ["installation_test_canary"] if hits else [],
        "route": "administrator" if hits else "normal", "backend": policy.get("backend", "local"),
        "message": "JEV Sentinel: stub verdict.",
        "session_ref": hashlib.sha256(canonical([harness, profile, session_id]).encode()).hexdigest() if session_id else "",
    }
    if command == "check":
        print(json.dumps(verdict))
        return 0
    audit.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": verdict["id"], "decision": decision, "enforced": enforced, "reason_codes": verdict["reason_codes"],
        "route": verdict["route"], "backend": verdict["backend"], "session_ref": verdict["session_ref"],
        "stage": stage, "harness": harness, "tool_name": raw.get("tool_name", ""),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "action_sha256": hashlib.sha256(canonical(args).encode()).hexdigest(),
    }
    with audit.open("a") as handle:
        handle.write(json.dumps(row) + "\\n")
    veto = enforced and decision != "DEFER"
    reason = verdict["message"] + " Event: " + verdict["id"]
    if not veto:
        out = {}
    elif stage == "ingress":
        out = {"decision": "block", "reason": reason}
    elif stage == "tool_before":
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
    else:
        out = {"decision": "block", "reason": reason, "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason}}
    print(json.dumps(out))
    return 0


sys.exit(main())
'''


class SentinelTestCase(unittest.TestCase):
    """One stub component and profile per test, all inside a temp directory."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.component = self.root / "component"
        self.component.mkdir()
        (self.component / "launch.py").write_text(STUB_LAUNCH, encoding="utf-8")
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy_path = self.state / "policy.json"
        self.policy_path.write_text(
            json.dumps({"schema_version": 1, "mode": "shadow", "backend": "local"}), encoding="utf-8"
        )
        self.profile_root = self.root / "profile"
        self.profile_root.mkdir()
        self.identity = {
            "profile": "codex-stub",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_call_id": "",
            "workspace": "/workspace",
            "parent_event_id": "",
        }

    def tearDown(self):
        self.work.cleanup()

    def write_hooks(self, *, launcher=None, disable=False, matcher=".*", events=None):
        launcher = launcher or self.component / "launch.py"
        hooks = {}
        for event_name, stage in adapter.EVENTS.items():
            if events is not None and stage not in events:
                continue
            argv = [
                sys.executable,
                "-I",
                str(launcher),
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
            entry = {"hooks": [{"type": "command", "command": shlex.join(argv), "timeout": 12}]}
            if stage != "ingress":
                entry["matcher"] = matcher
            hooks[event_name] = [entry]
        document = {"hooks": hooks}
        if disable:
            document["disableAllHooks"] = True
        path = self.profile_root / "hooks.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def policy(self, mode="shadow"):
        self.policy_path.write_text(
            json.dumps({"schema_version": 1, "mode": mode, "backend": "local"}), encoding="utf-8"
        )
        return sb.load_policy(self.policy_path)

    def observe(self, event_name, raw, identity=None, policy=None):
        active = policy or self.policy()
        return sb.observe(
            raw,
            event_name=event_name,
            profile=self.identity["profile"],
            identity=identity or self.identity,
            component=self.component,
            policy_path=self.policy_path,
            policy=active,
            enforced=active["mode"] == "enforce",
            state_dir=self.state,
        )

    def audit_rows(self):
        path = self.state / "stub-audit.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def run_canary(self, enabled=True, mode="shadow"):
        return sb.canary(
            component=self.component,
            policy_path=self.policy_path,
            policy=self.policy(mode),
            identity=self.identity,
            state_dir=self.state,
            enabled=enabled,
        )


class TranslationFidelityTests(unittest.TestCase):
    """The vendored Codex translation must equal the pinned component's."""

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_event_map_matches_the_pinned_component(self):
        self.assertEqual(self.golden["events"], adapter.EVENTS)
        self.assertEqual(
            {
                "UserPromptSubmit": "ingress",
                "PreToolUse": "tool_before",
                "PostToolUse": "tool_after",
            },
            adapter.EVENTS,
        )

    def test_normalize_matches_every_golden_case(self):
        self.assertTrue(self.golden["normalize"])
        for case in self.golden["normalize"]:
            self.assertEqual(
                case["result"],
                adapter.normalize(case["event"], case["payload"], case["profile"]),
                case["event"],
            )

    def test_refusals_are_value_errors_like_the_component(self):
        self.assertTrue(self.golden["normalize_errors"])
        for case in self.golden["normalize_errors"]:
            self.assertTrue(case["error_is_value_error"], case)
            with self.assertRaises(ValueError):
                adapter.normalize(case["event"], case["payload"], "p")

    def test_render_matches_every_golden_case(self):
        self.assertTrue(self.golden["render"])
        for case in self.golden["render"]:
            self.assertEqual(
                case["result"], adapter.render(case["event"], case["verdict"], case["raw"])
            )

    def test_codex_never_emits_an_output_replacement(self):
        for case in self.golden["render"]:
            for field in adapter.REPLACEMENT_FIELDS:
                self.assertNotIn(field, json.dumps(case["result"]))
        with self.assertRaises(adapter.AdapterError):
            adapter.assert_no_replacement(
                {"hookSpecificOutput": {"updatedToolOutput": {"stdout": "x"}}}
            )

    def test_manifest_agrees_with_the_pinned_adapter(self):
        manifest = sb.load_manifest()
        component = sb.component_index(manifest)
        self.assertEqual(adapter.REVISION, component["revision"])
        self.assertEqual("jev-sentinel", component["id"])
        self.assertIn("sentinel_incident", manifest["events"]["kinds"])
        self.assertEqual(
            "jev-sentinel", manifest["ownership"]["interface_owners"]["sentinel_boundary"]
        )


class SwitchTests(unittest.TestCase):
    def test_both_sentinel_switches_default_off(self):
        state = sb.feature_switches(sb.load_manifest(), env={})
        self.assertFalse(state["sentinel.shadow"])
        self.assertFalse(state["sentinel.enforcement"])
        self.assertEqual(
            ["sentinel.shadow", "sentinel.enforcement"],
            list(state),
            "shadow must be resolved before enforcement",
        )

    def test_environment_selects_the_switches(self):
        state = sb.feature_switches(
            sb.load_manifest(),
            env={"JEV_SWITCH_SENTINEL_SHADOW": "1", "JEV_SWITCH_SENTINEL_ENFORCEMENT": "0"},
        )
        self.assertTrue(state["sentinel.shadow"])
        self.assertFalse(state["sentinel.enforcement"])

    def test_manifest_keeps_enforcement_behind_shadow(self):
        features = sb.load_manifest()["features"]
        self.assertIn("sentinel.shadow", features["sentinel.enforcement"]["requires"])


class BoundTests(SentinelTestCase):
    def test_payload_within_limits_is_forwarded_unchanged(self):
        event = adapter.normalize("UserPromptSubmit", {"prompt": "hi"}, "p")
        self.assertEqual(event, sb.bound_event(event, {"max_content_bytes": 65536}))

    def test_oversize_content_refuses_and_never_truncates(self):
        event = adapter.normalize("UserPromptSubmit", {"prompt": "x" * 70000}, "p")
        with self.assertRaises(sb.BoundaryError) as caught:
            sb.bound_event(event, {"max_content_bytes": 65536})
        self.assertEqual(sb.E_PAYLOAD_BOUND, caught.exception.code)
        self.assertNotIn("x" * 32, str(caught.exception))

    def test_a_refused_payload_is_recorded_and_not_forwarded(self):
        observed = self.observe("UserPromptSubmit", {"prompt": "y" * 70000})
        self.assertEqual(sb.E_PAYLOAD_BOUND, observed["refused"])
        self.assertEqual({}, observed["response"])
        self.assertEqual([], self.audit_rows(), "the component must not be called")
        incident = observed["incident"]
        self.assertEqual("boundary_refusal", incident["outcome"])
        self.assertEqual("REVIEW", incident["sentinel"]["decision"])
        self.assertEqual(["host_payload_bound"], incident["sentinel"]["reason_codes"])
        self.assertEqual("host_boundary", incident["sentinel"]["backend"])

    def test_missing_wiring_is_a_bounded_refusal(self):
        with self.assertRaises(sb.BoundaryError) as caught:
            sb.read_coverage(self.profile_root)
        self.assertEqual(sb.E_HOOKS_MISSING, caught.exception.code)


class IncidentTests(SentinelTestCase):
    def test_canary_binds_the_manifest_envelope(self):
        manifest = sb.load_manifest()
        result = self.run_canary()
        self.assertTrue(result["complete"])
        records = sb.read_incidents(sb.incident_path(self.state))
        self.assertEqual(3, len(records))
        for record in records:
            for field in manifest["events"]["envelope_fields"]:
                self.assertIn(field, record, field)
            self.assertEqual("sentinel_incident", record["kind"])
            self.assertEqual("jev-sentinel", record["component"])
            self.assertEqual("content_sha256_only", record["redaction"])
            self.assertEqual(self.identity["session_id"], record["session_id"])
            self.assertEqual(self.identity["turn_id"], record["turn_id"])
            self.assertEqual(64, len(record["sentinel"]["content_sha256"]))
            self.assertEqual(64, len(record["sentinel"]["session_ref"]))

    def test_canary_incidents_correlate_to_the_host_identity(self):
        self.run_canary()
        records = sb.read_incidents(sb.incident_path(self.state))
        by_stage = {record["stage"]: record for record in records}
        self.assertEqual({"ingress", "tool_before", "tool_after"}, set(by_stage))
        self.assertEqual("", by_stage["ingress"]["tool_call_id"])
        for stage in ("tool_before", "tool_after"):
            self.assertEqual(f"session-1-{stage}", by_stage[stage]["tool_call_id"])
        self.assertEqual(3, len({record["event_id"] for record in records}))
        # One session, one turn, three boundaries: the correlation #18 asks for.
        self.assertEqual(1, len({record["session_id"] for record in records}))
        self.assertEqual(1, len({record["turn_id"] for record in records}))

    def test_shadow_canary_records_but_never_vetoes(self):
        result = self.run_canary()
        for canary in result["canaries"]:
            self.assertEqual("BLOCK", canary["decision"])
            self.assertEqual(["installation_test_canary"], canary["reason_codes"])
            self.assertFalse(canary["enforced"])
            self.assertFalse(canary["host_vetoed"])
            self.assertEqual([], canary["host_response_keys"])

    def test_enforce_maps_each_stage_onto_its_supported_response(self):
        result = self.run_canary(mode="enforce")
        responses = {canary["stage"]: canary["host_response_keys"] for canary in result["canaries"]}
        self.assertEqual(["decision", "reason"], responses["ingress"])
        self.assertEqual(["hookSpecificOutput"], responses["tool_before"])
        self.assertEqual(["decision", "hookSpecificOutput", "reason"], responses["tool_after"])
        for canary in result["canaries"]:
            self.assertTrue(canary["host_vetoed"])
            self.assertTrue(canary["enforced"])

    def test_incidents_never_carry_raw_payload_text(self):
        self.observe(
            "PostToolUse",
            {"tool_name": "shell", "tool_response": {"stdout": f"secret {adapter.CANARY}"}},
        )
        text = sb.incident_path(self.state).read_text(encoding="utf-8")
        self.assertNotIn(adapter.CANARY, text)
        self.assertNotIn("secret", text)

    def test_a_disabled_switch_runs_no_canary(self):
        result = self.run_canary(enabled=False)
        self.assertFalse(result["complete"])
        self.assertEqual([], result["incident_event_ids"])
        self.assertEqual([], self.audit_rows())
        self.assertTrue(all(c["skipped"] == "switch_off" for c in result["canaries"]))

    def test_enforced_flag_follows_the_policy_not_the_switch(self):
        shadow = self.observe("PreToolUse", {"tool_name": "shell", "tool_input": {"a": 1}})
        self.assertFalse(shadow["verdict"]["enforced"])
        enforced = self.observe(
            "PreToolUse", {"tool_name": "shell", "tool_input": {"a": 1}}, policy=self.policy("enforce")
        )
        self.assertTrue(enforced["verdict"]["enforced"])


class CoverageTests(SentinelTestCase):
    def report(self, *, probe=False, nonce="n1"):
        return sb.coverage_report(
            manifest=sb.load_manifest(),
            component=self.component,
            profile_root=self.profile_root,
            policy_path=self.policy_path,
            hooks_path=None,
            switches={"sentinel.shadow": True, "sentinel.enforcement": False},
            probe=probe,
            nonce=nonce,
        )

    def test_report_names_every_covered_path_and_tool(self):
        self.write_hooks()
        report = self.report()
        self.assertEqual(["ingress", "tool_after", "tool_before"], report["wired_stages"])
        self.assertEqual(["*"], report["covered_tools"])
        self.assertTrue(report["shadow"]["effective"])
        self.assertFalse(report["response_platform"]["replacement_fields"])
        for stage, spec in report["event_paths"].items():
            self.assertTrue(spec["wired"], stage)
            self.assertTrue(spec["reachable"], stage)
            self.assertEqual(["*"], spec["covered_tools"], stage)

    def test_a_narrow_matcher_reports_the_tools_it_leaves_out(self):
        self.write_hooks(matcher="shell|read")
        report = self.report()
        self.assertEqual(["read", "shell"], report["covered_tools"])
        self.assertIn(
            "tools_outside_matcher.tool_before",
            [surface["id"] for surface in report["bypass_surfaces"]],
        )

    def test_missing_event_path_is_reported_as_uncovered(self):
        self.write_hooks(events={"ingress", "tool_before"})
        report = self.report()
        self.assertNotIn("tool_after", report["wired_stages"])
        self.assertIn("hook_not_wired.tool_after", [s["id"] for s in report["bypass_surfaces"]])
        self.assertFalse(report["shadow"]["effective"])

    def test_native_disable_all_hooks_unwires_everything(self):
        self.write_hooks(disable=True)
        report = self.report(probe=True)
        self.assertEqual([], report["wired_stages"])
        self.assertTrue(report["disable_all_hooks"])
        self.assertIn("native_disable_all_hooks", [s["id"] for s in report["bypass_surfaces"]])
        self.assertFalse(report["activation"]["activated"])

    def test_installation_alone_is_not_activation(self):
        self.write_hooks()
        report = self.report()
        self.assertFalse(report["activation"]["activated"])
        self.assertEqual("none", report["activation"]["basis"])
        self.assertEqual([], self.audit_rows())

    def test_unresolvable_launcher_is_reported(self):
        self.write_hooks(launcher=self.root / "missing" / "launch.py")
        report = self.report(probe=True)
        self.assertEqual([], report["reachable_stages"])
        self.assertIn("launcher_unreachable.tool_before", [s["id"] for s in report["bypass_surfaces"]])
        self.assertFalse(report["activation"]["activated"])

    def test_probe_shows_activation_through_the_wired_commands(self):
        self.write_hooks()
        report = self.report(probe=True, nonce="probe-a")
        self.assertTrue(report["activation"]["activated"])
        self.assertEqual("probe_canary", report["activation"]["basis"])
        evidence = report["activation"]["evidence"]
        self.assertEqual(["ingress", "tool_after", "tool_before"], sorted(evidence))
        for stage, item in evidence.items():
            self.assertTrue(item["probed"], stage)
            self.assertTrue(item["responses"][0]["ok"], stage)
            self.assertEqual([], item["responses"][0]["replacement_fields"], stage)
            self.assertGreaterEqual(item["audit_rows"], 1, stage)
            self.assertEqual(["installation_test_canary"], item["reason_codes"], stage)

    def test_a_reachable_launcher_that_does_not_evaluate_is_not_activation(self):
        echoing = self.root / "launch.py"
        echoing.write_text("import sys, json\nsys.stdin.read()\nprint(json.dumps({}))\n", encoding="utf-8")
        self.write_hooks(launcher=echoing)
        report = self.report(probe=True, nonce="probe-b")
        self.assertFalse(report["activation"]["activated"])

    def test_probe_evidence_cannot_be_satisfied_by_an_earlier_run(self):
        self.write_hooks()
        self.assertTrue(self.report(probe=True, nonce="probe-1")["activation"]["activated"])
        echoing = self.root / "launch.py"
        echoing.write_text("import sys, json\nsys.stdin.read()\nprint(json.dumps({}))\n", encoding="utf-8")
        self.write_hooks(launcher=echoing)
        self.assertFalse(self.report(probe=True, nonce="probe-2")["activation"]["activated"])

    def test_bypass_surfaces_include_the_documented_set(self):
        self.write_hooks()
        report = self.report()
        ids = {surface["id"] for surface in report["bypass_surfaces"]}
        for expected in (
            "post_tool_replacement_unsupported",
            "host_trust_unverified",
            "local_rules_only",
            "ingress_scope_is_prompt_only",
        ):
            self.assertIn(expected, ids)
        kinds = {surface["kind"] for surface in report["bypass_surfaces"]}
        self.assertEqual({"bypass", "unsupported_replacement"}, kinds)

    def test_switch_off_is_reported_as_a_bypass(self):
        self.write_hooks()
        report = sb.coverage_report(
            manifest=sb.load_manifest(),
            component=self.component,
            profile_root=self.profile_root,
            policy_path=self.policy_path,
            hooks_path=None,
            switches={"sentinel.shadow": False, "sentinel.enforcement": False},
            probe=False,
        )
        self.assertIn("integration_switch_off", [s["id"] for s in report["bypass_surfaces"]])
