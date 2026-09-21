#!/usr/bin/env python3
"""Required-CI coverage for canonical capture and event correlation.

These tests drive `jev/scripts/canonical_capture.py` over the checked-in
synthetic rollouts in `jev/tests/capture_fixtures` (tier `rollout-fixture`).
They pin the contract issue #13 asks for: capture happens before projection,
evidence traces back to the exact source record, repeats do not multiply
records, gaps are reported instead of skipped, and credentials are absent from
the retrieval view.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import canonical_capture  # noqa: E402
import isolated_env  # noqa: E402

FIXTURES = REPO_ROOT / "jev" / "tests" / "capture_fixtures"


def fixture(name):
    return FIXTURES / name


class CaptureTestCase(unittest.TestCase):
    """One isolated environment shared by every test; each capture is fresh."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory()
        cls.env_dir = Path(cls.work.name) / "isolated"
        isolated_env.init_env(env_dir=cls.env_dir, root=REPO_ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def capture(self, *names):
        return canonical_capture.capture(
            self.env_dir, [str(fixture(name)) for name in names], root=REPO_ROOT
        )

    def events(self):
        return canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "events.jsonl"
        )

    def retrieval(self):
        return canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "retrieval.jsonl"
        )

    def duplicates(self):
        return canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "duplicates.jsonl"
        )

    def gaps(self):
        return canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "gaps.jsonl"
        )

    def skipped(self):
        return canonical_capture.read_jsonl(
            canonical_capture.capture_root(self.env_dir) / "skipped.jsonl"
        )

    def check(self, name):
        return canonical_capture.verify(self.env_dir, root=REPO_ROOT)["checks"][name]


class SimpleTurnTests(CaptureTestCase):
    def test_supported_user_and_assistant_events_are_captured_once_each(self):
        summary = self.capture("01-simple-turn.jsonl")
        self.assertEqual(summary["events"], 2)
        self.assertEqual(
            summary["kind_counts"], {"assistant_message": 1, "user_message": 1}
        )

    def test_a_second_native_surface_becomes_a_receipt_not_a_record(self):
        self.capture("01-simple-turn.jsonl")
        receipts = self.duplicates()
        self.assertEqual(len(receipts), 2)
        for receipt in receipts:
            self.assertTrue(receipt["duplicate_of"])
            self.assertEqual(len(receipt["surfaces"]), 2)
        self.assertEqual(len({event["event_id"] for event in self.events()}), 2)
        self.assertTrue(self.check("repeats_do_not_multiply_records"))

    def test_session_metadata_supplies_the_correlation_identifiers(self):
        self.capture("01-simple-turn.jsonl")
        for event in self.events():
            self.assertEqual(event["session_id"], "01a0-fixture-session")
            self.assertEqual(event["turn_id"], "turn-fixture-1")
            self.assertEqual(event["origin_workspace"], "/workspace/demo")
            self.assertEqual(event["component"], "codex-jev")
            self.assertEqual(event["stage"], 0)

    def test_events_form_a_turn_chain(self):
        self.capture("01-simple-turn.jsonl")
        events = self.events()
        first = next(e for e in events if e["kind"] == "user_message")
        second = next(e for e in events if e["kind"] == "assistant_message")
        self.assertIsNone(first["parent_event_id"])
        self.assertEqual(second["parent_event_id"], first["event_id"])

    def test_canonical_content_is_content_addressed_and_hash_matches(self):
        self.capture("01-simple-turn.jsonl")
        for event in self.events():
            if event["kind"] != "user_message":
                continue
            path = (
                canonical_capture.capture_root(self.env_dir)
                / "content"
                / f"{event['capture_id']}.txt"
            )
            self.assertTrue(path.is_file())
            self.assertEqual(
                canonical_capture.sha256_bytes(path.read_bytes()),
                event["content_sha256"],
            )
            self.assertEqual(event["capture_id"], "cap_" + event["content_sha256"][:32])


class CorrelationTests(CaptureTestCase):
    def test_tool_call_and_result_are_paired_by_call_id(self):
        self.capture("02-tool-call.jsonl")
        call = next(e for e in self.events() if e["kind"] == "tool_call")
        result = next(e for e in self.events() if e["kind"] == "tool_result")
        self.assertEqual(call["tool_call_id"], "call-fixture-shell")
        self.assertEqual(result["tool_call_id"], call["tool_call_id"])
        self.assertTrue(self.check("tool_calls_and_results_are_paired"))

    def test_collaboration_call_is_classified_as_a_collab_message(self):
        self.capture("03-collab-plaintext.jsonl")
        collab = [e for e in self.events() if e["kind"] == "collab_message"]
        self.assertEqual(len(collab), 1)
        self.assertEqual(collab[0]["tool_call_id"], "call-fixture-collab")
        self.assertIn("offline plaintext task", self.collab_text(collab[0]))

    def collab_text(self, event):
        path = (
            canonical_capture.capture_root(self.env_dir)
            / "content"
            / f"{event['capture_id']}.txt"
        )
        return path.read_text(encoding="utf-8")

    def test_trace_reports_the_origin_record_and_its_chain(self):
        self.capture("02-tool-call.jsonl")
        call = next(e for e in self.events() if e["kind"] == "tool_call")
        result = next(e for e in self.events() if e["kind"] == "tool_result")
        traced = canonical_capture.trace(
            self.env_dir, tool_call_id="call-fixture-shell"
        )
        self.assertEqual(traced["event"]["event_id"], call["event_id"])
        self.assertTrue(traced["origin"]["record_verified"])
        kinds = [step["kind"] for step in traced["turn_chain"]]
        self.assertEqual(kinds, ["user_message", "tool_call"])
        self.assertIn(
            result["event_id"], [other["event_id"] for other in traced["related"]]
        )


class RedactionTests(CaptureTestCase):
    def test_canonical_content_keeps_the_value_the_view_replaces(self):
        self.capture("04-secret-bearing.jsonl")
        canonical = [
            (
                canonical_capture.capture_root(self.env_dir)
                / "content"
                / f"{event['capture_id']}.txt"
            ).read_text(encoding="utf-8")
            for event in self.events()
        ]
        self.assertTrue(any(canonical_capture.secret_hits(text) for text in canonical))
        for record in self.retrieval():
            self.assertEqual([], canonical_capture.secret_hits(record["text"]))

    def test_the_retrieval_view_names_the_rules_that_matched(self):
        self.capture("04-secret-bearing.jsonl")
        rules = {
            entry["rule"]
            for record in self.retrieval()
            for entry in record["redaction"]["rules"]
        }
        self.assertIn("openai_style_key", rules)
        self.assertIn("github_token", rules)
        self.assertIn("jwt", rules)
        self.assertIn("assigned_credential", rules)

    def test_the_view_is_marked_untrusted_and_possibly_stale(self):
        self.capture("04-secret-bearing.jsonl")
        for record in self.retrieval():
            self.assertTrue(record["untrusted"])
            self.assertTrue(record["possibly_stale"])
            self.assertEqual(record["redaction"]["view"], "retrieval")
        self.assertTrue(self.check("retrieval_view_is_marked_untrusted_and_stale"))

    def test_absence_of_a_secret_is_provable_not_assumed(self):
        # The scanner must be able to fail, or "no secrets" means nothing.
        text = "api key sk-FIXTURE-NOT-A-REAL-KEY-0000"
        self.assertEqual(["openai_style_key"], canonical_capture.secret_hits(text))
        view, applied = canonical_capture.redact(text)
        self.assertEqual([], canonical_capture.secret_hits(view))
        self.assertEqual(1, len(applied))


class DedupAndGapTests(CaptureTestCase):
    def test_a_retried_item_does_not_multiply_records(self):
        summary = self.capture("05-retry-duplicate.jsonl")
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["duplicates"], 1)
        self.assertTrue(self.check("repeats_do_not_multiply_records"))

    def test_a_missing_ordinal_is_a_reported_gap(self):
        self.capture("06-gap.jsonl")
        kinds = [gap["kind"] for gap in self.gaps()]
        self.assertIn("missing_ordinal", kinds)
        self.assertTrue(self.gaps()[0]["missing"] >= 1)

    def test_a_truncated_transcript_is_a_reported_gap(self):
        self.capture("07-truncated.jsonl")
        self.assertIn("unparsable_record", [gap["kind"] for gap in self.gaps()])

    def test_an_orphan_tool_result_is_a_reported_gap(self):
        self.capture("08-orphan-tool-result.jsonl")
        self.assertIn("orphan_tool_result", [gap["kind"] for gap in self.gaps()])

    def test_verify_reports_a_gap_instead_of_hiding_it(self):
        self.capture("06-gap.jsonl")
        report = canonical_capture.verify(self.env_dir, root=REPO_ROOT)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["no_capture_gaps"])
        self.assertTrue(report["checks"]["every_event_traces_to_its_origin_record"])
        self.assertTrue(report["gaps"])

    def test_a_clean_capture_verifies_completely(self):
        self.capture("02-tool-call.jsonl", "03-collab-plaintext.jsonl")
        report = canonical_capture.verify(self.env_dir, root=REPO_ROOT)
        self.assertTrue(report["ok"], report["checks"])

    def test_capture_is_repeatable_for_the_same_input(self):
        first = self.capture("01-simple-turn.jsonl")
        ids = [event["event_id"] for event in self.events()]
        second = self.capture("01-simple-turn.jsonl")
        self.assertEqual(first["events"], second["events"])
        self.assertEqual(ids, [event["event_id"] for event in self.events()])


class BoundaryTests(CaptureTestCase):
    def test_injected_user_role_context_is_not_operator_speech(self):
        summary = self.capture("09-injected-context.jsonl")
        self.assertEqual(summary["events"], 0)
        reasons = " ".join(skip["reason"] for skip in self.skipped())
        self.assertIn("injected user-role context", reasons)

    def test_every_captured_kind_is_declared_by_the_manifest(self):
        self.capture(
            "01-simple-turn.jsonl",
            "02-tool-call.jsonl",
            "03-collab-plaintext.jsonl",
        )
        declared = canonical_capture.envelope_kinds(REPO_ROOT)
        for event in self.events():
            self.assertIn(event["kind"], declared)
        self.assertTrue(self.check("envelope_kinds_are_declared_by_the_manifest"))

    def test_the_envelope_carries_every_declared_field(self):
        self.capture("02-tool-call.jsonl")
        for event in self.events():
            self.assertTrue(set(canonical_capture.ENVELOPE_FIELDS) <= set(event))
        self.assertTrue(self.check("envelope_carries_every_declared_field"))

    def test_fixture_rollouts_are_labelled_as_fixtures(self):
        summary = self.capture("01-simple-turn.jsonl")
        self.assertEqual(summary["tiers"], ["rollout-fixture"])
        for event in self.events():
            self.assertEqual(event["tier"], "rollout-fixture")
        self.assertEqual(
            "real-host-rollout",
            canonical_capture.detect_tier(
                Path(self.work.name) / "rollout-real.jsonl", REPO_ROOT
            ),
        )

    def test_capture_needs_an_isolated_environment(self):
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(isolated_env.EnvError):
                canonical_capture.capture(
                    Path(work) / "absent",
                    [str(fixture("01-simple-turn.jsonl"))],
                    root=REPO_ROOT,
                )

    def test_capture_fails_closed_on_a_missing_rollout(self):
        with self.assertRaises(canonical_capture.CaptureError):
            canonical_capture.capture(
                self.env_dir, [str(FIXTURES / "does-not-exist.jsonl")], root=REPO_ROOT
            )

    def test_status_before_a_capture_fails_closed(self):
        with tempfile.TemporaryDirectory() as work:
            nested = Path(work) / "isolated"
            isolated_env.init_env(env_dir=nested, root=REPO_ROOT)
            with self.assertRaises(canonical_capture.CaptureError):
                canonical_capture.status(nested)

    def test_status_reports_the_store_and_the_ambient_home(self):
        self.capture("01-simple-turn.jsonl")
        report = canonical_capture.status(self.env_dir)
        self.assertEqual(report["events"], report["events_present"])
        self.assertEqual(report["events"], report["content_files_present"])
        self.assertEqual(report["duplicates_recorded"], 2)
        self.assertIn("config_present", report["ambient_home"])

    def test_the_store_lives_inside_the_isolated_environment(self):
        self.capture("01-simple-turn.jsonl")
        root = canonical_capture.capture_root(self.env_dir).resolve()
        self.assertTrue(str(root).startswith(str(self.env_dir.resolve())))
        self.assertTrue((root / "index.json").is_file())
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["store_version"], canonical_capture.STORE_VERSION)
        self.assertEqual(index["env_dir"], str(self.env_dir))


if __name__ == "__main__":
    unittest.main()
