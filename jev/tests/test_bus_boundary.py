"""Wire-request tests for the native request adapter and jev-bus boundary.

These prove the acceptance criteria for #15: the boundary invokes one bus owner
in manifest order exactly once, the canonical transcript is never mutated, only
supported message shapes can change, and a stage that fails, reorders, removes,
or edits an unsupported shape contributes nothing.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bus_boundary
import dedup_receipts
import fabric_views
import jev_bus

DEDUP = "jev-prune.dedup"
VIEW = "jev-context-fabric.view"


def sample_request():
    return {
        "model": "gpt-5-codex",
        "instructions": "system",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
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
                "type": "reasoning",
                "id": "r_1",
                "summary": [{"type": "summary_text", "text": "think"}],
                "encrypted_content": None,
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        ],
    }


def enabled_state(**overrides):
    state = {
        "projection.dedup_receipts": True,
        "projection.fabric_views": True,
    }
    state.update(overrides)
    return state


def make_registry(state=None):
    return bus_boundary.build_registry(
        state if state is not None else enabled_state(),
        {"dedup": ["dedup"], "fabric_view": ["view"]},
    )


class Recorder:
    """A deterministic stage spy: records invocation order and returns a shape."""

    def __init__(self, transform=None, fail=None, notes=None):
        self.calls = []
        self._transform = transform
        self._fail = fail
        self._notes = notes or {}

    def __call__(self, stage, request, workspace, budget_ms):
        name = stage["name"]
        self.calls.append(name)
        if self._fail and name == self._fail:
            raise jev_bus.BusError("stage boom")
        messages = request["messages"]
        if self._transform is not None:
            messages = self._transform(name, messages)
        return {
            "ok": True,
            "messages": messages,
            "notes": list(self._notes.get(name, [])),
        }


class BoundaryOrderTests(unittest.TestCase):
    def test_ordered_single_invocation(self):
        recorder = Recorder()
        request = sample_request()
        _, report = bus_boundary.project(
            request, registry=make_registry(), invoke=recorder
        )
        self.assertEqual(recorder.calls, [DEDUP, VIEW])
        self.assertEqual(report["invoked"], [DEDUP, VIEW])

    def test_only_dedup_enabled_invokes_one_stage(self):
        recorder = Recorder()
        registry = make_registry(enabled_state(**{"projection.fabric_views": False}))
        _, report = bus_boundary.project(
            sample_request(), registry=registry, invoke=recorder
        )
        self.assertEqual(recorder.calls, [DEDUP])
        self.assertEqual(report["invoked"], [DEDUP])

    def test_registry_order_follows_manifest(self):
        registry = make_registry()
        priorities = [stage["priority"] for stage in registry["stages"]]
        self.assertEqual(priorities, [100, 200])
        self.assertEqual(
            [stage["package"] for stage in registry["stages"]],
            ["jev-prune-kit", "jev-context-fabric"],
        )


class BoundaryMutationTests(unittest.TestCase):
    def test_canonical_transcript_not_mutated(self):
        request = sample_request()
        before = copy.deepcopy(request)

        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[4]["content"] = "deduped prose"
            return updated

        outgoing, _ = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(request, before)  # caller's request untouched
        self.assertNotEqual(
            outgoing["input"], before["input"]
        )  # outgoing payload replaced
        self.assertEqual(
            outgoing["input"][4]["content"],
            [{"type": "output_text", "text": "deduped prose"}],
        )

    def test_only_supported_shapes_change(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == VIEW:
                updated[4]["content"] = "viewed"
            return updated

        request = sample_request()
        opaque = [request["input"][i] for i in (0, 3)]
        outgoing, _ = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing["input"][0], opaque[0])
        self.assertEqual(outgoing["input"][3], opaque[1])
        self.assertEqual(
            outgoing["input"][4]["content"], [{"type": "output_text", "text": "viewed"}]
        )

    def test_unsupported_mutation_is_refused(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            updated[3]["_jev_raw"]["summary"][0]["text"] = "tampered"
            return updated

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )

    def test_reorder_is_refused(self):
        def transform(name, messages):
            if name != DEDUP:
                return messages
            order = list(messages)
            order[1], order[2] = order[2], order[1]
            return order

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )

    def test_removal_without_an_approved_view_is_reverted(self):
        def transform(name, messages):
            return messages[:-1]

        request = sample_request()
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, before)  # the removed item is restored
        self.assertTrue(
            any(
                note.get("action") == "reverted"
                and note.get("detail") == "unapproved_removal"
                for note in report["notes"]
            )
        )


class BoundaryFallbackTests(unittest.TestCase):
    def test_stage_failure_falls_back_to_stage_input(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[4]["content"] = "deduped prose"
            return updated

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform, fail=VIEW)
        )
        # The failed view stage exits with its input (stage 100's output) intact.
        self.assertEqual(
            outgoing["input"][4]["content"],
            [{"type": "output_text", "text": "deduped prose"}],
        )
        self.assertEqual(report["invoked"], [DEDUP, VIEW])
        self.assertEqual(report["applied"], [DEDUP])
        self.assertTrue(
            any(
                note.get("stage") == VIEW and note.get("action") == "passthrough"
                for note in report["notes"]
            )
        )

    def test_deadline_exhaustion_skips_remaining_stages(self):
        recorder = Recorder()
        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=recorder, chain_timeout_ms=0
        )
        self.assertEqual(recorder.calls, [])
        self.assertEqual(outgoing, request)
        self.assertEqual(report["invoked"], [])
        self.assertTrue(
            all(note.get("action") == "skipped" for note in report["notes"])
        )


class BoundarySwitchTests(unittest.TestCase):
    def test_disabled_switches_pass_through_without_invocation(self):
        def explode(
            stage, request, workspace, budget_ms
        ):  # pragma: no cover - must not run
            raise AssertionError("no stage may be invoked while disabled")

        request = sample_request()
        registry = make_registry(
            {"projection.dedup_receipts": False, "projection.fabric_views": False}
        )
        outgoing, report = bus_boundary.project(
            request, registry=registry, invoke=explode
        )
        self.assertEqual(outgoing, request)
        self.assertEqual(report["invoked"], [])
        self.assertTrue(
            any(note.get("action") == "disabled" for note in report["notes"])
        )

    def test_fabric_requires_dedup(self):
        with self.assertRaises(bus_boundary.BoundaryError) as ctx:
            make_registry(
                {"projection.dedup_receipts": False, "projection.fabric_views": True}
            )
        self.assertEqual(ctx.exception.code, bus_boundary.E_SWITCH_ORDER)

    def test_switch_state_reads_env(self):
        state = bus_boundary.switch_state({"JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS": "1"})
        self.assertTrue(state["projection.dedup_receipts"])
        self.assertFalse(state["projection.fabric_views"])


class BoundaryReceiptTests(unittest.TestCase):
    def test_receipt_emitted_for_applied_stage(self):
        recorder = Recorder(
            notes={DEDUP: [{"stage": DEDUP, "action": "projected", "id": "call_1"}]}
        )
        _, report = bus_boundary.project(
            sample_request(), registry=make_registry(), invoke=recorder
        )
        self.assertEqual(len(report["receipts"]), 2)
        receipt = report["receipts"][0]
        self.assertEqual(receipt["kind"], bus_boundary.RECEIPT_KIND)
        self.assertEqual(receipt["component"], "jev-prune-kit")
        self.assertEqual(receipt["stage"], 100)
        self.assertEqual(receipt["capture_id"], "call_1")

    def test_receipt_is_idempotent(self):
        recorder = Recorder()
        first, _ = bus_boundary.project(
            sample_request(), registry=make_registry(), invoke=recorder, session="s1"
        )
        second, _ = bus_boundary.project(
            sample_request(), registry=make_registry(), invoke=recorder, session="s1"
        )
        self.assertEqual(first, second)


class ReceiptEnforcementTests(unittest.TestCase):
    """The dedup stage may only replace a body that a receipt proves (contract C3)."""

    def _long_request(self):
        # Long enough that the retained-witness marker is strictly shorter.
        body = "FILE A BODY " * 40
        items = [
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"a"}',
                "call_id": "call_1",
            },
            {"type": "function_call_output", "call_id": "call_1", "output": body},
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path":"a"}',
                "call_id": "call_2",
            },
            {"type": "function_call_output", "call_id": "call_2", "output": body},
        ]
        for n in range(16):
            items.append(
                {
                    "type": "reasoning",
                    "id": f"r_{n}",
                    "summary": [],
                    "encrypted_content": None,
                }
            )
        return {"model": "gpt-5-codex", "instructions": "system", "input": items}

    def test_unproven_dedup_edit_is_reverted(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[2]["content"] = dedup_receipts.MARKER.format(witness="call_2")
            return updated

        request = (
            sample_request()
        )  # no duplicate read, so no witness proves a replacement
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing["input"][2]["output"], "FILE A BODY")
        self.assertTrue(
            any(note.get("action") == "reverted" for note in report["notes"])
        )

    def test_proven_dedup_edit_is_accepted(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[1]["content"] = dedup_receipts.MARKER.format(witness="call_2")
            return updated

        request = self._long_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(
            outgoing["input"][1]["output"],
            dedup_receipts.MARKER.format(witness="call_2"),
        )
        self.assertEqual(report["dedup"]["accepted"], [1])
        self.assertEqual(report["dedup"]["receipts"][0]["witness"]["id"], "call_2")
        self.assertEqual(
            request["input"][1]["output"], "FILE A BODY " * 40
        )  # caller untouched


class BoundaryBoundTests(unittest.TestCase):
    def test_wire_bound_refused(self):
        request = sample_request()
        request["input"] = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "x" * (jev_bus.MAX_WIRE + 1)}
                ],
            }
        ]
        with self.assertRaises(bus_boundary.BoundaryError) as ctx:
            bus_boundary.project(request, registry=make_registry(), invoke=Recorder())
        self.assertEqual(ctx.exception.code, bus_boundary.E_WIRE_BOUND)

    def test_missing_input_array_refused(self):
        with self.assertRaises(bus_boundary.BoundaryError) as ctx:
            bus_boundary.project(
                {"model": "m"}, registry=make_registry(), invoke=Recorder()
            )
        self.assertEqual(ctx.exception.code, bus_boundary.E_INPUT_SHAPE)


class VendoredBusTests(unittest.TestCase):
    def test_vendored_bus_matches_pinned_component(self):
        """The host vendors the bus contract byte-identically to the pinned owner."""
        candidate = Path("/home/agent/jev/checkouts/jev-prune-kit/jev_prune/bus.py")
        if not candidate.is_file():
            self.skipTest("pinned jev-prune-kit checkout unavailable")
        vendored = SCRIPTS / "jev_bus.py"
        self.assertEqual(vendored.read_bytes(), candidate.read_bytes())


def prose_request():
    """Two eligible assistant-prose messages ahead of the recent tail."""
    items = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "chatter one"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "chatter two"}],
        },
    ]
    for n in range(16):
        items.append(
            {
                "type": "reasoning",
                "id": f"r_{n}",
                "summary": [],
                "encrypted_content": None,
            }
        )
    return {"model": "gpt-5-codex", "instructions": "system", "input": items}


class BoundaryViewTests(unittest.TestCase):
    """The boundary applies an approved prose view bound to the post-dedup snapshot (C4)."""

    def test_approved_view_removes_prose_and_reports_metrics(self):
        request = prose_request()
        before = copy.deepcopy(request)
        view = fabric_views.apply(
            request["input"],
            fabric_views.plan(request["input"], target_bytes=10**6),
            approved=True,
        )
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(), view=view
        )
        self.assertEqual(len(outgoing["input"]), len(request["input"]) - 2)
        self.assertEqual([r["index"] for r in report["view"]["removed"]], [0, 1])
        self.assertGreater(report["view_metrics"]["bytes_removed"], 0)
        self.assertEqual(report["view_metrics"]["native_compaction_called"], False)
        self.assertEqual(request, before)  # caller never mutated
        # Reasoning (compaction) items survive untouched.
        self.assertTrue(all(i["type"] == "reasoning" for i in outgoing["input"]))

    def test_stale_view_is_refused(self):
        request = prose_request()
        view = fabric_views.apply(
            request["input"],
            fabric_views.plan(request["input"], target_bytes=10**6),
            approved=True,
        )
        changed = copy.deepcopy(request)
        changed["input"][0]["content"] = [{"type": "output_text", "text": "edited"}]
        outgoing, report = bus_boundary.project(
            changed, registry=make_registry(), invoke=Recorder(), view=view
        )
        self.assertEqual(outgoing, changed)
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )

    def test_cancelled_turn_removes_nothing(self):
        request = prose_request()
        view = fabric_views.apply(
            request["input"],
            fabric_views.plan(request["input"], target_bytes=10**6),
            approved=True,
        )
        outgoing, report = bus_boundary.project(
            request,
            registry=make_registry(),
            invoke=Recorder(),
            view=view,
            cancelled=True,
        )
        self.assertEqual(outgoing["input"], request["input"])
        self.assertTrue(report["view"]["cancelled"])


if __name__ == "__main__":
    unittest.main()
