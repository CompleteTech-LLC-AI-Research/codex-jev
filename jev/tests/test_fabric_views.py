"""Tests for approved, reversible Fabric prose views (contract C4 / issue #17).

They cover plan binding, explicit approval, stale rejection, exact reset,
cancellation, eligibility (prose only), and that bytes and tokens are reported
separately.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fabric_views


def assistant(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def user(text):
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def reasoning(n):
    return {
        "type": "reasoning",
        "id": f"r_{n}",
        "summary": [],
        "encrypted_content": None,
    }


def array_with_prose():
    return [assistant("chatter one"), assistant("chatter two")] + [
        reasoning(n) for n in range(16)
    ]


class PlanTests(unittest.TestCase):
    def test_plan_previews_without_mutating(self):
        items = array_with_prose()
        before = copy.deepcopy(items)
        doc = fabric_views.plan(items, goal="g", target_bytes=10**6)
        self.assertEqual(items, before)
        self.assertEqual([c["index"] for c in doc["candidates"]], [0, 1])
        self.assertEqual(doc["fingerprint"], fabric_views.snapshot(items))
        self.assertFalse(doc["applied"])
        self.assertGreater(doc["estimated_bytes_removed"], 0)
        self.assertIn("plan_id", doc)

    def test_guard_and_non_assistant_are_not_eligible(self):
        items = [
            user("hello"),
            assistant("you must never do that"),  # guarded
            {
                "type": "function_call",
                "name": "read",
                "arguments": "{}",
                "call_id": "c",
            },
        ] + [reasoning(n) for n in range(16)]
        doc = fabric_views.plan(items, target_bytes=10**6)
        self.assertEqual(doc["candidates"], [])

    def test_prose_in_the_recent_tail_is_not_eligible(self):
        items = [reasoning(n) for n in range(16)] + [assistant("late chatter")]
        self.assertEqual(fabric_views.plan(items, target_bytes=10**6)["candidates"], [])


class ApplyTests(unittest.TestCase):
    def test_apply_requires_explicit_approval(self):
        items = array_with_prose()
        doc = fabric_views.plan(items, target_bytes=10**6)
        with self.assertRaises(fabric_views.ViewError) as ctx:
            fabric_views.apply(items, doc, approved=False)
        self.assertEqual(str(ctx.exception), "view_not_approved")

    def test_stale_plan_is_rejected(self):
        items = array_with_prose()
        doc = fabric_views.plan(items, target_bytes=10**6)
        changed = copy.deepcopy(items)
        changed[0]["content"] = [{"type": "output_text", "text": "edited"}]
        with self.assertRaises(fabric_views.ViewError) as ctx:
            fabric_views.apply(changed, doc, approved=True)
        self.assertEqual(str(ctx.exception), "stale_plan")

    def test_approved_view_binds_snapshot_and_keys(self):
        items = array_with_prose()
        doc = fabric_views.plan(items, target_bytes=10**6)
        view = fabric_views.apply(items, doc, approved=True)
        self.assertEqual(view["kind"], fabric_views.KIND)
        self.assertEqual(view["fingerprint"], fabric_views.snapshot(items))
        self.assertEqual(
            view["keys"],
            [
                fabric_views.message_key(items[0], 0),
                fabric_views.message_key(items[1], 1),
            ],
        )

    def test_empty_plan_is_rejected(self):
        items = [reasoning(n) for n in range(16)]
        doc = fabric_views.plan(items, target_bytes=10**6)
        with self.assertRaises(fabric_views.ViewError) as ctx:
            fabric_views.apply(items, doc, approved=True)
        self.assertEqual(str(ctx.exception), "empty_plan")


class FilterTests(unittest.TestCase):
    def _view(self, items):
        return fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )

    def test_filter_drops_only_approved_prose(self):
        items = array_with_prose()
        view = self._view(items)
        out, report = fabric_views.filter(items, view)
        self.assertEqual([r["index"] for r in report["removed"]], [0, 1])
        self.assertTrue(all(i["type"] == "reasoning" for i in out))
        self.assertEqual(len(out), len(items) - 2)

    def test_filter_refuses_a_stale_view(self):
        items = array_with_prose()
        view = self._view(items)
        changed = copy.deepcopy(items)
        changed[2] = reasoning(99)
        with self.assertRaises(fabric_views.ViewError) as ctx:
            fabric_views.filter(changed, view)
        self.assertEqual(str(ctx.exception), "stale_view")

    def test_cancelled_filter_removes_nothing(self):
        items = array_with_prose()
        view = self._view(items)
        out, report = fabric_views.filter(items, view, cancelled=True)
        self.assertEqual(out, items)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["removed"], [])

    def test_reset_restores_the_original_bytes(self):
        items = array_with_prose()
        view = self._view(items)
        out, _ = fabric_views.filter(items, view)
        self.assertEqual(fabric_views.reset(view), None)
        # After reset the caller keeps the original array; the view is discarded.
        self.assertEqual(items, array_with_prose())
        self.assertNotEqual(out, items)


class MetricsTests(unittest.TestCase):
    def test_bytes_and_tokens_are_reported_separately(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, _ = fabric_views.filter(items, view)
        m = fabric_views.metrics(items, out)
        self.assertGreater(m["bytes_removed"], 0)
        self.assertIsNone(m["tokens_measured"])  # no counter supplied
        self.assertIn("note", m["tokens_estimated"])
        self.assertFalse(m["native_compaction_called"])

    def test_measured_tokens_use_the_supplied_counter(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, _ = fabric_views.filter(items, view)
        m = fabric_views.metrics(items, out, token_counter=lambda v: len(v))
        self.assertEqual(
            m["tokens_measured"], {"before": len(items), "after": len(out)}
        )


if __name__ == "__main__":
    unittest.main()
