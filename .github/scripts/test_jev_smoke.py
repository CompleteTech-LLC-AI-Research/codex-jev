"""Required-CI gate for the host-driven plaintext smoke under ``jev/smoke``.

Runs from ``just test-github-scripts`` alongside the other repository-level
Python checks. The smoke fixture drives a real Codex host through a
parent/child collaboration turn; CI cannot build that host, so what it gates is
the half that must never rot: the checker itself has to pass the plaintext
shape and *reject* the encrypted shape, a missing child turn, and a call that
drops the tool namespace.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_ROOT = REPO_ROOT / "jev" / "smoke"
SELF_TEST = SMOKE_ROOT / "self_test.py"
PROJECTION_SELF_TEST = SMOKE_ROOT / "self_test_projection.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SmokeFixtureTests(unittest.TestCase):
    def test_fixture_files_are_checked_in(self):
        for name in (
            "check_plaintext.py",
            "mock_responses_server.py",
            "self_test.py",
            "run-plaintext-smoke.sh",
            "collect_evidence.py",
            "check_projection_reset.py",
            "gen_projection_receipts.py",
            "mock_projection_server.py",
            "run-projection-reset-smoke.sh",
            "seed_projection_rollout.py",
            "self_test_projection.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    (SMOKE_ROOT / name).is_file(), f"missing jev/smoke/{name}"
                )

    def test_projection_checker_asserts_the_boundary_properties(self):
        checker = load_module(
            "jev_smoke_check_projection", SMOKE_ROOT / "check_projection_reset.py"
        )
        import inspect

        source = inspect.getsource(checker.main)
        # A checker that stopped asserting the reduction, the exact reset, or the
        # absence of a persisted projection would still pass a real run.
        for required in (
            "reduction-strictly-smaller",
            "exact-reset",
            "no-persisted-projection-receipt",
            "receipt-emitted-only-when-switched-on",
            "rollout-unprojected",
        ):
            with self.subTest(assertion=required):
                self.assertIn(required, source)
        self.assertEqual(checker.TIER, "real-host-binary", "tier label changed")

    def test_checker_requires_a_child_turn(self):
        checker = load_module(
            "jev_smoke_check_plaintext", SMOKE_ROOT / "check_plaintext.py"
        )
        # The turn script is what proves a real parent/child turn happened; a
        # checker that tolerated its absence would pass on outbound bodies alone.
        self.assertEqual(
            checker.EXPECTED_NAMESPACE, "collaboration", "namespace assertion removed"
        )
        import inspect

        source = inspect.getsource(checker.main)
        for required in ("parent_initial", "child", "parent_after_spawn"):
            with self.subTest(turn=required):
                self.assertIn(required, source)

    def test_self_test_passes(self):
        result = subprocess.run(
            [sys.executable, str(SELF_TEST)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"jev/smoke/self_test.py failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("self-test: passed", result.stdout)

    def test_projection_self_test_passes(self):
        result = subprocess.run(
            [sys.executable, str(PROJECTION_SELF_TEST)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"jev/smoke/self_test_projection.py failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("self-test: passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
