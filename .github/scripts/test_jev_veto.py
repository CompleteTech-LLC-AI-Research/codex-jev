#!/usr/bin/env python3
"""Required-CI coverage for Sentinel veto precedence and concurrency (#19).

These tests pin the host contract issue #19 asks for: a configured
``REVIEW``/``BLOCK``/``QUARANTINE`` outcome maps onto the one veto the platform
can express for that stage; a latched veto cannot be downgraded by a later
approval, a later ``DEFER``, or a weaker finding; the subsequent action is
prevented before execution; parallel evaluations of one session are serialized
and never lose a raise; and a timeout, a malformed response, a cancellation, or
a policy failure fails closed instead of turning into an allow. Shadow stays
observational.

Evidence tiers, both offline:

* ``component-stub`` - a tiny ``launch.py`` implements only the component's
  documented ``check`` wire and a set of fault markers, so the host layer can be
  driven deterministically without the component checkout or a network. The stub
  is not a detection oracle; it exists to prove the host's own bookkeeping.

The real-component run is in ``jev/tests/test_veto_precedence.py``.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402
import sentinel_veto as sv  # noqa: E402

MODULE = SCRIPTS / "sentinel_veto.py"

# A stand-in for the pinned component: only ``check``, plus fault markers the
# tests use to drive the host's fail-closed paths.
STUB_LAUNCH = '''
import hashlib, json, sys, time
from pathlib import Path


def arg(name, default=""):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    policy_path = Path(arg("--policy", str(Path.home() / ".jev-sentinel/policy.json")))
    policy = json.loads(policy_path.read_text()) if policy_path.is_file() else {}
    raw = json.loads(sys.stdin.read() or "{}")
    text = str(raw.get("content", "")) + "\\n" + json.dumps(raw.get("tool_input", {}), sort_keys=True)
    if "JEV_SLEEP" in text:
        time.sleep(5)
    if "JEV_MALFORMED_JSON" in text:
        print("this is not json")
        return 0
    if "JEV_MALFORMED_VERDICT" in text:
        print(json.dumps({"decision": "MAYBE", "enforced": True, "id": "stub"}))
        return 0
    if "JEV_EXIT" in text:
        return 3
    decision, reasons = "DEFER", []
    for marker, name in (("JEV_QUARANTINE", "QUARANTINE"),
                         ("JEV_SENTINEL_TEST_BLOCK", "BLOCK"),
                         ("JEV_REVIEW", "REVIEW")):
        if marker in text:
            decision, reasons = name, [marker.lower()]
            break
    print(json.dumps({
        "id": hashlib.sha256(text.encode()).hexdigest()[:32],
        "decision": decision,
        "enforced": policy.get("mode", "shadow") == "enforce",
        "reason_codes": reasons,
        "route": "administrator" if decision != "DEFER" else "normal",
        "backend": "local",
        "message": "JEV Sentinel: stub verdict.",
    }))
    return 0


sys.exit(main())
'''


class VetoTestCase(unittest.TestCase):
    """One stub component, policy, and state directory per test."""

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
            json.dumps({"schema_version": 1, "mode": "enforce", "backend": "local"}),
            encoding="utf-8",
        )
        self.identity = {
            "profile": "codex-stub",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_call_id": "",
            "workspace": "/workspace",
            "parent_event_id": "",
        }
        self._timeout = sb.HOOK_TIMEOUT_S

    def tearDown(self):
        sb.HOOK_TIMEOUT_S = self._timeout
        self.work.cleanup()

    def policy(self, mode="enforce"):
        self.policy_path.write_text(
            json.dumps({"schema_version": 1, "mode": mode, "backend": "local"}),
            encoding="utf-8",
        )
        return sb.load_policy(self.policy_path)

    def run_event(self, payload, *, event_name="UserPromptSubmit", session="session-1",
                  enforce=True, mode="enforce", turn="turn-1", identity=None):
        policy = self.policy(mode)
        return sv.handle(
            payload,
            event_name=event_name,
            profile="codex-stub",
            identity=identity or {**self.identity, "session_id": session, "turn_id": turn},
            component=self.component,
            policy_path=self.policy_path,
            policy=policy,
            enforce=enforce,
            state_dir=self.state,
        )

    def rows(self):
        return sv.read_latch(sv.latch_path(self.state))


class MappingTests(VetoTestCase):
    """A configured outcome maps onto the one veto the stage can express."""

    def test_defer_under_enforcement_emits_no_veto(self):
        result = self.run_event({"prompt": "ordinary work"})
        self.assertEqual("DEFER", result["verdict"]["decision"])
        self.assertFalse(result["vetoed"])
        self.assertEqual({}, result["response"])
        self.assertEqual([], self.rows())

    def test_each_configured_outcome_vetoes_at_ingress(self):
        for marker, decision in (
            ("JEV_REVIEW", "REVIEW"),
            ("JEV_SENTINEL_TEST_BLOCK", "BLOCK"),
            ("JEV_QUARANTINE", "QUARANTINE"),
        ):
            with self.subTest(decision=decision):
                result = self.run_event({"prompt": marker}, session=f"s-{decision}")
                self.assertTrue(result["vetoed"], decision)
                self.assertEqual(decision, result["effective_decision"])
                self.assertEqual("event", result["source"])
                self.assertEqual({"decision", "reason"}, set(result["response"]))

    def test_each_configured_outcome_vetoes_at_tool_before(self):
        for marker, decision in (
            ("JEV_REVIEW", "REVIEW"),
            ("JEV_SENTINEL_TEST_BLOCK", "BLOCK"),
            ("JEV_QUARANTINE", "QUARANTINE"),
        ):
            with self.subTest(decision=decision):
                result = self.run_event(
                    {"tool_name": "Bash", "tool_input": {"command": marker}},
                    event_name="PreToolUse",
                    session=f"t-{decision}",
                )
                self.assertTrue(result["vetoed"], decision)
                out = result["response"]["hookSpecificOutput"]
                self.assertEqual("PreToolUse", out["hookEventName"])
                self.assertEqual("deny", out["permissionDecision"])
                self.assertEqual({"hookSpecificOutput"}, set(result["response"]))

    def test_each_configured_outcome_vetoes_at_tool_after(self):
        result = self.run_event(
            {"tool_name": "Bash", "tool_response": "JEV_SENTINEL_TEST_BLOCK"},
            event_name="PostToolUse",
        )
        self.assertTrue(result["vetoed"])
        self.assertEqual({"decision", "reason", "hookSpecificOutput"}, set(result["response"]))
        self.assertEqual("PostToolUse", result["response"]["hookSpecificOutput"]["hookEventName"])

    def test_no_response_path_carries_an_output_replacement(self):
        for event_name, payload in (
            ("UserPromptSubmit", {"prompt": "JEV_SENTINEL_TEST_BLOCK"}),
            ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "JEV_QUARANTINE"}}),
            ("PostToolUse", {"tool_name": "Bash", "tool_response": "JEV_REVIEW"}),
        ):
            with self.subTest(event=event_name):
                result = self.run_event(payload, event_name=event_name)
                self.assertTrue(result["vetoed"])
                for field in adapter.REPLACEMENT_FIELDS:
                    self.assertNotIn(field, json.dumps(result["response"]))

    def test_the_host_never_emits_permission_decision_allow(self):
        for event_name, payload in (
            ("UserPromptSubmit", {"prompt": "ordinary work"}),
            ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            ("PostToolUse", {"tool_name": "Bash", "tool_response": "ok"}),
        ):
            with self.subTest(event=event_name):
                result = self.run_event(payload, event_name=event_name)
                self.assertNotIn("allow", json.dumps(result["response"]))


class PrecedenceTests(VetoTestCase):
    """A latched veto cannot be downgraded, and it gates the next action."""

    def test_a_block_is_not_downgraded_by_a_later_defer(self):
        first = self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"})
        self.assertEqual("BLOCK", first["effective_decision"])
        second = self.run_event(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            event_name="PreToolUse",
            turn="turn-2",
        )
        self.assertEqual("DEFER", second["verdict"]["decision"])
        self.assertTrue(second["vetoed"])
        self.assertEqual("latch", second["source"])
        self.assertEqual("BLOCK", second["effective_decision"])
        self.assertEqual("deny", second["response"]["hookSpecificOutput"]["permissionDecision"])
        reason = second["response"]["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("latched BLOCK veto", reason)

    def test_a_later_approval_shaped_event_cannot_clear_a_veto(self):
        self.run_event({"prompt": "JEV_QUARANTINE"}, session="s-approval")
        approved = self.run_event(
            {"tool_name": "Bash", "tool_input": {"command": "ls", "approved": True}},
            event_name="PreToolUse",
            session="s-approval",
            turn="turn-2",
        )
        self.assertTrue(approved["vetoed"])
        self.assertEqual("latch", approved["source"])
        self.assertEqual("QUARANTINE", approved["effective_decision"])

    def test_the_latch_keeps_the_strongest_decision(self):
        self.run_event({"prompt": "JEV_QUARANTINE"}, session="s-strong")
        later = self.run_event(
            {"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="s-strong", turn="turn-2"
        )
        self.assertEqual("QUARANTINE", later["effective_decision"])
        self.assertEqual("QUARANTINE", later["latch_after"]["decision"])

    def test_a_stronger_finding_does_escalate_the_latch(self):
        self.run_event({"prompt": "JEV_REVIEW"}, session="s-rise")
        later = self.run_event(
            {"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="s-rise", turn="turn-2"
        )
        self.assertEqual("BLOCK", later["effective_decision"])
        self.assertEqual("BLOCK", later["latch_after"]["decision"])
        self.assertTrue(later["latch"]["written"])

    def test_the_latch_is_per_session(self):
        self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="s-a")
        other = self.run_event({"prompt": "ordinary"}, session="s-b", turn="turn-2")
        self.assertFalse(other["vetoed"])
        self.assertIsNone(other["latch_after"])

    def test_no_session_identity_means_no_latch(self):
        first = self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="")
        self.assertTrue(first["vetoed"])
        self.assertFalse(first["latch"]["written"])
        self.assertEqual([], self.rows())
        second = self.run_event({"prompt": "ordinary"}, session="", turn="turn-2")
        self.assertFalse(second["vetoed"])

    def test_the_session_key_matches_the_boundary(self):
        event = adapter.normalize("UserPromptSubmit", {"prompt": "x"}, "codex-stub")
        event["session_id"] = "session-1"
        self.assertEqual(
            sb.session_ref(event), sb.session_ref_for("codex", "codex-stub", "session-1")
        )
        self.assertEqual("", sb.session_ref_for("codex", "codex-stub", ""))

    def test_the_incident_chain_points_at_the_latched_cause(self):
        first = self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"})
        second = self.run_event(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}}, event_name="PreToolUse"
        )
        chained = second["observed"]["incident"]
        self.assertEqual(first["incident_event_id"], chained["parent_event_id"])
        self.assertNotEqual("", chained["parent_event_id"])

    def test_shadow_records_but_neither_applies_nor_clears_the_latch(self):
        self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="s-shadow")
        shadow = self.run_event(
            {"prompt": "JEV_SENTINEL_TEST_BLOCK"},
            session="s-shadow",
            enforce=False,
            turn="turn-2",
        )
        self.assertEqual("BLOCK", shadow["verdict"]["decision"], "shadow still reports the finding")
        self.assertFalse(shadow["vetoed"])
        self.assertEqual({}, shadow["response"])
        self.assertEqual("BLOCK", shadow["latch_after"]["decision"])

    def test_enforcement_follows_the_switch_not_the_policy_mode(self):
        enforcing = {"mode": "enforce"}
        both = {sb.SHADOW_SWITCH: True, sb.ENFORCE_SWITCH: True}
        self.assertTrue(sv.enforcement_enabled(enforcing, both))
        self.assertFalse(sv.enforcement_enabled(enforcing, {}))
        self.assertFalse(sv.enforcement_enabled(enforcing, {sb.ENFORCE_SWITCH: True}))
        self.assertFalse(sv.enforcement_enabled({"mode": "shadow"}, both))


class ClearTests(VetoTestCase):
    """Clearing is explicit, auditable, and mirrors the component's command."""

    def test_clear_requires_confirmation_and_a_full_session_ref(self):
        ref = sb.session_ref_for("codex", "codex-stub", "session-1")
        path = sv.latch_path(self.state)
        with self.assertRaises(sv.VetoError) as caught:
            sv.clear(path, session_ref=ref, confirm=False)
        self.assertEqual(sv.E_CONFIRM, caught.exception.code)
        with self.assertRaises(sv.VetoError) as caught:
            sv.clear(path, session_ref="short", confirm=True)
        self.assertEqual(sv.E_SESSION_REF, caught.exception.code)

    def test_clear_lifts_the_latch_and_is_recorded(self):
        self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"})
        ref = sb.session_ref_for("codex", "codex-stub", "session-1")
        sv.clear(sv.latch_path(self.state), session_ref=ref, confirm=True)
        self.assertIsNone(sv.latched(self.rows(), ref))
        self.assertEqual("clear", self.rows()[-1]["action"])
        after = self.run_event({"prompt": "ordinary"}, turn="turn-2")
        self.assertFalse(after["vetoed"])
        self.assertEqual({}, after["response"])


class FailClosedTests(VetoTestCase):
    """A failure never becomes an allow."""

    def fail_closed(self, payload, *, event_name="UserPromptSubmit", expected):
        result = self.run_event(payload, event_name=event_name)
        self.assertEqual(expected, result["failure"])
        self.assertEqual("REVIEW", result["effective_decision"])
        self.assertTrue(result["vetoed"])
        self.assertEqual("failure", result["source"])
        self.assertEqual("REVIEW", result["latch_after"]["decision"])
        return result

    def test_a_component_timeout_fails_closed(self):
        sb.HOOK_TIMEOUT_S = 0.3
        self.fail_closed({"prompt": "JEV_SLEEP now"}, expected=sb.E_COMPONENT_FAILED)

    def test_a_malformed_component_response_fails_closed(self):
        self.fail_closed({"prompt": "JEV_MALFORMED_JSON"}, expected=sb.E_COMPONENT_FAILED)

    def test_a_malformed_verdict_fails_closed(self):
        self.fail_closed({"prompt": "JEV_MALFORMED_VERDICT"}, expected=sb.E_VERDICT_SHAPE)

    def test_a_nonzero_component_exit_fails_closed(self):
        self.fail_closed({"prompt": "JEV_EXIT"}, expected=sb.E_COMPONENT_FAILED)

    def test_a_malformed_payload_fails_closed(self):
        self.fail_closed({"tool_input": {}}, expected=sb.E_PAYLOAD_SHAPE)

    def test_a_boundary_refusal_also_vetoes_under_enforcement(self):
        self.policy_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "enforce",
                    "backend": "local",
                    "max_content_bytes": 256,
                }
            ),
            encoding="utf-8",
        )
        result = sv.handle(
            {"prompt": "x" * 400},
            event_name="UserPromptSubmit",
            profile="codex-stub",
            identity=self.identity,
            component=self.component,
            policy_path=self.policy_path,
            policy=sb.load_policy(self.policy_path),
            enforce=True,
            state_dir=self.state,
        )
        self.assertEqual(sb.E_PAYLOAD_BOUND, result["refused"])
        self.assertTrue(result["vetoed"])
        self.assertEqual("REVIEW", result["effective_decision"])
        self.assertEqual("boundary_refusal", result["source"])

    def test_a_corrupt_policy_is_treated_as_enforcing(self):
        self.policy_path.write_text("{not json", encoding="utf-8")
        policy = sv._load_policy_fail_closed(self.policy_path)
        self.assertEqual("enforce", policy["mode"])
        self.assertTrue(policy["fail_closed"])

    def test_a_failure_verdict_mirrors_the_component_shape(self):
        verdict = sv.failure_verdict("worker_error_or_timeout")
        self.assertEqual("REVIEW", verdict["decision"])
        self.assertTrue(verdict["enforced"])
        self.assertEqual("security_review", verdict["route"])
        self.assertEqual("unavailable", verdict["backend"])

    def test_cancellation_latches_before_it_propagates(self):
        original = sb.observe

        def cancelled(*args, **kwargs):
            raise KeyboardInterrupt()

        sb.observe = cancelled
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_event({"prompt": "ordinary"})
        finally:
            sb.observe = original
        ref = sb.session_ref_for("codex", "codex-stub", "session-1")
        state = sv.latched(self.rows(), ref)
        self.assertIsNotNone(state, "a cancelled evaluation must not leave the session ungated")
        self.assertEqual("REVIEW", state["decision"])
        self.assertEqual("failure", state["source"])


class ConcurrencyTests(VetoTestCase):
    """Parallel evaluations of one session are serialized and never lose a raise."""

    def test_the_session_lock_is_exclusive(self):
        inside = []
        overlap = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            for _ in range(20):
                with sv.session_lock(self.state, "s-lock"):
                    inside.append(1)
                    time.sleep(0.001)
                    if len(inside) > 1:
                        overlap.append(1)
                    inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], overlap, "two threads were inside one session lock at once")

    def test_parallel_escalations_never_lose_a_raise(self):
        ref = "a" * 64
        path = sv.latch_path(self.state)
        decisions = ["REVIEW", "BLOCK", "QUARANTINE", "BLOCK", "REVIEW"]
        barrier = threading.Barrier(len(decisions))

        def worker(decision):
            barrier.wait()
            with sv.session_lock(self.state, ref):
                sv.escalate(path, session_ref=ref, decision=decision, source="event")

        threads = [threading.Thread(target=worker, args=(d,)) for d in decisions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = self.rows()
        self.assertEqual("QUARANTINE", sv.latched(rows, ref)["decision"])
        ranks = [row["rank"] for row in rows]
        self.assertEqual(sorted(set(ranks)), ranks, "the ledger is strictly increasing in rank")
        self.assertEqual(3, ranks[-1], "the strongest concurrent raise survives")

    def test_two_parallel_calls_cannot_both_become_the_winner(self):
        """One call vetoes, the other defers; neither reads the latch stale."""
        outcomes = []
        barrier = threading.Barrier(2)
        policy = self.policy("enforce")

        def worker(prompt, turn):
            barrier.wait()
            outcomes.append(
                sv.handle(
                    {"prompt": prompt},
                    event_name="UserPromptSubmit",
                    profile="codex-stub",
                    identity={**self.identity, "turn_id": turn},
                    component=self.component,
                    policy_path=self.policy_path,
                    policy=policy,
                    enforce=True,
                    state_dir=self.state,
                )
            )

        threads = [
            threading.Thread(target=worker, args=("JEV_SENTINEL_TEST_BLOCK", "turn-a")),
            threading.Thread(target=worker, args=("ordinary", "turn-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        blocked = [o for o in outcomes if o["verdict"]["decision"] == "BLOCK"]
        deferred = [o for o in outcomes if o["verdict"]["decision"] == "DEFER"]
        self.assertEqual(1, len(blocked))
        self.assertEqual(1, len(deferred))
        self.assertTrue(blocked[0]["vetoed"])
        # Serialization makes the order total, so the deferring call is vetoed
        # exactly when it evaluated after the vetoing one. It never reads a stale
        # latch: a call that ran first legitimately has no veto to inherit yet.
        ran_second = deferred[0]["latch_before"] is not None
        self.assertEqual(ran_second, deferred[0]["vetoed"])
        if ran_second:
            self.assertEqual("latch", deferred[0]["source"])
            self.assertEqual("BLOCK", deferred[0]["effective_decision"])
        final = sv.latched(self.rows(), deferred[0]["session_ref"])
        self.assertEqual("BLOCK", final["decision"], "the veto survives both calls")

    def test_parallel_calls_under_an_existing_latch_are_all_vetoed(self):
        ref = sb.session_ref_for("codex", "codex-stub", "session-1")
        sv.escalate(sv.latch_path(self.state), session_ref=ref, decision="BLOCK", source="event")
        policy = self.policy("enforce")
        outcomes = []
        barrier = threading.Barrier(4)

        def worker(turn):
            barrier.wait()
            outcomes.append(
                sv.handle(
                    {"prompt": "ordinary"},
                    event_name="UserPromptSubmit",
                    profile="codex-stub",
                    identity={**self.identity, "turn_id": turn},
                    component=self.component,
                    policy_path=self.policy_path,
                    policy=policy,
                    enforce=True,
                    state_dir=self.state,
                )
            )

        threads = [threading.Thread(target=worker, args=(f"turn-{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(4, len(outcomes))
        for outcome in outcomes:
            self.assertTrue(outcome["vetoed"], "no parallel call escapes the latch")
            self.assertEqual("latch", outcome["source"])
            self.assertEqual("BLOCK", outcome["effective_decision"])


class LedgerTests(VetoTestCase):
    """The ledger is append-only, bounded, and replayable."""

    def test_state_is_replayed_from_the_rows(self):
        ref = "b" * 64
        path = sv.latch_path(self.state)
        sv.escalate(path, session_ref=ref, decision="BLOCK", source="event")
        sv.clear(path, session_ref=ref, confirm=True)
        self.assertIsNone(sv.latched(sv.read_latch(path), ref))
        sv.escalate(path, session_ref=ref, decision="REVIEW", source="event")
        self.assertEqual("REVIEW", sv.latched(sv.read_latch(path), ref)["decision"])

    def test_a_bounded_tail_reproduces_the_full_state(self):
        ref = "c" * 64
        path = sv.latch_path(self.state)
        sv.escalate(path, session_ref=ref, decision="BLOCK", source="event")
        sv.clear(path, session_ref=ref, confirm=True)
        sv.escalate(path, session_ref=ref, decision="REVIEW", source="event")
        sv.escalate(path, session_ref=ref, decision="QUARANTINE", source="event")
        full = sv.latched(sv.read_latch(path), ref)
        self.assertEqual("QUARANTINE", full["decision"])
        original = sv.MAX_LATCH_ROWS
        try:
            for window in (1, 2, 3):
                with self.subTest(rows=window):
                    sv.MAX_LATCH_ROWS = window
                    tail = sv.read_latch(path)
                    self.assertEqual(window, len(tail))
                    self.assertEqual(full, sv.latched(tail, ref))
        finally:
            sv.MAX_LATCH_ROWS = original

    def test_a_foreign_row_is_refused(self):
        sv.append_latch(sv.latch_path(self.state), {"kind": "something_else"})
        with self.assertRaises(sv.VetoError) as caught:
            sv.read_latch(sv.latch_path(self.state))
        self.assertEqual(sv.E_LATCH_SHAPE, caught.exception.code)

    def test_the_ledger_carries_no_context_text(self):
        self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK secret-prompt-text"})
        raw = sv.latch_path(self.state).read_text(encoding="utf-8")
        self.assertNotIn("secret-prompt-text", raw)
        self.assertEqual(sb.REDACTION, self.rows()[0]["redaction"])

    def test_the_summary_reports_every_session(self):
        self.run_event({"prompt": "JEV_SENTINEL_TEST_BLOCK"}, session="s-one")
        self.run_event({"prompt": "JEV_QUARANTINE"}, session="s-two", turn="turn-2")
        self.run_event({"prompt": "ordinary"}, session="s-quiet", turn="turn-3")
        summary = {row["session_ref"]: row for row in sv.latch_summary(self.rows())}
        self.assertEqual(2, len(summary), "only sessions that latched appear")
        ref = sb.session_ref_for("codex", "codex-stub", "s-one")
        self.assertTrue(summary[ref]["latched"])
        self.assertEqual("BLOCK", summary[ref]["decision"])


class CliTests(VetoTestCase):
    """The hook-facing command prints the response and nothing else."""

    def invoke(self, *argv, payload=None):
        request = self.root / "request.json"
        request.write_text(json.dumps(payload or {}), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(MODULE),
                *argv,
                "--component",
                str(self.component),
                "--policy",
                str(self.policy_path),
                "--state-dir",
                str(self.state),
                "--request",
                str(request),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_enforce_prints_only_the_response(self):
        done = self.invoke(
            "enforce",
            "--event",
            "PreToolUse",
            "--profile",
            "codex-stub",
            "--session",
            "cli-session",
            "--force-enforce",
            payload={"tool_name": "Bash", "tool_input": {"command": "JEV_SENTINEL_TEST_BLOCK"}},
        )
        self.assertEqual(0, done.returncode, done.stderr)
        response = json.loads(done.stdout)
        self.assertEqual("deny", response["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(1, len(done.stdout.strip().splitlines()))

    def test_enforce_is_observational_without_the_switch(self):
        done = self.invoke(
            "enforce",
            "--event",
            "PreToolUse",
            "--profile",
            "codex-stub",
            "--session",
            "cli-session",
            payload={"tool_name": "Bash", "tool_input": {"command": "JEV_SENTINEL_TEST_BLOCK"}},
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual({}, json.loads(done.stdout))

    def test_latch_then_clear_round_trips_on_the_command_line(self):
        self.invoke(
            "enforce",
            "--event",
            "UserPromptSubmit",
            "--profile",
            "codex-stub",
            "--session",
            "cli-session",
            "--force-enforce",
            payload={"prompt": "JEV_SENTINEL_TEST_BLOCK"},
        )
        listed = subprocess.run(
            [sys.executable, str(MODULE), "latch", "--state-dir", str(self.state), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        report = json.loads(listed.stdout)
        self.assertEqual(1, len(report["sessions"]))
        self.assertEqual("BLOCK", report["sessions"][0]["decision"])
        ref = report["sessions"][0]["session_ref"]
        args = [sys.executable, str(MODULE), "clear", "--state-dir", str(self.state),
                "--session-ref", ref]
        without_confirm = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(1, without_confirm.returncode)
        cleared = subprocess.run(
            [*args, "--confirm", "--json"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, cleared.returncode, cleared.stderr)
        self.assertEqual(ref, json.loads(cleared.stdout)["cleared"])


if __name__ == "__main__":
    unittest.main()
