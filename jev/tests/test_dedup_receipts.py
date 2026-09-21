"""Tests for duplicate-read proof receipts (contract C3 / issue #16).

They cover stale receipts, changed arguments, missing witnesses, concurrent
results, the protected turn and tail, idempotence, and that unique evidence is
never removed.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dedup_receipts

ARGS = '{"path":"a"}'
OTHER_ARGS = '{"path":"b"}'
BODY = "FILE A BODY"


def msg(role, text):
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def call(call_id, name="read_file", arguments=ARGS):
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def result(call_id, body=BODY):
    return {"type": "function_call_output", "call_id": call_id, "output": body}


def marker(witness):
    return dedup_receipts.MARKER.format(witness=witness)


def duplicate_pair():
    return [call("call_1"), result("call_1"), call("call_2"), result("call_2")]


def enforced(items, outgoing):
    return dedup_receipts.enforce(items, outgoing, keep_recent=0)


class CandidateTests(unittest.TestCase):
    def test_candidate_receipt_binds_both_identities(self):
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

    def test_no_candidate_without_a_duplicate(self):
        items = [call("call_1"), result("call_1")]
        self.assertEqual(dedup_receipts.candidates(items, keep_recent=0), [])

    def test_changed_arguments_is_not_a_candidate(self):
        items = [
            call("call_1"),
            result("call_1"),
            call("call_2", arguments=OTHER_ARGS),
            result("call_2"),
        ]
        self.assertEqual(dedup_receipts.candidates(items, keep_recent=0), [])

    def test_non_read_tool_is_not_a_candidate(self):
        items = [
            call("call_1", name="shell"),
            result("call_1"),
            call("call_2", name="shell"),
            result("call_2"),
        ]
        self.assertEqual(dedup_receipts.candidates(items, keep_recent=0), [])


class EnforceTests(unittest.TestCase):
    def test_valid_replacement_is_accepted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = enforced(items, outgoing)
        self.assertEqual(report["accepted"], [1])
        self.assertEqual(report["reverted"], [])
        self.assertEqual(out[1]["output"], marker("call_2"))

    def test_stale_receipt_changed_body_is_reverted(self):
        items = duplicate_pair()
        items[3]["output"] = "DIFFERENT BODY"
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = enforced(items, outgoing)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_CHANGED_BODY)
        self.assertEqual(out[1]["output"], BODY)

    def test_changed_arguments_is_reverted(self):
        items = duplicate_pair()
        items[2]["arguments"] = OTHER_ARGS
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = enforced(items, outgoing)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_CHANGED_ARGS)
        self.assertEqual(out[1]["output"], BODY)

    def test_missing_witness_is_reverted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_9")
        out, report = enforced(items, outgoing)
        self.assertEqual(
            report["reverted"][0]["reason"], dedup_receipts.R_MISSING_WITNESS
        )
        self.assertEqual(out[1]["output"], BODY)

    def test_concurrent_results_are_reverted(self):
        items = duplicate_pair() + [result("call_2", body="NEWER CONCURRENT BODY")]
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = enforced(items, outgoing)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_AMBIGUOUS)
        self.assertEqual(out[1]["output"], BODY)

    def test_witness_must_be_the_retained_later_copy(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[3]["output"] = marker("call_1")
        out, report = enforced(items, outgoing)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_NOT_FORWARD)
        self.assertEqual(out[3]["output"], BODY)

    def test_unproven_replacement_is_reverted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = "just dropped the body"
        out, report = enforced(items, outgoing)
        self.assertEqual(report["reverted"][0]["reason"], dedup_receipts.R_NOT_MARKER)
        self.assertEqual(out[1]["output"], BODY)

    def test_protected_recent_tail_is_reverted(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(
            items, outgoing, keep_recent=4
        )  # everything is protected
        self.assertEqual(report["accepted"], [])
        self.assertIn(
            report["reverted"][0]["reason"],
            {dedup_receipts.R_PROTECTED_TAIL, dedup_receipts.R_PROTECTED_TURN},
        )
        self.assertEqual(out[1]["output"], BODY)

    def test_protected_current_turn_is_reverted(self):
        # The duplicate pair sits inside the current user turn.
        items = [msg("user", "turn")] + duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[2]["output"] = marker("call_2")
        out, report = dedup_receipts.enforce(items, outgoing, keep_recent=0)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(
            report["reverted"][0]["reason"], dedup_receipts.R_PROTECTED_TURN
        )
        self.assertEqual(out[2]["output"], BODY)

    def test_enforcement_is_idempotent(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        once, _ = enforced(items, outgoing)
        twice, report = enforced(items, once)
        self.assertEqual(once, twice)
        self.assertEqual(report["accepted"], [1])

    def test_unique_evidence_is_never_removed(self):
        items = [
            call("call_1", arguments=OTHER_ARGS),
            result("call_1", body="UNIQUE"),
            call("call_2"),
            result("call_2"),
        ]
        outgoing = copy.deepcopy(items)
        out, report = enforced(items, outgoing)
        self.assertEqual(out, items)
        self.assertEqual(report["accepted"], [])

    def test_pairing_and_envelopes_preserved(self):
        items = duplicate_pair()
        outgoing = copy.deepcopy(items)
        outgoing[1]["output"] = marker("call_2")
        out, _ = enforced(items, outgoing)
        self.assertEqual(len(out), len(items))
        self.assertEqual([i["type"] for i in out], [i["type"] for i in items])
        for index in (0, 2, 3):
            self.assertEqual(out[index], items[index])

    def test_prose_view_change_is_not_policed(self):
        items = [msg("user", "hi"), msg("assistant", "old prose")]
        outgoing = copy.deepcopy(items)
        outgoing[1]["content"] = [{"type": "output_text", "text": "viewed"}]
        out, report = enforced(items, outgoing)
        self.assertEqual(out[1]["content"], [{"type": "output_text", "text": "viewed"}])
        self.assertEqual(report["accepted"], [])

    def test_length_change_is_refused(self):
        items = duplicate_pair()
        with self.assertRaises(dedup_receipts.ReceiptError):
            enforced(items, items[:-1])


if __name__ == "__main__":
    unittest.main()
