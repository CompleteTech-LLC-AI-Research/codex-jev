#!/usr/bin/env python3
"""Required-CI coverage for duplicate-read proof receipts (contract C3 / #16).

These tests drive `jev/scripts/dedup_receipts.py` directly and through the
`bus_boundary.py` dedup stage. They pin the contract issue #16 asks for: a
projection may replace an older read body only when a receipt proves it (exact
arguments and body on a later retained copy, unique native identities, outside
the protected current turn and recent tail). Stale receipts, changed arguments
or bodies, missing witnesses, concurrent results, and unmarked edits all keep
the original content; enforcement is idempotent and never removes unique
evidence. They also pin the two rules the pinned component enforces on itself
(#52): a marker must be strictly shorter than the body it replaces, and no
receipt may name a witness that is also a source.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import bus_boundary  # noqa: E402
import dedup_receipts  # noqa: E402

DEDUP = "jev-prune.dedup"
VIEW = "jev-context-fabric.view"
ARGS = '{"path":"a"}'
OTHER_ARGS = '{"path":"b"}'
BODY = "FILE A BODY"
# A body long enough that the retained-witness marker is strictly shorter, which
# is what makes a replacement a projection rather than an expansion. Real read
# results are far larger; `BODY` stays short for the refusal cases.
LONG_BODY = "FILE A BODY " * 40


def call(call_id, name="read_file", arguments=ARGS):
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def result(call_id, body=BODY):
    return {"type": "function_call_output", "call_id": call_id, "output": body}


def duplicate_pair():
    return [call("call_1"), result("call_1"), call("call_2"), result("call_2")]


def long_pair():
    return [
        call("call_1"),
        result("call_1", body=LONG_BODY),
        call("call_2"),
        result("call_2", body=LONG_BODY),
    ]


def marker(witness):
    return dedup_receipts.MARKER.format(witness=witness)


class ReceiptContractTests(unittest.TestCase):
    """The proof itself, independent of any particular stage."""

    def test_candidate_binds_source_and_witness(self):
        receipts = dedup_receipts.candidates(
            duplicate_pair(), keep_recent=0, session="s1"
        )
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt["policy"], dedup_receipts.POLICY)
        self.assertEqual(receipt["source"]["id"], "call_1")
        self.assertEqual(receipt["witness"]["id"], "call_2")
        self.assertEqual(receipt["source"]["request"], receipt["witness"]["request"])
        self.assertEqual(receipt["source"]["hash"], receipt["witness"]["hash"])

    def test_proven_replacement_is_accepted(self):
        items = long_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["accepted"], [1])
        self.assertEqual(report["reverted"], [])
        self.assertEqual(out[1]["output"], marker("call_2"))

    def test_changed_body_is_reverted(self):
        items = duplicate_pair()
        items[3]["output"] = "DIFFERENT BODY"
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_CHANGED_BODY)
        self.assertEqual(out[1]["output"], BODY)

    def test_changed_arguments_and_non_read_are_not_candidates(self):
        items = [
            call("call_1"),
            result("call_1"),
            call("call_2", arguments=OTHER_ARGS),
            result("call_2"),
        ]
        self.assertEqual(dedup_receipts.candidates(items, keep_recent=0), [])
        non_read = [
            call("call_1", name="shell"),
            result("call_1"),
            call("call_2", name="shell"),
            result("call_2"),
        ]
        self.assertEqual(dedup_receipts.candidates(non_read, keep_recent=0), [])

    def test_missing_witness_is_reverted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_9")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(
            report["reverted"][0]["reason"], dedup_receipts.R_MISSING_WITNESS
        )
        self.assertEqual(out[1]["output"], BODY)

    def test_concurrent_result_is_reverted(self):
        items = duplicate_pair() + [result("call_2", body="NEWER CONCURRENT BODY")]
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_AMBIGUOUS)
        self.assertEqual(out[1]["output"], BODY)

    def test_unproven_edit_and_non_forward_witness_are_reverted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = "just dropped the body"
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_NOT_MARKER)
        self.assertEqual(out[1]["output"], BODY)
        backward = copy.deepcopy(items)
        backward[3]["output"] = marker("call_1")
        out2, report2 = dedup_receipts.enforce(items, backward, keep_recent=0)
        self.assertEqual(report2["reverted"][0]["reason"], dedup_receipts.R_NOT_FORWARD)
        self.assertEqual(out2[3]["output"], BODY)

    def test_protected_turn_and_tail_are_reverted(self):
        pair = duplicate_pair()
        outgoing = copy.deepcopy(pair)
        outgoing[1]["output"] = marker("call_2")
        _out, tail_report = dedup_receipts.enforce(pair, outgoing, keep_recent=4)
        self.assertEqual(tail_report["accepted"], [])
        turn = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "turn"}],
            }
        ] + pair
        outgoing = copy.deepcopy(turn)
        outgoing[2]["output"] = marker("call_2")
        _out2, turn_report = dedup_receipts.enforce(turn, outgoing, keep_recent=0)
        self.assertEqual(turn_report["accepted"], [])
        self.assertEqual(
            turn_report["reverted"][0]["reason"], dedup_receipts.R_PROTECTED_TURN
        )

    def test_enforcement_is_idempotent_and_keeps_unique_evidence(self):
        items = long_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        once, _ = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        twice, report = dedup_receipts.enforce(items, once, keep_recent=0)
        self.assertEqual(once, twice)
        self.assertEqual(report["accepted"], [1])
        unique = [
            call("call_1", arguments=OTHER_ARGS),
            result("call_1", body="UNIQUE"),
            call("call_2"),
            result("call_2"),
        ]
        out, report = dedup_receipts.enforce(
            unique, copy.deepcopy(unique), keep_recent=0
        )
        self.assertEqual(out, unique)
        self.assertEqual(report["accepted"], [])

    def test_length_change_is_refused(self):
        items = duplicate_pair()
        with self.assertRaises(dedup_receipts.ReceiptError):
            dedup_receipts.enforce(items, items[:-1], keep_recent=0)

    def test_non_reducing_marker_is_reverted(self):
        """A marker longer than the body it replaces saves nothing (#52 G2).

        The pinned component refuses a marker that is not strictly shorter, so
        accepting one here would leave the host's guard weaker than the validator
        it mirrors - and would grow the request.
        """

        items = [
            call("call_1"),
            result("call_1", body="aaaa"),
            call("call_2"),
            result("call_2", body="aaaa"),
        ]
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(
            report["reverted"][0]["reason"], dedup_receipts.R_NON_REDUCING
        )
        self.assertEqual(out[1]["output"], "aaaa")
        # The acceptance criterion is about the serialized request, not the body.
        self.assertLessEqual(len(json.dumps(out)), len(json.dumps(items)))

    def test_reducing_marker_is_still_accepted(self):
        """The reduction rule must not reject a genuine projection."""

        for size in (255, 256, 2000):
            with self.subTest(size=size):
                body = "x" * size
                items = [
                    call("call_1"),
                    result("call_1", body=body),
                    call("call_2"),
                    result("call_2", body=body),
                ]
                outgoing = copy.deepcopy(items)
                outgoing[1]["output"] = marker("call_2")
                out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
                self.assertEqual(report["accepted"], [1])
                self.assertLess(len(json.dumps(out)), len(json.dumps(items)))

    def test_marker_chain_is_reverted_whole(self):
        """A witness that is also a source makes the set unverifiable (#52 G1).

        `call_1 -> call_2` plus `call_2 -> call_3` leaves a receipt whose witness
        body this same request has replaced, so the receipt is no longer
        re-derivable from the request it accompanies. The component refuses the
        set as "Receipt dependencies conflict"; the host reverts it whole.
        """

        items = [
            call("call_1"),
            result("call_1", body=LONG_BODY),
            call("call_2"),
            result("call_2", body=LONG_BODY),
            call("call_3"),
            result("call_3", body=LONG_BODY),
        ]
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        outgoing[3]["output"] = marker("call_3")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(
            [entry["reason"] for entry in report["reverted"]],
            [dedup_receipts.R_DEP_CONFLICT, dedup_receipts.R_DEP_CONFLICT],
        )
        # Every hashed witness body is still present in the emitted request.
        for index in (1, 3, 5):
            self.assertEqual(out[index]["output"], LONG_BODY)


class Recorder:
    """A deterministic stage spy that edits the bus message array in place."""

    def __init__(self, transform=None):
        self.calls = []
        self._transform = transform

    def __call__(self, stage, request, workspace, budget_ms):
        name = stage["name"]
        self.calls.append(name)
        messages = request["messages"]
        if self._transform is not None:
            messages = self._transform(name, messages)
        return {"ok": True, "messages": messages, "notes": []}


def enabled_state(**overrides):
    state = {"projection.dedup_receipts": True, "projection.fabric_views": True}
    state.update(overrides)
    return state


def make_registry(state=None):
    return bus_boundary.build_registry(
        state if state is not None else enabled_state(),
        {"dedup": ["dedup"], "fabric_view": ["view"]},
    )


def boundary_request(items):
    return {"model": "gpt-5-codex", "instructions": "system", "input": items}


def single_read_request():
    """One read with no retained duplicate, so no witness can prove a replacement."""
    return boundary_request(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
            call("call_1"),
            result("call_1"),
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
        ]
    )


def long_request():
    """A duplicate read far enough back that a later witness is retained."""
    items = long_pair()
    for n in range(16):
        items.append(
            {
                "type": "reasoning",
                "id": f"r_{n}",
                "summary": [],
                "encrypted_content": None,
            }
        )
    return boundary_request(items)


class BoundaryEnforcementTests(unittest.TestCase):
    """The boundary keeps only the dedup edits a receipt proves (contract C3)."""

    def test_unproven_dedup_edit_is_reverted(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[2]["content"] = marker("call_2")
            return updated

        request = single_read_request()  # no duplicate, so no witness proves it
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing["input"][2]["output"], BODY)
        self.assertEqual(request, before)  # caller's request is never mutated
        self.assertTrue(
            any(note.get("action") == "reverted" for note in report["notes"])
        )

    def test_proven_dedup_edit_is_accepted(self):
        def transform(name, messages):
            updated = copy.deepcopy(messages)
            if name == DEDUP:
                updated[1]["content"] = marker("call_2")
            return updated

        request = long_request()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing["input"][1]["output"], marker("call_2"))
        self.assertEqual(report["dedup"]["accepted"], [1])
        self.assertEqual(report["dedup"]["receipts"][0]["witness"]["id"], "call_2")
        self.assertEqual(request["input"][1]["output"], LONG_BODY)  # caller untouched

    def test_dedup_stage_is_owned_by_jev_prune_kit(self):
        registry = make_registry()
        self.assertEqual(
            [stage["priority"] for stage in registry["stages"]], [100, 200]
        )
        self.assertEqual(
            [stage["package"] for stage in registry["stages"]],
            ["jev-prune-kit", "jev-context-fabric"],
        )


if __name__ == "__main__":
    unittest.main()
