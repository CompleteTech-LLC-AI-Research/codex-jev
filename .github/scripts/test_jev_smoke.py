"""Required-CI gate for the host-driven smokes under ``jev/smoke``.

Runs from ``just test-github-scripts`` alongside the other repository-level
Python checks. Each smoke fixture drives a real Codex host -- one through a
parent/child collaboration turn, one through a seeded projection transcript,
one through a transcript whose duplicate pair the host produces itself by
executing a read tool; CI cannot build that host, so what this gates is the
half that must never rot: the checkers. Each has to pass the honest shape and
*reject* the hollow ones.
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
READ_TOOL_CHECKER = SMOKE_ROOT / "check_read_tool_projection.py"
READ_TOOL_SELF_TEST = SMOKE_ROOT / "self_test_read_tool_projection.py"


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
            "check_read_tool_projection.py",
            "run-read-tool-projection-smoke.sh",
            "self_test_read_tool_projection.py",
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

    def test_read_tool_checker_keeps_its_tier_and_marker_honest(self):
        checker = load_module("jev_smoke_check_read_tool_projection", READ_TOOL_CHECKER)
        pinned = load_module(
            "jev_smoke_pinned_dedup",
            REPO_ROOT / "jev" / "scripts" / "dedup_receipts.py",
        )
        # The checker re-derives the replacement the *host* proves; a lookalike
        # marker regex here would let the fixture accept a shape the carrier
        # itself refuses.
        self.assertEqual(
            checker.MARKER_RE.pattern,
            pinned.MARKER_RE.pattern,
            "the fixture's marker no longer matches the pinned dedup policy",
        )
        import inspect

        source = inspect.getsource(checker.main)
        # A projection run over a loopback mock measures serialized bytes. If the
        # tier label or the unmeasured-token line were dropped, the same numbers
        # would read as a token measurement or as live-provider evidence.
        self.assertIn("real-host-binary", source, "tier label removed")
        self.assertIn("unmeasured", source, "unmeasured-token disclaimer removed")

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

    def test_read_tool_self_test_passes(self):
        result = subprocess.run(
            [sys.executable, str(READ_TOOL_SELF_TEST)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            "jev/smoke/self_test_read_tool_projection.py failed:\n"
            f"{result.stdout}\n{result.stderr}",
        )
        self.assertIn("self-test ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
