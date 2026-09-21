#!/usr/bin/env python3
"""Required-CI coverage for canonical capture and event correlation (#13).

These tests pin the contract the host owns: the envelope schema is read from
the manifest and fails closed on drift, one lifecycle-hook payload maps to one
canonical envelope without guessing correlation identity, a repeated delivery
collapses onto the first record, parent links are proven rather than inferred,
capture gaps are reported, and a secret in the canonical bytes never reaches
the retrieval view. They are tier `offline-fixture`: everything here is
synthetic payload text, no host and no provider is involved.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import capture_hook  # noqa: E402
import event_envelope  # noqa: E402

CAPTURE_HOOK = REPO_ROOT / "jev" / "scripts" / "capture_hook.py"
ENVELOPE_CLI = REPO_ROOT / "jev" / "scripts" / "event_envelope.py"
MANIFEST = REPO_ROOT / "jev" / "compatibility-manifest.json"

UNCAPTURED_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "Interrupt",
    "SubagentStart",
)


def hook_payload(event, **overrides):
    base = {
        "session_id": "sess-capture",
        "turn_id": "turn-capture",
        "cwd": "/tmp/jev-workspace",
        "hook_event_name": event,
    }
    base.update(overrides)
    return base


PROMPT = hook_payload("UserPromptSubmit", prompt="summarise the ledger")
CALL = hook_payload(
    "PreToolUse",
    tool_name="shell",
    tool_input={"command": "true"},
    tool_use_id="call-1",
)
RESULT = hook_payload(
    "PostToolUse",
    tool_name="shell",
    tool_input={"command": "true"},
    tool_response={"exit_code": 0},
    tool_use_id="call-1",
)
REPLY = hook_payload("Stop", last_assistant_message="ledger summarised")


def stored_envelopes(records):
    """The envelopes inside capture-log records, in stored order."""
    return [
        record["envelope"]
        for record in records
        if isinstance(record.get("envelope"), dict)
    ]


class ContractTests(unittest.TestCase):
    def test_contract_matches_the_manifest(self):
        contract = event_envelope.load_event_contract(REPO_ROOT)
        self.assertEqual(contract["envelope_version"], 1)
        self.assertEqual(
            tuple(contract["envelope_fields"]), event_envelope.EXPECTED_FIELDS
        )
        self.assertEqual(tuple(contract["kinds"]), event_envelope.EXPECTED_KINDS)
        self.assertEqual(contract["stages"], event_envelope.EXPECTED_STAGES)

    def test_kind_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "jev").mkdir()
            document = json.loads(MANIFEST.read_text(encoding="utf-8"))
            document["events"]["kinds"].append("invented_kind")
            (root / "jev" / "compatibility-manifest.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaises(event_envelope.EnvelopeError):
                event_envelope.load_event_contract(root)

    def test_missing_events_contract_fails_closed(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "jev").mkdir()
            (root / "jev" / "compatibility-manifest.json").write_text(
                json.dumps({"components": []}), encoding="utf-8"
            )
            with self.assertRaises(event_envelope.EnvelopeError):
                event_envelope.load_event_contract(root)


class EnvelopeMappingTests(unittest.TestCase):
    def test_every_captured_hook_maps_to_its_kind(self):
        for event, kind in event_envelope.HOOK_EVENT_KIND.items():
            with self.subTest(event=event):
                self.assertIn(kind, event_envelope.EXPECTED_KINDS)

    def test_user_prompt_becomes_a_user_message(self):
        envelope = event_envelope.envelope_from_payload(PROMPT)
        self.assertEqual(envelope["kind"], "user_message")
        self.assertEqual(envelope["component"], "codex-jev")
        self.assertEqual(envelope["session_id"], "sess-capture")
        self.assertEqual(envelope["turn_id"], "turn-capture")
        self.assertIsNone(envelope["tool_call_id"])
        self.assertEqual(envelope["origin_workspace"], "/tmp/jev-workspace")
        self.assertEqual(event_envelope.event_content(PROMPT), "summarise the ledger")
        self.assertEqual(
            envelope["capture_id"],
            event_envelope.digest("summarise the ledger"),
        )
        self.assertEqual(envelope["redaction"]["canonical"], False)
        self.assertTrue(envelope["redaction"]["retrieval_view"])
        self.assertEqual(
            envelope["redaction"]["canonical_sha256"], envelope["capture_id"]
        )
        self.assertEqual(event_envelope.validate_envelope(envelope), [])

    def test_tool_events_keep_their_tool_call_id(self):
        call = event_envelope.envelope_from_payload(CALL)
        result = event_envelope.envelope_from_payload(RESULT)
        self.assertEqual(call["kind"], "tool_call")
        self.assertEqual(result["kind"], "tool_result")
        self.assertEqual(call["tool_call_id"], "call-1")
        self.assertEqual(result["tool_call_id"], "call-1")
        self.assertIn('"tool":"shell"', event_envelope.event_content(CALL))
        self.assertIn('"result":{"exit_code":0}', event_envelope.event_content(RESULT))
        self.assertEqual(event_envelope.validate_envelope(call), [])
        self.assertEqual(event_envelope.validate_envelope(result), [])

    def test_stop_and_subagent_stop_become_messages(self):
        reply = event_envelope.envelope_from_payload(REPLY)
        child_payload = hook_payload(
            "SubagentStop",
            agent_id="agent-7",
            agent_type="explorer",
            last_assistant_message="child done",
        )
        child = event_envelope.envelope_from_payload(child_payload)
        self.assertEqual(reply["kind"], "assistant_message")
        self.assertEqual(child["kind"], "collab_message")
        self.assertIn(
            '"agent_id":"agent-7"', event_envelope.event_content(child_payload)
        )

    def test_uncaptured_hooks_are_skipped_not_invented(self):
        for event in UNCAPTURED_EVENTS:
            with self.subTest(event=event):
                self.assertIsNone(
                    event_envelope.envelope_from_payload(hook_payload(event))
                )

    def test_missing_correlation_identity_is_never_guessed(self):
        cases = {
            "no session": hook_payload("UserPromptSubmit", prompt="x", session_id=None),
            "empty session": hook_payload(
                "UserPromptSubmit", prompt="x", session_id=""
            ),
            "no turn": hook_payload("UserPromptSubmit", prompt="x", turn_id=None),
            "tool without tool_use_id": hook_payload("PreToolUse", tool_name="shell"),
            "tool with empty tool_use_id": hook_payload(
                "PreToolUse", tool_name="shell", tool_use_id=""
            ),
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(event_envelope.envelope_from_payload(payload))

    def test_empty_content_is_not_captured(self):
        self.assertIsNone(
            event_envelope.envelope_from_payload(
                hook_payload("UserPromptSubmit", prompt="")
            )
        )
        self.assertIsNone(
            event_envelope.envelope_from_payload(
                hook_payload("Stop", last_assistant_message=None)
            )
        )

    def test_event_identity_is_deterministic(self):
        first = event_envelope.envelope_from_payload(PROMPT, occurred_at_ms=1)
        second = event_envelope.envelope_from_payload(PROMPT, occurred_at_ms=99)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["occurred_at_ms"], second["occurred_at_ms"])
        other = event_envelope.envelope_from_payload(
            hook_payload("UserPromptSubmit", prompt="a different prompt")
        )
        self.assertNotEqual(first["event_id"], other["event_id"])

    def test_validate_envelope_reports_broken_identity(self):
        envelope = event_envelope.envelope_from_payload(RESULT)
        envelope["tool_call_id"] = None
        findings = event_envelope.validate_envelope(envelope)
        self.assertTrue(any("needs a tool_call_id" in item for item in findings))
        envelope["kind"] = "invented_kind"
        findings = event_envelope.validate_envelope(envelope)
        self.assertTrue(any(item.startswith("E_ENVELOPE_KIND") for item in findings))


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.log_path = Path(self.work.name) / "events.jsonl"
        self.log = event_envelope.CaptureLog(self.log_path)

    def test_repeat_delivery_collapses_onto_the_first_record(self):
        content = event_envelope.event_content(PROMPT)
        first = self.log.append(
            event_envelope.envelope_from_payload(PROMPT), content=content
        )
        second = self.log.append(
            event_envelope.envelope_from_payload(PROMPT), content=content
        )
        self.assertTrue(first["stored"])
        self.assertFalse(second["stored"])
        self.assertEqual(second["reason"], "duplicate")
        records = self.log.read()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"], "summarise the ledger")
        self.assertEqual(
            records[0]["content_sha256"], records[0]["envelope"]["capture_id"]
        )

    def test_raw_delivery_is_hashed_and_kept(self):
        raw = json.dumps(PROMPT)
        record = self.log.append(event_envelope.envelope_from_payload(PROMPT), raw=raw)[
            "record"
        ]
        self.assertEqual(record["origin"]["source"], "host-hook")
        self.assertEqual(record["origin"]["raw_sha256"], event_envelope.digest(raw))
        self.assertEqual(record["origin"]["raw_bytes"], len(raw))

    def test_skipped_observation_is_recorded(self):
        record = self.log.append_skipped(
            "PreCompact", "unsupported_or_incomplete_event"
        )
        self.assertEqual(record["skipped"]["hook_event_name"], "PreCompact")
        findings = event_envelope.validate_stream({"records": self.log.read()})
        self.assertTrue(
            any(item.startswith("W_CAPTURE_UNSUPPORTED_EVENT") for item in findings)
        )


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.log = event_envelope.CaptureLog(Path(self.work.name) / "events.jsonl")

    def append(self, *payloads):
        for payload in payloads:
            self.log.append(event_envelope.envelope_from_payload(payload))
        return event_envelope.CaptureLog(self.log.path).read()

    def test_one_turn_resolves_both_parent_links(self):
        records = self.append(PROMPT, CALL, RESULT, REPLY)
        report = event_envelope.correlate(stored_envelopes(records))
        self.assertEqual(report["gaps"], [])
        self.assertEqual(len(report["parents"]), 2)
        prompt_id = event_envelope.envelope_from_payload(PROMPT)["event_id"]
        call_id = event_envelope.envelope_from_payload(CALL)["event_id"]
        self.assertEqual(
            report["parents"][event_envelope.envelope_from_payload(RESULT)["event_id"]],
            call_id,
        )
        self.assertEqual(
            report["parents"][event_envelope.envelope_from_payload(REPLY)["event_id"]],
            prompt_id,
        )
        self.assertEqual(event_envelope.validate_stream({"records": records}), [])

    def test_parent_is_resolved_on_append(self):
        self.append(PROMPT, CALL, RESULT, REPLY)
        stored = {
            record["envelope"]["kind"]: record["envelope"]
            for record in event_envelope.CaptureLog(self.log.path).read()
        }
        self.assertEqual(
            stored["tool_result"]["parent_event_id"], stored["tool_call"]["event_id"]
        )
        self.assertEqual(
            stored["assistant_message"]["parent_event_id"],
            stored["user_message"]["event_id"],
        )

    def test_result_without_its_call_is_a_reported_gap(self):
        records = self.append(RESULT)
        report = event_envelope.correlate(stored_envelopes(records))
        self.assertEqual(
            [gap["code"] for gap in report["gaps"]],
            ["W_CAPTURE_MISSING_TOOL_CALL"],
        )
        findings = event_envelope.validate_stream({"records": records})
        self.assertTrue(
            any(item.startswith("W_CAPTURE_MISSING_TOOL_CALL") for item in findings)
        )

    def test_message_without_its_prompt_is_a_reported_gap(self):
        records = self.append(REPLY)
        report = event_envelope.correlate(stored_envelopes(records))
        self.assertEqual(
            [gap["code"] for gap in report["gaps"]], ["W_CAPTURE_MISSING_PROMPT"]
        )

    def test_component_kinds_are_validated_not_correlated(self):
        envelope = event_envelope.envelope_from_payload(PROMPT)
        envelope["kind"] = "approval_judgment"
        envelope["component"] = "jev-codex-approval"
        self.assertEqual(event_envelope.validate_envelope(envelope), [])
        report = event_envelope.correlate([envelope])
        self.assertEqual(
            [item["kind"] for item in report["orphans"]], ["approval_judgment"]
        )
        self.assertEqual(report["gaps"], [])


class RetrievalViewTests(unittest.TestCase):
    def test_secret_in_both_sides_is_reported(self):
        secret = "sk-" + "a" * 24
        findings = event_envelope.retrieval_view_findings(
            f"canonical {secret}", f"view {secret}", secrets=[secret]
        )
        self.assertTrue(
            any(item.startswith("E_CAPTURE_SECRET_IN_VIEW") for item in findings)
        )

    def test_redacted_view_has_no_findings(self):
        secret = "sk-" + "b" * 24
        findings = event_envelope.retrieval_view_findings(
            f"canonical {secret}", "view [redacted]", secrets=[secret]
        )
        self.assertEqual(findings, [])

    def test_credential_shapes_are_caught_without_a_planted_secret(self):
        findings = event_envelope.retrieval_view_findings(
            "canonical", "key AKIAIOSFODNN7EXAMPLE in the view"
        )
        self.assertTrue(
            any(item.startswith("E_CAPTURE_SECRET_IN_VIEW") for item in findings)
        )


class EnvelopeCliTests(unittest.TestCase):
    """The written capture log is a JSONL stream, and the CLI must read it."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.log = Path(self.work.name) / "events.jsonl"

    def write_log(self, *payloads):
        store = event_envelope.CaptureLog(self.log)
        for payload in payloads:
            store.append(event_envelope.envelope_from_payload(payload))
        return store.read()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ENVELOPE_CLI), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validate_reads_a_jsonl_capture_log(self):
        self.write_log(PROMPT, CALL, RESULT, REPLY)
        completed = self.run_cli("validate", str(self.log))
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("0 error(s)", completed.stdout)

    def test_validate_exits_one_on_an_error(self):
        records = self.write_log(PROMPT)
        records[0]["envelope"]["kind"] = "invented_kind"
        self.log.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        completed = self.run_cli("validate", str(self.log))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("E_ENVELOPE_KIND", completed.stdout)

    def test_correlate_reads_a_jsonl_capture_log(self):
        self.write_log(PROMPT, CALL, RESULT, REPLY)
        completed = self.run_cli("correlate", str(self.log))
        self.assertEqual(completed.returncode, 0, completed.stdout)
        report = json.loads(completed.stdout)
        self.assertEqual(report["envelopes"], 4)
        self.assertEqual(report["parents"], 2)
        self.assertEqual(report["gaps"], [])

    def test_correlate_exits_one_on_a_gap(self):
        self.write_log(RESULT)
        completed = self.run_cli("correlate", str(self.log))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            json.loads(completed.stdout)["gaps"][0]["code"],
            "W_CAPTURE_MISSING_TOOL_CALL",
        )

    def test_a_single_json_document_is_still_accepted(self):
        records = self.write_log(PROMPT, CALL, RESULT, REPLY)
        wrapped = Path(self.work.name) / "stream.json"
        wrapped.write_text(json.dumps({"records": records}), encoding="utf-8")
        self.assertEqual(self.run_cli("validate", str(wrapped)).returncode, 0)

    def test_mappings_prints_the_hook_and_component_tables(self):
        completed = self.run_cli("mappings")
        self.assertEqual(completed.returncode, 0)
        mappings = json.loads(completed.stdout)
        self.assertEqual(mappings["hook_events"], event_envelope.HOOK_EVENT_KIND)
        self.assertEqual(mappings["component"], event_envelope.COMPONENT_KIND)

    def test_unreadable_streams_fail_closed(self):
        empty = Path(self.work.name) / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(self.run_cli("validate", str(empty)).returncode, 1)
        garbage = Path(self.work.name) / "garbage.jsonl"
        garbage.write_text("not json\n", encoding="utf-8")
        self.assertEqual(self.run_cli("correlate", str(garbage)).returncode, 1)
        missing = Path(self.work.name) / "absent.jsonl"
        self.assertEqual(self.run_cli("validate", str(missing)).returncode, 1)


class CaptureAdapterTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.capture_dir = self.root / "capture"

    def run_hook(self, payload, event=None, capture_dir=None, **env_overrides):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("JEV_CAPTURE_DIR", "CODEX_HOME")
        }
        if capture_dir is not None:
            environment["JEV_CAPTURE_DIR"] = str(capture_dir)
        environment.update(env_overrides)
        argv = [sys.executable, str(CAPTURE_HOOK)]
        if event:
            argv += ["--event", event]
        return subprocess.run(
            argv,
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def records(self, capture_dir=None):
        path = (capture_dir or self.capture_dir) / "events.jsonl"
        return event_envelope.CaptureLog(path).read() if path.is_file() else []

    def test_adapter_records_the_event_and_stays_silent(self):
        completed = self.run_hook(
            PROMPT, event="UserPromptSubmit", capture_dir=self.capture_dir
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["envelope"]["kind"], "user_message")
        self.assertEqual(records[0]["content"], "summarise the ledger")
        self.assertEqual(records[0]["origin"]["hook_event_name"], "user_message")

    def test_replay_does_not_multiply_records(self):
        for _ in range(3):
            self.run_hook(
                PROMPT, event="UserPromptSubmit", capture_dir=self.capture_dir
            )
        self.assertEqual(len(self.records()), 1)

    def test_unsupported_event_is_recorded_as_skipped(self):
        completed = self.run_hook(
            hook_payload("SessionStart", source="startup"),
            event="SessionStart",
            capture_dir=self.capture_dir,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertNotIn("envelope", records[0])
        self.assertEqual(records[0]["skipped"]["hook_event_name"], "SessionStart")

    def test_unparseable_payload_is_recorded_as_skipped(self):
        completed = self.run_hook(
            "{not json", event="Stop", capture_dir=self.capture_dir
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.records()[0]["skipped"]["reason"], "unparseable_payload")

    def test_mismatched_event_name_is_recorded_as_skipped(self):
        completed = self.run_hook(CALL, event="Stop", capture_dir=self.capture_dir)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.records()[0]["skipped"]["reason"], "event_mismatch")

    def test_capture_dir_defaults_to_codex_home(self):
        home = self.root / "codex-home"
        completed = self.run_hook(
            PROMPT, event="UserPromptSubmit", CODEX_HOME=str(home)
        )
        self.assertEqual(completed.returncode, 0)
        records = self.records(home / "capture")
        self.assertEqual(len(records), 1)
        self.assertFalse((self.root / ".jev").exists())

    def test_explicit_capture_dir_wins_over_codex_home(self):
        explicit = self.root / "explicit"
        home = self.root / "codex-home"
        self.run_hook(
            PROMPT,
            event="UserPromptSubmit",
            capture_dir=explicit,
            CODEX_HOME=str(home),
        )
        self.assertEqual(len(self.records(explicit)), 1)
        self.assertFalse((home / "capture" / "events.jsonl").exists())

    def test_capture_dir_precedence_helper(self):
        with mock.patch.dict(
            os.environ, {"JEV_CAPTURE_DIR": "/tmp/explicit"}, clear=False
        ):
            self.assertEqual(capture_hook.capture_dir(), Path("/tmp/explicit"))
            self.assertEqual(capture_hook.capture_dir("/tmp/flag"), Path("/tmp/flag"))
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": "/tmp/codex-home"}, clear=False
        ):
            os.environ.pop("JEV_CAPTURE_DIR", None)
            self.assertEqual(
                capture_hook.capture_dir(), Path("/tmp/codex-home/capture")
            )

    def test_one_turn_captured_by_the_adapter_correlates(self):
        for item in (PROMPT, CALL, RESULT, REPLY):
            self.run_hook(
                item, event=item["hook_event_name"], capture_dir=self.capture_dir
            )
        records = self.records()
        self.assertEqual(
            [record["envelope"]["kind"] for record in records],
            ["user_message", "tool_call", "tool_result", "assistant_message"],
        )
        self.assertEqual(event_envelope.validate_stream({"records": records}), [])


if __name__ == "__main__":
    unittest.main()
