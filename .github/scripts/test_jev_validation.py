#!/usr/bin/env python3
"""Required-CI coverage for the offline performance validation (#25).

`repo-checks` discovers `.github/scripts/test_jev_*.py`, so the phase-6.2 claims
are pinned here rather than only in `jev/tests`. This file asserts the
properties that make the measurement trustworthy rather than merely green:

* it passes, and every declared case is individually ok;
* the integrated request is strictly smaller than the baseline, and dedup shrinks
  bytes without changing the item count;
* every case that must fall back is byte-identical, and every refusal is counted
  and named rather than dropped;
* no case claims a measured token count, and the token figure is labelled a
  byte-derived estimate;
* service usage is fixed at zero provider requests;
* every case carries an offline tier and none claims a `real-host` or
  `live-provider` run;
* the deterministic digest excludes the observed latency (two runs at different
  `repetitions` agree);
* the harness refuses an unusable fixture directory (exit 2) instead of passing.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import perf_validation  # noqa: E402


class MeasurementTests(unittest.TestCase):
    """One measurement per class; the harness is offline and deterministic."""

    @classmethod
    def setUpClass(cls):
        cls.document = perf_validation.measure(repetitions=3)

    def cases(self):
        return {case["name"]: case for case in self.document["cases"]}

    def test_every_declared_case_is_individually_ok(self):
        self.assertTrue(self.document["ok"])
        self.assertEqual(self.document["summary"]["failed"], 0)
        for case in self.document["cases"]:
            with self.subTest(case=case["name"]):
                self.assertTrue(case["ok"])

    def test_the_integrated_request_is_strictly_smaller(self):
        summary = self.document["summary"]
        self.assertGreater(summary["bytes_removed"], 0)
        self.assertLess(summary["integrated_bytes"], summary["baseline_bytes"])
        self.assertLess(summary["bytes_ratio"], 1.0)
        dedup = self.cases()["dedup_only_replaces_the_body"]
        self.assertLess(dedup["bytes_after"], dedup["bytes_before"])
        self.assertEqual(dedup["items_before"], dedup["items_after"])

    def test_every_fallback_case_is_byte_identical(self):
        cases = self.cases()
        for name, case in cases.items():
            if case["fallback"]:
                with self.subTest(case=name):
                    self.assertEqual(case["bytes_after"], case["bytes_before"])
        self.assertGreaterEqual(self.document["summary"]["fell_back"], 4)

    def test_every_refusal_is_counted_and_named(self):
        cases = self.cases()
        refused = [case for case in cases.values() if case["refused"]]
        self.assertTrue(refused)
        for case in refused:
            with self.subTest(case=case["name"]):
                self.assertTrue(case["code"], msg="a refusal must name its code")
        self.assertEqual(
            sorted(self.document["summary"]["refused_names"]),
            sorted(case["name"] for case in refused),
        )

    def test_no_case_claims_a_measured_token_count(self):
        self.assertIsNone(self.document["summary"]["tokens_measured"])
        estimated = self.document["summary"]["tokens_estimated"]
        self.assertTrue(estimated["note"].startswith("byte-derived"))
        for case in self.document["cases"]:
            self.assertNotIn("tokens_measured", case)

    def test_service_usage_is_zero_and_no_tier_overclaims(self):
        self.assertEqual(
            self.document["summary"]["service_usage"]["provider_requests"], 0
        )
        tiers = {case["tier"] for case in self.document["cases"]}
        self.assertTrue(tiers <= {"offline-fixture", "bus-stage-stub"})
        self.assertNotIn("real-host", tiers)
        self.assertNotIn("live-provider", tiers)
        self.assertEqual(self.document["tier_labels"]["real_host"], [])
        self.assertEqual(self.document["tier_labels"]["live_provider"], [])

    def test_the_digest_excludes_the_observed_latency(self):
        again = perf_validation.measure(repetitions=7)
        self.assertEqual(
            again["deterministic_digest"], self.document["deterministic_digest"]
        )
        summary = self.document["summary"]["latency_ms"]
        self.assertEqual(summary["source"], "observed")
        self.assertEqual(len(summary["samples"]), 3)


class RefusalTests(unittest.TestCase):
    """A harness that cannot fail is not evidence."""

    def test_a_missing_fixture_directory_is_a_usage_error(self):
        with tempfile.TemporaryDirectory(prefix="jev-perf-none-") as empty:
            code = perf_validation.main(["run", "--quiet", "--fixtures", empty])
            self.assertEqual(code, 2)

    def test_the_document_is_json_serializable_and_stable(self):
        one = perf_validation.measure(repetitions=2)
        two = perf_validation.measure(repetitions=2)

        def facts(document):
            body = json.loads(json.dumps(document, sort_keys=True))
            body["summary"].pop("latency_ms")
            return json.dumps(body, sort_keys=True)

        self.assertEqual(one["deterministic_digest"], two["deterministic_digest"])
        self.assertEqual(facts(one), facts(two))


if __name__ == "__main__":
    unittest.main()
