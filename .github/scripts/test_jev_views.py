#!/usr/bin/env python3
"""Required-CI coverage for approved, reversible Fabric prose views (C4 / #17).

These tests drive `jev/scripts/fabric_views.py` directly and through
`bus_boundary.py`. They pin the contract issue #17 asks for: an approved view is
bound to the exact post-dedup snapshot and rejected when that snapshot changed,
approval is explicit, reset restores the original bytes, cancellation removes
nothing, only standalone assistant prose is eligible (compaction items survive),
and byte counts and token figures are reported separately.
"""

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import bus_boundary  # noqa: E402
import fabric_views  # noqa: E402

DEDUP = "jev-prune.dedup"


def assistant(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
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


def request_with_prose():
    return {
        "model": "gpt-5-codex",
        "instructions": "system",
        "input": array_with_prose(),
    }


class Recorder:
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


class ViewControlTests(unittest.TestCase):
    """The reversible control itself, independent of the boundary."""

    def test_plan_previews_without_mutating_and_binds_the_snapshot(self):
        items = array_with_prose()
        before = copy.deepcopy(items)
        doc = fabric_views.plan(items, goal="g", target_bytes=10**6)
        self.assertEqual(items, before)
        self.assertEqual([c["index"] for c in doc["candidates"]], [0, 1])
        self.assertEqual(doc["fingerprint"], fabric_views.snapshot(items))
        self.assertFalse(doc["applied"])

    def test_only_prose_is_eligible(self):
        items = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "you must never do that"}],
            },
            {
                "type": "function_call",
                "name": "read",
                "arguments": "{}",
                "call_id": "c",
            },
        ] + [reasoning(n) for n in range(16)]
        self.assertEqual(fabric_views.plan(items, target_bytes=10**6)["candidates"], [])

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

    def test_stale_view_filters_nothing_and_refuses(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        changed = copy.deepcopy(items)
        changed[2] = reasoning(99)
        with self.assertRaises(fabric_views.ViewError) as ctx:
            fabric_views.filter(changed, view)
        self.assertEqual(str(ctx.exception), "stale_view")

    def test_filter_drops_only_approved_prose_and_preserves_compaction(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, report = fabric_views.filter(items, view)
        self.assertEqual([r["index"] for r in report["removed"]], [0, 1])
        self.assertEqual(len(out), len(items) - 2)
        self.assertTrue(all(i["type"] == "reasoning" for i in out))

    def test_cancelled_filter_removes_nothing(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, report = fabric_views.filter(items, view, cancelled=True)
        self.assertEqual(out, items)
        self.assertTrue(report["cancelled"])

    def test_reset_restores_the_original_bytes(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, _ = fabric_views.filter(items, view)
        self.assertNotEqual(out, items)
        self.assertEqual(fabric_views.reset(view), None)
        self.assertEqual(items, array_with_prose())  # original intact

    def test_bytes_and_tokens_are_reported_separately(self):
        items = array_with_prose()
        view = fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )
        out, _ = fabric_views.filter(items, view)
        m = fabric_views.metrics(items, out)
        self.assertGreater(m["bytes_removed"], 0)
        self.assertIsNone(m["tokens_measured"])
        self.assertIn("note", m["tokens_estimated"])
        self.assertFalse(m["native_compaction_called"])
        counted = fabric_views.metrics(items, out, token_counter=lambda v: len(v))
        self.assertEqual(
            counted["tokens_measured"], {"before": len(items), "after": len(out)}
        )


class BoundaryViewTests(unittest.TestCase):
    """The boundary applies the approved view and reports its savings."""

    def _view(self, items):
        return fabric_views.apply(
            items, fabric_views.plan(items, target_bytes=10**6), approved=True
        )

    def test_boundary_removes_approved_prose(self):
        request = request_with_prose()
        before = copy.deepcopy(request)
        outgoing, report = bus_boundary.project(
            request,
            registry=make_registry(),
            invoke=Recorder(),
            view=self._view(request["input"]),
        )
        self.assertEqual(len(outgoing["input"]), len(request["input"]) - 2)
        self.assertEqual([r["index"] for r in report["view"]["removed"]], [0, 1])
        self.assertGreater(report["view_metrics"]["bytes_removed"], 0)
        self.assertEqual(request, before)

    def test_boundary_refuses_a_stale_view(self):
        request = request_with_prose()
        view = self._view(request["input"])
        changed = copy.deepcopy(request)
        changed["input"][0]["content"] = [{"type": "output_text", "text": "edited"}]
        outgoing, report = bus_boundary.project(
            changed, registry=make_registry(), invoke=Recorder(), view=view
        )
        self.assertEqual(outgoing, changed)
        self.assertTrue(any(n.get("action") == "refused" for n in report["notes"]))

    def test_boundary_without_a_view_reverts_removal(self):
        def transform(name, messages):
            if name != DEDUP:
                return messages
            return messages[1:]  # drop the current-turn user message

        request = request_with_prose()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)  # restored, nothing approved
        self.assertTrue(
            any(
                n.get("action") == "reverted"
                and n.get("detail") == "unapproved_removal"
                for n in report["notes"]
            )
        )

    def test_unapproved_removal_names_the_stage_that_dropped_it(self):
        """C4 attribution comes from the array, not from which stage is the view stage.

        A removal by stage 100 was previously reported against
        `jev-context-fabric.view`, so the report blamed a stage that had changed
        nothing. The wire array was right either way; the owner was not.
        """

        def transform(name, messages):
            if name != DEDUP:
                return messages
            return messages[1:]

        request = request_with_prose()
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        blamed = [
            n.get("stage")
            for n in report["notes"]
            if n.get("detail") == "unapproved_removal"
        ]
        self.assertEqual(blamed, [DEDUP])

    def test_unapproved_removal_is_reported_while_the_view_stage_is_disabled(self):
        """The finding is about the array, so it does not depend on the view switch.

        With `projection.fabric_views` off, a drop by stage 100 was restored by
        `_rebuild` without any note at all.
        """

        def transform(name, messages):
            return messages[1:]

        request = request_with_prose()
        state = enabled_state()
        state["projection.fabric_views"] = False
        outgoing, report = bus_boundary.project(
            request, registry=make_registry(state), invoke=Recorder(transform)
        )
        self.assertEqual(outgoing, request)
        self.assertTrue(
            any(
                n.get("stage") == DEDUP
                and n.get("action") == "reverted"
                and n.get("detail") == "unapproved_removal"
                for n in report["notes"]
            )
        )

    def test_a_reverted_removal_earns_no_receipt(self):
        """A receipt records a projection that reached the wire.

        `_rebuild` restored the dropped item — only the approved-view filter can
        keep one out — so a stage whose only change was that removal projected
        nothing and must not hold a receipt for it.
        """

        def transform(name, messages):
            if name != DEDUP:
                return messages
            return messages[1:]

        request = request_with_prose()
        _outgoing, report = bus_boundary.project(
            request, registry=make_registry(), invoke=Recorder(transform)
        )
        self.assertNotIn(DEDUP, report["applied"])
        self.assertNotIn(100, [r["stage"] for r in report["receipts"]])

    def test_a_stray_removal_is_reported_even_when_a_view_is_supplied(self):
        """A view approves its own indices, not whatever a stage decided to drop.

        The dropped reasoning item is restored and the removal is reported against
        the stage that made it. The earlier `len(produced) < len(items)` test could
        not see this at all whenever a view was supplied.
        """

        def transform(name, messages):
            if name != DEDUP:
                return messages
            return messages[:-1]  # a reasoning item no prose view may approve

        request = request_with_prose()
        outgoing, report = bus_boundary.project(
            request,
            registry=make_registry(),
            invoke=Recorder(transform),
            view=self._view(request["input"]),
        )
        # Exactly the two approved prose items leave; the reasoning item returns.
        self.assertEqual(len(outgoing["input"]), len(request["input"]) - 2)
        self.assertTrue(
            any(
                n.get("stage") == DEDUP
                and n.get("action") == "reverted"
                and n.get("detail") == "unapproved_removal"
                for n in report["notes"]
            )
        )

    def _view_of(self, items, approved):
        return {
            "kind": fabric_views.KIND,
            "version": fabric_views.STATE_VERSION,
            "fingerprint": fabric_views.snapshot(items),
            "keys": [fabric_views.message_key(items[i], i) for i in approved],
        }

    def test_an_approved_removal_credits_the_stage_that_made_it(self):
        """`kept_out` shares the stage's index space, so credit is positional.

        A stage's removal is the boundary tag that vanished, and the approved-view
        filter numbers the same positions: it is handed `_rebuild`'s output, which
        is positionally the original wire input. So a stage that dropped exactly
        the item the view approves reached the wire with that removal and keeps its
        receipt, while a stage that dropped a different item had its removal
        restored and earns nothing.
        """

        def drop(index):
            def transform(name, messages):
                if name != DEDUP:
                    return messages
                return messages[:index] + messages[index + 1 :]

            return transform

        # Same index: the stage dropped what the view approves -> it reaches the wire.
        request = request_with_prose()
        items = request["input"]
        outgoing, report = bus_boundary.project(
            request,
            registry=make_registry(),
            invoke=Recorder(drop(0)),
            view=self._view_of(items, [0]),
        )
        self.assertEqual([r["index"] for r in report["view"]["removed"]], [0])
        self.assertEqual(outgoing["input"], items[1:])  # the stage's own item left
        self.assertIn(DEDUP, report["applied"])
        self.assertFalse(
            any(n.get("detail") == "unapproved_removal" for n in report["notes"])
        )

        # Different index: the stage's removal was restored, so it earns nothing.
        request = request_with_prose()
        items = request["input"]
        outgoing, report = bus_boundary.project(
            request,
            registry=make_registry(),
            invoke=Recorder(drop(1)),
            view=self._view_of(items, [0]),
        )
        self.assertEqual([r["index"] for r in report["view"]["removed"]], [0])
        self.assertEqual(outgoing["input"], items[1:])  # the approved item left
        self.assertIn(items[1], outgoing["input"])  # the stage's item was restored
        self.assertNotIn(DEDUP, report["applied"])
        self.assertTrue(
            any(
                n.get("stage") == DEDUP
                and n.get("action") == "reverted"
                and n.get("detail") == "unapproved_removal"
                for n in report["notes"]
            )
        )


if __name__ == "__main__":
    unittest.main()
