#!/usr/bin/env python3
"""Required-CI coverage for the redacted diagnostics report (#105).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the diagnostics
report's own contract is pinned here as well as in its module docstring. Phase
6.3 asks the package to ship diagnostics; what makes a diagnostics report
trustworthy is not the field list but two properties, and both are asserted
here:

1. it is **allow-listed**, so it cannot become an exfiltration path for the
   content it describes; and
2. its `redaction` section is a measurement rather than a promise, so a lane
   does not have to take the author's word that the document is clean.

Everything here is offline: checked-in files, local modules, and a synthetic
environment variable. No provider is contacted and no paid inference is used.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import jev_diagnostics  # noqa: E402
import release_readiness  # noqa: E402

#: A credential shape the declared vocabulary names (`scm_token`), used as a
#: synthetic input. It is not a real token and never leaves this process.
SYNTHETIC_TOKEN = "ghp_" + "A" * 24

#: The labelled redaction fixture. Its subject is a credential shape, so if any
#: of its bytes reached the report the redaction scan would flag them - which is
#: exactly the property `test_the_report_reads_no_file_content` relies on.
SECRET_FIXTURE = (
    REPO_ROOT / "jev" / "tests" / "capture_fixtures" / "04-secret-bearing.jsonl"
)


def _shape(*parts):
    """Join fragments so this file never contains a credential shape itself.

    The candidate gate scans `.github/scripts/test_jev_*.py` with the same rules
    the diagnostics report redacts on, so a literal sample written out here
    would refuse the whole release artifact. Every sample is therefore
    assembled at run time: the file is clean, the sample is not.
    """
    return "".join(parts)


class DiagnosticsReportTests(unittest.TestCase):
    """One build for the class; each test asserts a different property of it."""

    @classmethod
    def setUpClass(cls):
        cls.document = jev_diagnostics.build(REPO_ROOT)

    def test_the_report_is_one_parseable_document(self):
        text = json.dumps(self.document, sort_keys=True)
        reparsed = json.loads(text)
        self.assertEqual(reparsed["schema"], jev_diagnostics.SCHEMA)
        for block in (
            "host",
            "manifest",
            "profiles",
            "patches",
            "native_adapter",
            "features",
            "remote_inference",
            "components",
            "binary",
            "ambient_home",
            "isolated",
            "fixtures",
            "redaction",
            "checks",
        ):
            with self.subTest(block=block):
                self.assertIn(block, reparsed)

    def test_the_report_describes_this_checkout(self):
        self.assertEqual(
            self.document["revision"], jev_diagnostics.revision_of(REPO_ROOT)
        )
        self.assertTrue(
            self.document["manifest"]["ok"], self.document["manifest"]["errors"]
        )
        # Every declared profile validates, and the switch set is the effective
        # one rather than the raw overrides.
        self.assertGreaterEqual(len(self.document["profiles"]), 4)
        for profile in self.document["profiles"]:
            with self.subTest(profile=profile["id"]):
                self.assertTrue(profile["ok"], msg=profile["errors"])
                self.assertIn("features", profile)
        # The checkout under test carries the pinned patches and adapter.
        self.assertEqual(self.document["patches"]["state"], "applied")
        self.assertEqual(self.document["native_adapter"]["state"], "applied")
        for patch in self.document["patches"]["patches"]:
            with self.subTest(patch=patch["id"]):
                self.assertTrue(patch["matches_declared"])

    def test_the_report_is_deterministic(self):
        # No timestamp and no unordered set, so two builds of one tree are equal
        # and a lane can diff two revisions directly.
        self.assertEqual(
            json.dumps(jev_diagnostics.build(REPO_ROOT), sort_keys=True),
            json.dumps(self.document, sort_keys=True),
        )

    def test_the_report_reads_no_file_content(self):
        # The allow-list property: a report that described this checkout's files
        # by content would carry the labelled credential fixture's bytes. It
        # describes them by path, digest, and count instead.
        blob = json.dumps(self.document, sort_keys=True)
        self.assertTrue(SECRET_FIXTURE.is_file(), msg=str(SECRET_FIXTURE))
        for line in SECRET_FIXTURE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.assertNotIn(line.strip(), blob)
        # And the ambient home is path-and-existence only.
        self.assertEqual(
            set(self.document["ambient_home"]), {"path", "exists", "config_present"}
        )

    def test_the_document_carries_no_credential_shaped_value(self):
        self.assertEqual(self.document["redaction"]["findings"], [])
        self.assertTrue(self.document["redaction"]["clean"])
        self.assertTrue(self.document["ok"], msg=self.document["checks"]["failures"])

    def test_a_credential_shaped_input_is_redacted_and_named(self):
        # A poisoned ambient home is a realistic input: the path is read from
        # the environment, so a misconfigured variable would otherwise land in
        # the report verbatim.
        poisoned = str(Path("/tmp/jev-diag") / f"home-{SYNTHETIC_TOKEN}")
        with mock.patch.dict(os.environ, {"CODEX_HOME": poisoned}):
            document = jev_diagnostics.build(REPO_ROOT)
        findings = document["redaction"]["findings"]
        self.assertEqual(
            findings, [{"pointer": "ambient_home/path", "rule": "scm_token"}]
        )
        self.assertFalse(document["redaction"]["clean"])
        # The value is replaced, and the finding names the rule and the pointer
        # without repeating the match.
        blob = json.dumps(document, sort_keys=True)
        self.assertNotIn(SYNTHETIC_TOKEN, blob)
        self.assertIn("[redacted:scm_token]", document["ambient_home"]["path"])
        for finding in findings:
            self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(finding))

    def test_every_declared_redaction_rule_is_exercised(self):
        # "No findings" only means something if the scanner would find
        # something, so each declared rule is given a value it must catch. This
        # fails the moment a rule is added to the candidate gate's vocabulary
        # without the diagnostics scan being able to observe it.
        samples = {
            "private_key": _shape("-----BEGIN ", "PRIVATE ", "KEY-----"),
            "provider_key": _shape("sk-", "b" * 24),
            "scm_token": SYNTHETIC_TOKEN,
            "cloud_key": _shape("AKIA", "C" * 16),
            "chat_token": _shape("xoxb-", "d" * 12),
            "bearer_header": _shape("Authorization", ": ", "Bearer ", "e" * 12),
            "secret_assignment": _shape("api_key", '": "', "f" * 12, '"'),
        }
        declared = [rule for rule, _ in release_readiness.CONTENT_RULES]
        # The two provider_key rules share a name, so compare as a set.
        self.assertEqual(set(samples), set(declared))
        for rule, sample in sorted(samples.items()):
            with self.subTest(rule=rule):
                _, findings = jev_diagnostics.redact({"value": sample})
                self.assertIn(rule, [finding["rule"] for finding in findings])

    def test_check_fails_on_a_finding_and_passes_when_clean(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jev_diagnostics.main(["report", "--quiet"]), 0)
        poisoned = str(Path("/tmp/jev-diag") / f"home-{SYNTHETIC_TOKEN}")
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": poisoned}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(jev_diagnostics.main(["report", "--check", "--quiet"]), 1)

    def test_check_refuses_a_missing_binary(self):
        # A path that does not exist is reported, not silently hashed as null.
        missing = REPO_ROOT / "jev" / "no-such-binary"
        document = jev_diagnostics.build(REPO_ROOT, binary=missing)
        self.assertFalse(document["ok"])
        self.assertIsNone(document["binary"]["sha256"])
        self.assertIn(
            "binary: the requested path does not exist", document["checks"]["failures"]
        )


if __name__ == "__main__":
    unittest.main()
