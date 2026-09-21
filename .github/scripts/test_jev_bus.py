#!/usr/bin/env python3
"""Required-CI coverage for the native request adapter and jev-bus boundary (#15).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the acceptance
criteria for issue #15 are pinned here rather than only in `jev/tests`, which
required CI does not run. The contract is C2 in `jev/CONTRACTS.md`: the host
builds one outgoing request, invokes exactly one bus owner in manifest order,
never mutates the canonical transcript, and lets only supported message shapes
change. The C3 receipt enforcement that gates replacements is pinned separately
in `test_jev_receipts.py`; this file drives it through the boundary and the
transport.

The bus stages themselves live in separate repositories that CI does not check
out, so the transport half of these tests drives the checked-in doubles in
`jev/tests/bus_stage_stub/`. Every result from those doubles is tier
`bus-stage-stub`, never real component evidence.
"""

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import bus_boundary  # noqa: E402
import dedup_receipts  # noqa: E402
import jev_bus  # noqa: E402

DEDUP = "jev-prune.dedup"
VIEW = "jev-context-fabric.view"
STUBS = REPO_ROOT / "jev" / "tests" / "bus_stage_stub"
ARGS = '{"path":"a"}'
BODY = "FILE A BODY " * 40
PROSE = "done"

ENABLED = {"projection.dedup_receipts": True, "projection.fabric_views": True}
DISABLED = {"projection.dedup_receipts": False, "projection.fabric_views": False}


def call(call_id):
    return {
        "type": "function_call",
        "name": "read_file",
        "arguments": ARGS,
        "call_id": call_id,
    }


def result(call_id, body=BODY):
    return {"type": "function_call_output", "call_id": call_id, "output": body}


def reasoning(index):
    return {
        "type": "reasoning",
        "id": f"r_{index}",
        "summary": [{"type": "summary_text", "text": "think"}],
        "encrypted_content": None,
    }


def message(role, text, kind):
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def marker(witness):
    return dedup_receipts.MARKER.format(witness=witness)


def sample_request():
    """A Codex-shaped request: a duplicate read pair, an opaque item, and prose.

    The pair sits in the protected tail, so C3 can never prove a replacement
    here: `long_request` is the provable shape.
    """
    return {
        "model": "gpt-5-codex",
        "instructions": "system",
        "input": [
            message("user", "please read a", "input_text"),
            call("call_1"),
            result("call_1"),
            reasoning(1),
            call("call_2"),
            result("call_2"),
            message("assistant", PROSE, "output_text"),
        ],
    }


DEDUP_INDEX = 1
VIEW_INDEX = 21
OPAQUE_INDEX = 3


def long_request():
    """The duplicate pair sits outside the protected tail, so a marker is provable.

    `dedup_receipts` protects the current user turn and the last 16 items, so the
    pair has to be pushed back before the last user message for the replacement of
    the older body by a retained witness to hold a receipt.
    """
    items = [call("call_1"), result("call_1"), call("call_2"), result("call_2")]
    items += [reasoning(index) for index in range(16)]
    items += [
        message("user", "summarize a", "input_text"),
        message("assistant", PROSE, "output_text"),
    ]
    return {"model": "gpt-5-codex", "instructions": "system", "input": items}


def registry(state=None):
    return bus_boundary.build_registry(
        ENABLED if state is None else state,
        {"dedup": ["dedup"], "fabric_view": ["view"]},
    )


class Recorder:
    """A stage spy that records invocation order and returns what it was given."""

    def __init__(self, transform=None, fail=None, notes=None):
        self.calls = []
        self._transform = transform
        self._fail = fail
        self._notes = notes or {}

    def __call__(self, stage, request, workspace, budget_ms):
        name = stage["name"]
        self.calls.append(name)
        if self._fail and name == self._fail:
            raise jev_bus.BusError("stage declined")
        messages = request["messages"]
        if self._transform is not None:
            messages = self._transform(name, messages)
        return {"ok": True, "messages": messages, "notes": list(self._notes.get(name, []))}


def prove(source_index, witness="call_2"):
    """A stage transform that substitutes one proved read body in place."""

    def transform(name, messages):
        updated = copy.deepcopy(messages)
        if name == DEDUP:
            updated[source_index]["content"] = marker(witness)
        return updated

    return transform


def view_prose(index, text="[Jev view: approved prose view applied]"):
    def transform(name, messages):
        updated = copy.deepcopy(messages)
        if name == VIEW:
            updated[index]["content"] = text
        return updated

    return transform


class OrderedSingleInvocationTests(unittest.TestCase):
    def test_each_enabled_stage_is_invoked_once_in_manifest_order(self):
        recorder = Recorder()
        _, report = bus_boundary.project(
            sample_request(), registry=registry(), invoke=recorder
        )
        self.assertEqual(recorder.calls, [DEDUP, VIEW])
        self.assertEqual(report["invoked"], [DEDUP, VIEW])

    def test_registry_priorities_and_owners_come_from_the_manifest(self):
        stages = registry()["stages"]
        self.assertEqual([stage["priority"] for stage in stages], [100, 200])
        self.assertEqual(
            [stage["package"] for stage in stages],
            ["jev-prune-kit", "jev-context-fabric"],
        )

    def test_only_the_enabled_stage_runs(self):
        recorder = Recorder()
        _, report = bus_boundary.project(
            sample_request(),
            registry=registry(dict(ENABLED, **{"projection.fabric_views": False})),
            invoke=recorder,
        )
        self.assertEqual(recorder.calls, [DEDUP])
        self.assertEqual(report["invoked"], [DEDUP])


class CanonicalTranscriptTests(unittest.TestCase):
    def test_supported_shapes_change_only_in_the_outgoing_payload(self):
        request = long_request()
        before = copy.deepcopy(request)

        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[DEDUP_INDEX]["content"] = marker("call_2")
            elif name == VIEW:
                updated[VIEW_INDEX]["content"] = "viewed"
            return updated

        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=Recorder(transform)
        )
        self.assertEqual(request, before)
        self.assertNotEqual(outgoing["input"], before["input"])
        # The proved tool-result replacement and the prose rewrite both land.
        self.assertEqual(outgoing["input"][DEDUP_INDEX]["output"], marker("call_2"))
        self.assertEqual(
            outgoing["input"][VIEW_INDEX]["content"],
            [{"type": "output_text", "text": "viewed"}],
        )
        self.assertEqual(report["applied"], [DEDUP, VIEW])
        self.assertEqual(report["dedup"]["accepted"], [DEDUP_INDEX])

    def test_in_place_opaque_edit_cannot_reach_the_caller_or_the_payload(self):
        """An in-process stage edits the tag it was handed, with no deep copy."""

        def transform(name, messages):
            if name == DEDUP:
                messages[OPAQUE_INDEX]["_jev_raw"]["summary"][0]["text"] = "tampered"
            return messages

        request = sample_request()
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=Recorder(transform)
        )
        self.assertEqual(request, before)
        self.assertEqual(outgoing, request)
        self.assertEqual(outgoing["input"][OPAQUE_INDEX], before["input"][OPAQUE_INDEX])
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )

    def test_replaced_opaque_edit_is_refused(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[OPAQUE_INDEX]["_jev_raw"]["summary"][0]["text"] = "tampered"
            return updated

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )

    def test_unsupported_shapes_are_retained_verbatim(self):
        request = sample_request()
        outgoing, _ = bus_boundary.project(
            request, registry=registry(), invoke=Recorder(view_prose(6, "viewed"))
        )
        self.assertEqual(outgoing["input"][OPAQUE_INDEX], request["input"][OPAQUE_INDEX])
        self.assertEqual(outgoing["input"][1], request["input"][1])
        self.assertEqual(
            outgoing["input"][6]["content"], [{"type": "output_text", "text": "viewed"}]
        )

    def test_reorder_or_removal_is_refused_and_discards_every_stage(self):
        def transform(name, messages):
            if name == DEDUP:
                messages[1], messages[2] = messages[2], messages[1]
            elif name == VIEW:
                return messages[:-1]
            return messages

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        self.assertTrue(
            any(note.get("action") == "refused" for note in report["notes"])
        )


class SelectionAndBoundsTests(unittest.TestCase):
    def test_disabled_switches_pass_through_without_invoking_anything(self):
        def explode(stage, request, workspace, budget_ms):  # pragma: no cover
            raise AssertionError("a disabled stage must never be invoked")

        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=registry(DISABLED), invoke=explode
        )
        self.assertEqual(outgoing, request)
        self.assertEqual(report["invoked"], [])
        self.assertTrue(
            any(note.get("action") == "disabled" for note in report["notes"])
        )

    def test_fabric_view_requires_dedup(self):
        with self.assertRaises(bus_boundary.BoundaryError) as ctx:
            registry(
                {"projection.dedup_receipts": False, "projection.fabric_views": True}
            )
        self.assertEqual(ctx.exception.code, bus_boundary.E_SWITCH_ORDER)

    def test_unrelated_switches_do_not_enable_projection(self):
        state = bus_boundary.switch_state(
            {
                "JEV_SWITCH_REMOTE_INFERENCE_ENABLED": "1",
                "JEV_SWITCH_CAPTURE_CANONICAL_EVIDENCE": "1",
            }
        )
        self.assertFalse(state["projection.dedup_receipts"])
        self.assertFalse(state["projection.fabric_views"])

    def test_request_above_the_wire_bound_is_refused(self):
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
            bus_boundary.project(request, registry=registry(), invoke=Recorder())
        self.assertEqual(ctx.exception.code, bus_boundary.E_WIRE_BOUND)

    def test_request_without_an_input_array_is_refused(self):
        with self.assertRaises(bus_boundary.BoundaryError) as ctx:
            bus_boundary.project({"model": "m"}, registry=registry(), invoke=Recorder())
        self.assertEqual(ctx.exception.code, bus_boundary.E_INPUT_SHAPE)

    def test_exhausted_chain_deadline_skips_every_stage(self):
        recorder = Recorder()
        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=recorder, chain_timeout_ms=0
        )
        self.assertEqual(recorder.calls, [])
        self.assertEqual(outgoing, request)
        self.assertEqual(report["invoked"], [])

    def test_stage_failure_keeps_the_earlier_stages_outgoing_payload(self):
        request = long_request()
        outgoing, report = bus_boundary.project(
            request,
            registry=registry(),
            invoke=Recorder(prove(DEDUP_INDEX), fail=VIEW),
        )
        self.assertEqual(outgoing["input"][DEDUP_INDEX]["output"], marker("call_2"))
        self.assertEqual(report["applied"], [DEDUP])
        self.assertTrue(
            any(
                note.get("stage") == VIEW and note.get("action") == "passthrough"
                for note in report["notes"]
            )
        )


class ReceiptRuleTests(unittest.TestCase):
    """Which stages hold a projection receipt, and what the note wording may not do."""

    def test_projection_earns_its_receipt_despite_a_passthrough_note(self):
        """The pinned stage 100 reports substitutions and unrelated notes together.

        Its note wording is another package's, so a stage that changed the array
        must not lose its receipt because one of its notes says ``passthrough``.
        """
        recorder = Recorder(
            prove(DEDUP_INDEX),
            notes={
                DEDUP: [
                    {"action": "duplicate read bodies substituted", "count": 1},
                    {"action": "passthrough", "detail": "1 stale receipt ignored"},
                ]
            },
        )
        outgoing, report = bus_boundary.project(
            long_request(), registry=registry(), invoke=recorder
        )
        self.assertEqual(outgoing["input"][DEDUP_INDEX]["output"], marker("call_2"))
        self.assertEqual(report["applied"], [DEDUP, VIEW])
        self.assertEqual([r["stage"] for r in report["receipts"]], [100, 200])

    def test_a_passthrough_note_cannot_skip_receipt_enforcement(self):
        """The classification must never gate C3: the revert still runs."""
        recorder = Recorder(
            prove(2, witness="call_2"),
            notes={DEDUP: [{"action": "passthrough", "detail": "1 stale receipt"}]},
            )
        request = sample_request()  # the pair is protected, so this is unproven
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=recorder
        )
        self.assertEqual(outgoing["input"][2]["output"], BODY)
        self.assertEqual(
            report["dedup"]["reverted"],
            [{"index": 2, "reason": dedup_receipts.R_PROTECTED_TURN}],
        )
        self.assertEqual(report["applied"], [VIEW])
        self.assertEqual([r["stage"] for r in report["receipts"]], [200])

    def test_a_stage_that_hands_the_array_back_is_not_applied(self):
        recorder = Recorder(
            notes={DEDUP: [{"action": "passthrough", "detail": "no duplicate reads"}]}
        )
        request = sample_request()
        outgoing, report = bus_boundary.project(
            request, registry=registry(), invoke=recorder
        )
        self.assertEqual(report["invoked"], [DEDUP, VIEW])
        self.assertEqual(report["applied"], [VIEW])
        self.assertEqual([r["stage"] for r in report["receipts"]], [200])
        self.assertEqual(outgoing, request)

    def test_receipt_carries_the_stage_identity(self):
        recorder = Recorder(
            view_prose(6),
            notes={VIEW: [{"action": "approved prose view applied", "id": "view_1"}]},
        )
        _, report = bus_boundary.project(
            sample_request(),
            registry=registry(),
            invoke=recorder,
            session="s1",
            turn="t1",
            workspace="w1",
        )
        receipt = next(r for r in report["receipts"] if r["stage"] == 200)
        self.assertEqual(receipt["kind"], bus_boundary.RECEIPT_KIND)
        self.assertEqual(receipt["component"], "jev-context-fabric")
        self.assertEqual(receipt["stage"], 200)
        self.assertEqual(receipt["capture_id"], "view_1")
        self.assertEqual(receipt["session_id"], "s1")
        self.assertEqual(receipt["turn_id"], "t1")
        self.assertEqual(receipt["origin_workspace"], "w1")

    def test_stage_100_receipt_does_not_yet_bind_the_proved_identity(self):
        """Characterizes the known limit recorded in `jev/BUS_BOUNDARY.md`.

        The pinned stage 100 note carries no id, so the receipt's `capture_id`
        stays empty even though the C3 proof names the retained witness. Binding
        that witness identity onto the receipt is #16's projection accounting.
        """
        outgoing, report = bus_boundary.project(
            long_request(), registry=registry(), invoke=Recorder(prove(DEDUP_INDEX))
        )
        self.assertEqual(outgoing["input"][DEDUP_INDEX]["output"], marker("call_2"))
        self.assertEqual(report["dedup"]["receipts"][0]["witness"]["id"], "call_2")
        self.assertEqual(report["applied"], [DEDUP, VIEW])
        receipt = next(r for r in report["receipts"] if r["stage"] == 100)
        self.assertEqual(receipt["capture_id"], "")


class SubprocessTransportTests(unittest.TestCase):
    """Drives the real subprocess transport with the checked-in stage doubles."""

    def transport(self):
        return {
            "dedup": [sys.executable, str(STUBS / "dedup.py")],
            "fabric_view": [sys.executable, str(STUBS / "view.py")],
        }

    def test_outgoing_request_is_projected_in_place_in_order(self):
        request = long_request()
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request,
            registry=bus_boundary.build_registry(ENABLED, self.transport()),
            session="s1",
            turn="t1",
            workspace="w1",
        )
        self.assertEqual(report["invoked"], [DEDUP, VIEW])
        self.assertEqual(report["applied"], [DEDUP, VIEW])
        self.assertEqual(outgoing["input"][DEDUP_INDEX]["output"], marker("call_2"))
        self.assertEqual(outgoing["input"][3]["output"], BODY)  # retained witness
        self.assertEqual(
            outgoing["input"][VIEW_INDEX]["content"],
            [{"type": "output_text", "text": "[Jev view: approved prose view applied]"}],
        )
        self.assertEqual(len(outgoing["input"]), len(before["input"]))
        self.assertEqual(outgoing["input"][2], before["input"][2])
        self.assertEqual(request, before)

    def test_unproven_substitution_is_reverted_across_the_transport(self):
        """The stub substitutes on the wire; the host still reverts it (C3)."""
        request = sample_request()  # the pair is inside the protected tail
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request,
            registry=bus_boundary.build_registry(ENABLED, self.transport()),
            session="s1",
        )
        self.assertEqual(report["invoked"], [DEDUP, VIEW])
        self.assertEqual(outgoing["input"][2]["output"], BODY)
        self.assertEqual(
            report["dedup"]["reverted"],
            [{"index": 2, "reason": dedup_receipts.R_PROTECTED_TURN}],
        )
        self.assertTrue(
            any(note.get("action") == "reverted" for note in report["notes"])
        )
        differing = [
            index
            for index, (produced, original) in enumerate(
                zip(outgoing["input"], before["input"])
            )
            if produced != original
        ]
        self.assertEqual(differing, [6])  # only stage 200's prose view changed
        self.assertEqual(request, before)  # the caller's request is never touched

    def test_disabled_switches_leave_the_request_byte_identical(self):
        request = long_request()
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request,
            registry=bus_boundary.build_registry(DISABLED, self.transport()),
        )
        self.assertEqual(outgoing, before)
        self.assertEqual(report["invoked"], [])

    def test_receipts_are_emitted_once_per_applied_stage(self):
        request = long_request()
        _, report = bus_boundary.project(
            request,
            registry=bus_boundary.build_registry(ENABLED, self.transport()),
            session="s1",
            workspace="w1",
        )
        self.assertEqual(
            [(r["stage"], r["component"], r["kind"]) for r in report["receipts"]],
            [
                (100, "jev-prune-kit", "projection_receipt"),
                (200, "jev-context-fabric", "projection_receipt"),
            ],
        )


class VendoredContractTests(unittest.TestCase):
    def test_vendored_bus_declares_the_manifest_stage_schema(self):
        self.assertEqual(jev_bus.SCHEMA, "jev-bus.v1")
        self.assertEqual(jev_bus.STAGE_SCHEMA, "jev-bus.stage.v1")


if __name__ == "__main__":
    unittest.main()
