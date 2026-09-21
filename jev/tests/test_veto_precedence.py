#!/usr/bin/env python3
"""Real-component tests for Sentinel veto precedence and the latch (#19).

The required-CI suite drives the host layer with a stub component. These tests
drive the *pinned* ``jev-sentinel`` checkout through ``launch.py``, so the
evaluator, its policy handling, its audit store, and its own session taint latch
are the real ones. Tier: ``real-component`` (local execution, no network).

The point of this tier is the division of labour: the component decides, and the
host only maps, orders, and serializes. Every assertion here is a fact about those
two stores interacting, not a restatement of a rule.

When no checkout is resolvable the module skips with the reason, rather than
passing quietly: an unproven boundary must not look verified.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402
import sentinel_veto as sv  # noqa: E402

CANARY = adapter.CANARY
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
class VetoPrecedenceTestCase(unittest.TestCase):
    """One pinned component, policy, and state directory per test."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy_path = self.state / "policy.json"
        self.identity = {
            "profile": "codex-real",
            "session_id": "real-session-1",
            "turn_id": "real-turn-1",
            "tool_call_id": "",
            "workspace": "/workspace",
            "parent_event_id": "",
        }
        self.enforce(True)

    def tearDown(self):
        self.work.cleanup()

    def enforce(self, on: bool):
        """Write the real policy; ``Read`` is the one read-only tool."""
        self.policy_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "enforce" if on else "shadow",
                    "backend": "local",
                    "read_only_tools": {"codex": ["Read"]},
                }
            ),
            encoding="utf-8",
        )
        return sb.load_policy(self.policy_path)

    def run_event(
        self,
        payload,
        *,
        event_name="UserPromptSubmit",
        enforce=True,
        turn="real-turn-1",
    ):
        return sv.handle(
            payload,
            event_name=event_name,
            profile="codex-real",
            identity={**self.identity, "turn_id": turn},
            component=COMPONENT,
            policy_path=self.policy_path,
            policy=self.enforce(enforce),
            enforce=enforce,
            state_dir=self.state,
        )

    def rows(self):
        return sv.read_latch(sv.latch_path(self.state))

    def read_tool(self, turn="real-turn-2"):
        return self.run_event(
            {"tool_name": "Read", "tool_input": {"file_path": "/workspace/notes.md"}},
            event_name="PreToolUse",
            turn=turn,
        )

    def bash_tool(self, turn="real-turn-3"):
        return self.run_event(
            {"tool_name": "Bash", "tool_input": {"command": "printf hello"}},
            event_name="PreToolUse",
            turn=turn,
        )


class DetectionOwnershipTests(VetoPrecedenceTestCase):
    """The host adds ordering; it never re-derives a decision."""

    def test_the_host_verdict_is_the_components_verdict(self):
        payload = {"prompt": f"carry {CANARY} through"}
        event = adapter.normalize("UserPromptSubmit", payload, "codex-real")
        event["session_id"] = self.identity["session_id"]
        direct = sb.verdict_for(COMPONENT, self.policy_path, event)
        observed = self.run_event(payload)["verdict"]
        self.assertEqual("BLOCK", direct["decision"])
        self.assertEqual(direct["decision"], observed["decision"])
        self.assertEqual(direct["reason_codes"], observed["reason_codes"])
        self.assertEqual(direct["route"], observed["route"])
        self.assertEqual(direct["backend"], observed["backend"])

    def test_the_latch_key_is_the_components_own_session_key(self):
        result = self.run_event({"prompt": f"carry {CANARY} through"})
        self.assertEqual(
            sb.session_ref_for("codex", "codex-real", self.identity["session_id"]),
            result["verdict"]["session_ref"],
            "the host must latch on the component's key, not a second one",
        )
        self.assertEqual(result["session_ref"], result["verdict"]["session_ref"])
        self.assertEqual(result["session_ref"], self.rows()[0]["session_ref"])


class PrecedenceTests(VetoPrecedenceTestCase):
    """A latched veto gates the action that follows it."""

    def test_a_real_block_latches_and_denies_the_next_read_only_action(self):
        first = self.run_event({"prompt": f"carry {CANARY} through"})
        self.assertEqual("BLOCK", first["effective_decision"])
        self.assertEqual("event", first["source"], "the component's own finding vetoes")
        self.assertEqual("BLOCK", first["latch_after"]["decision"])

        later = self.read_tool()
        self.assertEqual(
            "DEFER",
            later["verdict"]["decision"],
            "the component has no finding of its own for a read-only tool",
        )
        self.assertTrue(later["vetoed"], "the latched veto still prevents the action")
        self.assertEqual("latch", later["source"])
        self.assertEqual("BLOCK", later["effective_decision"])
        self.assertEqual(
            "deny", later["response"]["hookSpecificOutput"]["permissionDecision"]
        )

    def test_the_latch_survives_a_new_turn(self):
        self.run_event({"prompt": f"carry {CANARY} through"}, turn="real-turn-1")
        later = self.read_tool(turn="real-turn-9")
        self.assertTrue(later["vetoed"])
        self.assertEqual("latch", later["source"])

    def test_shadow_is_observational_with_the_real_component(self):
        result = self.run_event({"prompt": f"carry {CANARY} through"}, enforce=False)
        self.assertEqual("BLOCK", result["verdict"]["decision"])
        self.assertFalse(result["vetoed"])
        self.assertEqual({}, result["response"])
        self.assertEqual([], self.rows(), "a shadow finding never writes a host latch")


class TwoLatchTests(VetoPrecedenceTestCase):
    """The host latch and the component's taint are separate stores."""

    def test_clearing_the_host_latch_leaves_the_components_taint_in_force(self):
        first = self.run_event({"prompt": f"carry {CANARY} through"})
        ref = sv.sb.session_ref_for("codex", "codex-real", self.identity["session_id"])
        self.assertEqual(first["session_ref"], ref)

        sv.clear(sv.latch_path(self.state), session_ref=ref, confirm=True)
        self.assertIsNone(sv.latched(self.rows(), ref))

        read_only = self.read_tool()
        self.assertFalse(
            read_only["vetoed"],
            "with the host latch cleared a read-only tool is not sensitive to the taint",
        )
        self.assertEqual({}, read_only["response"])

        sensitive = self.bash_tool()
        self.assertEqual(
            "REVIEW",
            sensitive["verdict"]["decision"],
            "the component's own taint still applies",
        )
        self.assertIn("tainted_session", sensitive["verdict"]["reason_codes"])
        self.assertTrue(sensitive["vetoed"])
        self.assertEqual(
            "event", sensitive["source"], "the finding is the component's own"
        )


class LedgerTests(VetoPrecedenceTestCase):
    """The host ledger joins to the component's own audit row."""

    def test_the_latch_row_joins_the_component_audit_row(self):
        result = self.run_event({"prompt": f"carry {CANARY} through"})
        rows = sb.outbox(COMPONENT, self.policy_path)
        event = result["observed"]["event"]
        digest = sb.digest_content(event)
        match = [row for row in rows if row.get("content_sha256") == digest]
        self.assertTrue(match, "the component recorded the same normalized content")
        self.assertEqual(result["verdict"]["id"], match[-1]["id"])
        self.assertEqual(result["session_ref"], match[-1]["session_ref"])
        self.assertEqual(result["session_ref"], self.rows()[0]["session_ref"])

    def test_the_incident_chain_points_at_the_latching_event(self):
        first = self.run_event({"prompt": f"carry {CANARY} through"})
        later = self.read_tool()
        chained = later["observed"]["incident"]
        self.assertEqual(first["incident_event_id"], chained["parent_event_id"])
        self.assertEqual("latch", later["source"])
        self.assertEqual(
            first["incident_event_id"], later["latch_before"]["incident_event_id"]
        )


if __name__ == "__main__":
    unittest.main()
