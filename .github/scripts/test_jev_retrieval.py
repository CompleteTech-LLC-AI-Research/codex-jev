#!/usr/bin/env python3
"""Required-CI coverage for budgeted retrieval and hydration (#14).

These tests drive `jev/scripts/retrieval.py` over the canonical capture store
built by `jev/scripts/canonical_capture.py` from the checked-in synthetic
rollouts. They pin the contract issue #14 asks for: excerpts are source-backed,
spending is explicit and bounded, recalled content is marked untrusted and
never authoritative, a workspace mismatch is refused, hydration re-proves the
origin (missing/changed sources are reported, not papered over), and optional
remote enrichment is refused rather than silently reaching the network.
"""

import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import canonical_capture  # noqa: E402
import isolated_env  # noqa: E402
import retrieval  # noqa: E402

FIXTURES = REPO_ROOT / "jev" / "tests" / "capture_fixtures"
WORKSPACE = "/workspace/demo"


def fixture(name):
    return FIXTURES / name


class RetrievalTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.env_dir = Path(cls.work.name) / "isolated"
        isolated_env.init_env(env_dir=cls.env_dir, root=REPO_ROOT)
        canonical_capture.capture(
            cls.env_dir,
            [
                str(fixture("01-simple-turn.jsonl")),
                str(fixture("02-tool-call.jsonl")),
                str(fixture("03-collab-plaintext.jsonl")),
                str(fixture("04-secret-bearing.jsonl")),
            ],
            root=REPO_ROOT,
        )
        cls.events = canonical_capture.read_jsonl(
            canonical_capture.capture_root(cls.env_dir) / "events.jsonl"
        )

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def event_of_kind(self, kind):
        return next(event for event in self.events if event["kind"] == kind)


class SearchTests(RetrievalTestCase):
    def test_search_returns_source_backed_excerpts(self):
        report = retrieval.search(
            self.env_dir, query="offline plaintext task", root=REPO_ROOT
        )
        self.assertEqual(1, len(report["excerpts"]))
        excerpt = report["excerpts"][0]
        self.assertEqual("collab_message", excerpt["kind"])
        self.assertTrue(excerpt["untrusted"])
        self.assertTrue(excerpt["possibly_stale"])
        provenance = excerpt["provenance"]
        self.assertTrue(provenance["source"])
        self.assertIsInstance(provenance["ordinal"], int)
        self.assertEqual(64, len(provenance["record_sha256"]))
        self.assertEqual(retrieval.EVIDENCE_NOTE, report["note"])

    def test_search_is_budgeted_by_excerpt_count(self):
        report = retrieval.search(self.env_dir, max_excerpts=1, root=REPO_ROOT)
        self.assertEqual(1, len(report["excerpts"]))
        self.assertGreater(report["budget"]["matched"], 1)
        self.assertGreater(report["budget"]["dropped"], 0)
        self.assertTrue(report["budget"]["truncated"])
        self.assertFalse(report["excerpts"][0]["truncated"])

    def test_search_is_budgeted_by_bytes_and_tokens(self):
        report = retrieval.search(
            self.env_dir, max_bytes=8, max_tokens=1024, root=REPO_ROOT
        )
        self.assertLessEqual(report["budget"]["byte_cap"], 8)
        self.assertLessEqual(report["budget"]["bytes_returned"], 8)
        self.assertTrue(any(e["truncated"] for e in report["excerpts"]))
        self.assertTrue(report["budget"]["truncated"])
        by_tokens = retrieval.search(
            self.env_dir, max_bytes=4096, max_tokens=1, root=REPO_ROOT
        )
        self.assertLessEqual(by_tokens["budget"]["byte_cap"], retrieval.BYTES_PER_TOKEN)
        self.assertLessEqual(
            by_tokens["budget"]["bytes_returned"], retrieval.BYTES_PER_TOKEN
        )

    def test_search_filters_by_kind_and_query(self):
        report = retrieval.search(self.env_dir, kind="tool_call", root=REPO_ROOT)
        self.assertTrue(report["excerpts"])
        self.assertEqual({"tool_call"}, {e["kind"] for e in report["excerpts"]})
        hit = retrieval.search(self.env_dir, query="no such text here", root=REPO_ROOT)
        self.assertEqual([], hit["excerpts"])

    def test_search_view_holds_no_detectable_credential(self):
        report = retrieval.search(self.env_dir, kind="user_message", root=REPO_ROOT)
        self.assertTrue(report["excerpts"])
        for excerpt in report["excerpts"]:
            self.assertEqual([], canonical_capture.secret_hits(excerpt["text"]))

    def test_search_refuses_a_workspace_mismatch(self):
        report = retrieval.search(
            self.env_dir, workspace="/other/workspace", root=REPO_ROOT
        )
        self.assertEqual([], report["excerpts"])
        self.assertFalse(report["workspace_matched"])
        self.assertIn(WORKSPACE, report["workspaces_present"])

    def test_search_scopes_to_the_matching_workspace(self):
        report = retrieval.search(self.env_dir, workspace=WORKSPACE, root=REPO_ROOT)
        self.assertTrue(report["workspace_matched"])
        self.assertTrue(report["excerpts"])
        self.assertEqual(
            {WORKSPACE}, {e["origin_workspace"] for e in report["excerpts"]}
        )

    def test_search_fails_closed_without_a_store(self):
        with tempfile.TemporaryDirectory() as work:
            nested = Path(work) / "isolated"
            isolated_env.init_env(env_dir=nested, root=REPO_ROOT)
            with self.assertRaises(retrieval.RetrievalError):
                retrieval.search(nested, root=REPO_ROOT)

    def test_search_rejects_a_degenerate_budget(self):
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.search(self.env_dir, max_bytes=0, root=REPO_ROOT)


class HydrateTests(RetrievalTestCase):
    def test_hydrate_returns_the_exact_canonical_bytes(self):
        event = self.event_of_kind("user_message")
        result = retrieval.hydrate(
            self.env_dir, event_id=event["event_id"], root=REPO_ROOT
        )
        self.assertTrue(result["ok"], result.get("status"))
        content = (
            canonical_capture.capture_root(self.env_dir)
            / "content"
            / f"{event['capture_id']}.txt"
        ).read_text(encoding="utf-8")
        self.assertEqual(content, result["text"])
        self.assertTrue(result["untrusted"])
        self.assertTrue(result["possibly_stale"])
        self.assertTrue(result["provenance"]["record_verified"])
        self.assertEqual(retrieval.EVIDENCE_NOTE, result["provenance"]["rule"])

    def test_hydrate_accepts_a_capture_id(self):
        event = self.event_of_kind("collab_message")
        result = retrieval.hydrate(
            self.env_dir, capture_id=event["capture_id"], root=REPO_ROOT
        )
        self.assertTrue(result["ok"])
        self.assertEqual(event["event_id"], result["event_id"])

    def test_hydrate_respects_the_byte_budget(self):
        event = self.event_of_kind("user_message")
        result = retrieval.hydrate(
            self.env_dir, event_id=event["event_id"], max_bytes=4, root=REPO_ROOT
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["bytes_returned"], 4)

    def test_hydrate_needs_an_identifier(self):
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.hydrate(self.env_dir, root=REPO_ROOT)

    def test_hydrate_refuses_a_workspace_mismatch(self):
        event = self.event_of_kind("user_message")
        result = retrieval.hydrate(
            self.env_dir,
            event_id=event["event_id"],
            workspace="/other/workspace",
            root=REPO_ROOT,
        )
        self.assertFalse(result["ok"])
        self.assertEqual("workspace_mismatch", result["status"])


class DriftTests(unittest.TestCase):
    """Hydration must report drift, not return stale bytes as authoritative."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.env_dir = self.root / "isolated"
        isolated_env.init_env(env_dir=self.env_dir, root=REPO_ROOT)
        self.rollout = self.root / "rollout-local.jsonl"
        shutil.copyfile(fixture("02-tool-call.jsonl"), self.rollout)
        canonical_capture.capture(self.env_dir, [str(self.rollout)], root=REPO_ROOT)
        self.event = canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "events.jsonl"
        )[0]

    def hydrate(self):
        return retrieval.hydrate(
            self.env_dir, event_id=self.event["event_id"], root=REPO_ROOT
        )

    def test_hydrate_reports_a_missing_source(self):
        self.rollout.rename(self.rollout.with_suffix(".gone"))
        result = self.hydrate()
        self.assertFalse(result["ok"])
        self.assertEqual("missing_source", result["status"])

    def test_hydrate_reports_a_changed_source(self):
        text = self.rollout.read_text(encoding="utf-8")
        self.rollout.write_text(
            text.replace("run the tool", "run the tool now"), "utf-8"
        )
        result = self.hydrate()
        self.assertFalse(result["ok"])
        self.assertEqual("source_changed", result["status"])

    def test_hydrate_reports_a_changed_content_file(self):
        path = (
            canonical_capture.capture_root(self.env_dir)
            / "content"
            / f"{self.event['capture_id']}.txt"
        )
        path.write_text("tampered", encoding="utf-8")
        result = self.hydrate()
        self.assertFalse(result["ok"])
        self.assertEqual("content_changed", result["status"])


class RemoteEnrichmentTests(RetrievalTestCase):
    def test_search_refuses_remote_enrichment(self):
        with self.assertRaises(retrieval.RetrievalError) as caught:
            retrieval.search(self.env_dir, root=REPO_ROOT, remote_enrichment=True)
        self.assertIn("E_REMOTE_ENRICHMENT_UNAUTHORIZED", str(caught.exception))

    def test_hydrate_refuses_remote_enrichment(self):
        event = self.event_of_kind("user_message")
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.hydrate(
                self.env_dir,
                event_id=event["event_id"],
                root=REPO_ROOT,
                remote_enrichment=True,
            )

    def test_no_implicit_network_call(self):
        # If retrieval reached the network, this would raise.
        def blocked(*args, **kwargs):
            raise AssertionError("retrieval attempted a network connection")

        original = socket.socket
        socket.socket = blocked  # type: ignore[assignment]
        try:
            report = retrieval.search(self.env_dir, root=REPO_ROOT)
            self.assertTrue(report["excerpts"])
            event = self.event_of_kind("user_message")
            result = retrieval.hydrate(
                self.env_dir, event_id=event["event_id"], root=REPO_ROOT
            )
            self.assertTrue(result["ok"])
        finally:
            socket.socket = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
