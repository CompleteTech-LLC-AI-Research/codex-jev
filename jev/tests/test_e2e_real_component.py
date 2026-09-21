#!/usr/bin/env python3
"""Real-component coverage for the composed end-to-end harness (#24).

The required-CI lane drives every composed scenario with the stub component,
because CI has no pinned ``jev-sentinel`` checkout. This module drives the same
harness with the *real* checkout through ``launch.py``, so the tier is
``real-component`` (local execution, no network, no provider).

The point is that the harness does not change shape between tiers: the same
fixtures, the same composed order, and the same assertions must pass, and only
the component's own decisions come from the real evaluator. A run that quietly
fell back to the stub would still be green, so the assertions below pin the tier
to the resolved checkout and its revision.

When no checkout is resolvable the module skips with the reason rather than
passing quietly: an unproven tier must not look verified.

Run with: python3 -m unittest discover -s jev/tests -t jev/tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import e2e_regression  # noqa: E402
import sentinel_boundary as sb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHECKOUTS = Path("/home/agent/jev/checkouts")


def find_component():
    for candidate in (
        os.environ.get("JEV_SENTINEL_ROOT", ""),
        str(DEFAULT_CHECKOUTS / "jev-sentinel"),
    ):
        if candidate and (Path(candidate) / "launch.py").is_file():
            return Path(candidate)
    return None


COMPONENT = find_component()


@unittest.skipUnless(
    COMPONENT,
    "no pinned jev-sentinel checkout; set JEV_SENTINEL_ROOT to run the real-component tier",
)
class ComposedRealComponentTestCase(unittest.TestCase):
    """One composed run against the pinned checkout, isolated per test."""

    def test_the_composed_scenarios_pass_with_the_real_component(self):
        with tempfile.TemporaryDirectory(prefix="jev-e2e-real-") as work:
            document, _ = e2e_regression.run(
                scratch=Path(work) / "scratch",
                component=str(COMPONENT),
                root=REPO_ROOT,
            )

        self.assertTrue(
            document["ok"],
            msg="failed: " + repr([c["id"] for c in document["checks"] if not c["ok"]]),
        )
        self.assertEqual(document["counts"]["failed"], 0)
        self.assertGreaterEqual(document["counts"]["checks"], 50)

        component = document["component"]
        self.assertFalse(component["stub"], msg="a real checkout must not be a stub")
        self.assertEqual(component["tier"], "real-component")
        self.assertEqual(component["revision"], sb.component_revision(COMPONENT))

        tiers = {check["tier"] for check in document["checks"]}
        self.assertIn("real-component", tiers)
        self.assertNotIn("component-stub", tiers)

    def test_the_real_component_run_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="jev-e2e-real-det-") as first:
            one, _ = e2e_regression.run(
                scratch=Path(first) / "a", component=str(COMPONENT), root=REPO_ROOT
            )
        with tempfile.TemporaryDirectory(prefix="jev-e2e-real-det-") as second:
            two, _ = e2e_regression.run(
                scratch=Path(second) / "b", component=str(COMPONENT), root=REPO_ROOT
            )
        self.assertEqual(one["digest"], two["digest"])


if __name__ == "__main__":
    unittest.main()
