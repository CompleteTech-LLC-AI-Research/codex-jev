#!/usr/bin/env python3
"""Required-CI coverage for the isolated JEV build and integration profile.

These tests are stdlib-only and never launch a Codex binary, so they run on
every pull request. They pin the parts of the isolated environment that a
future change could silently break: the plan refuses to enable optional
features, the generated home stays offline and outside the ambient Codex home,
the fixtures are deterministic and loopback-only, launch resolves without a
binary, and rollback only moves the environment aside.
"""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import build_provenance  # noqa: E402
import isolated_env  # noqa: E402
import jev_manifest  # noqa: E402
import launch_isolated  # noqa: E402
import offline_fixtures  # noqa: E402


class IsolatedPlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest = isolated_env.load_manifest(REPO_ROOT)

    def test_isolated_profile_disables_every_optional_feature(self):
        plan = isolated_env.resolve_plan(root=REPO_ROOT)
        self.assertEqual(plan["optional_features_enabled"], [])
        for name, enabled in plan["features"].items():
            if not self.manifest["features"][name]["default"]:
                self.assertFalse(enabled, name)
        self.assertFalse(plan["remote_inference"]["enabled"])

    def test_optional_feature_profile_is_rejected(self):
        # `integrated-offline` turns optional enforcement paths on, so it must
        # not be usable as the isolated starting profile.
        with self.assertRaises(isolated_env.EnvError):
            isolated_env.resolve_plan(profile_id="integrated-offline", root=REPO_ROOT)

    def test_plan_records_profile_digest_and_host_pin(self):
        plan = isolated_env.resolve_plan(root=REPO_ROOT)
        digest = jev_manifest.sha256_file(
            REPO_ROOT / "jev" / "profiles" / "isolated-offline.json"
        )
        self.assertEqual(plan["profile_digest"], digest)
        self.assertEqual(plan["host_commit"], self.manifest["host"]["base_commit"])

    def test_switch_contract_is_derived_from_features(self):
        plan = isolated_env.resolve_plan(root=REPO_ROOT)
        switch = isolated_env.switch_env(plan)
        self.assertEqual(switch["SENTINEL_SHADOW"], "0")
        self.assertEqual(switch["APPROVAL_PREFLIGHT"], "0")
        self.assertEqual(switch["REMOTE_INFERENCE_ENABLED"], "0")
        self.assertEqual(switch["COLLAB_PLAINTEXT_MESSAGES"], "1")
        self.assertEqual(
            set(switch), {name.upper().replace(".", "_") for name in plan["features"]}
        )


class GeneratedHomeTests(unittest.TestCase):
    def test_generated_home_is_offline_and_leaves_ambient_home_alone(self):
        with tempfile.TemporaryDirectory() as work:
            ambient = Path(work) / "ambient-codex-home"
            ambient.mkdir()
            sentinel = ambient / "config.toml"
            sentinel.write_text('model = "sentinel"\n', encoding="utf-8")
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)

            config = (env_dir / "home" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model_provider = "jev_offline"', config)
            self.assertIn("127.0.0.1", config)
            self.assertNotIn("api.openai.com", config)
            self.assertNotIn("[features]", config)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), 'model = "sentinel"\n'
            )

    def test_environment_records_switches_and_launcher(self):
        with tempfile.TemporaryDirectory() as work:
            env_dir = Path(work) / "isolated"
            plan = isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            profile_record = json.loads(
                (env_dir / "jev-profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile_record["features"], plan["features"])
            self.assertEqual(profile_record["switch_env"], plan["switch_env"])
            launcher = env_dir / isolated_env.LAUNCHER_REL
            self.assertTrue(launcher.is_file())
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertIn("launch_isolated.py", launcher.read_text(encoding="utf-8"))
            status = isolated_env.status(env_dir)
            # The plan points at the repository's default binary, so presence
            # depends on whether a build exists in this checkout. Assert the
            # report agrees with the filesystem rather than a fixed value.
            expected_present = isolated_env.default_binary(REPO_ROOT).is_file()
            self.assertEqual(status["binary_present"], expected_present)
            self.assertEqual(status["binary_matches_plan"], expected_present)
            self.assertEqual(status["optional_features_enabled"], [])

    def test_init_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as work:
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            with self.assertRaises(isolated_env.EnvError):
                isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT, force=True)


class RollbackTests(unittest.TestCase):
    def test_rollback_moves_only_the_environment(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / "repo"
            (root / "jev" / "profiles").parent.mkdir(parents=True, exist_ok=True)
            # A real manifest is not needed for rollback; use the repository's
            # one via the default root instead.
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            bystander = Path(work) / "keep-me.txt"
            bystander.write_text("keep\n", encoding="utf-8")

            result = isolated_env.rollback(
                env_dir=env_dir,
                root=REPO_ROOT,
                stamp="teststamp",
                target_root=Path(work) / "superseded",
            )
            self.assertEqual(result["state"], "moved")
            self.assertFalse(env_dir.exists())
            self.assertTrue(Path(result["moved_to"], "isolated-env.json").is_file())
            self.assertEqual(bystander.read_text(encoding="utf-8"), "keep\n")

    def test_rollback_of_absent_environment_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as work:
            result = isolated_env.rollback(
                env_dir=Path(work) / "missing", root=REPO_ROOT
            )
            self.assertEqual(result["state"], "absent")


class OfflineFixtureTests(unittest.TestCase):
    def test_catalog_is_ordered_and_tiered(self):
        fixtures = offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")
        priorities = [fixture["priority"] for fixture in fixtures]
        self.assertEqual(priorities, sorted(priorities))
        for fixture in fixtures:
            self.assertEqual(fixture["tier"], "offline-fixture")

    def test_specific_fixtures_win_over_the_fallback(self):
        fixtures = offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")
        chosen = offline_fixtures.select_fixture(
            fixtures,
            '{"input":[{"type":"message","text":"JEV_FIXTURE_COLLAB_ENCRYPTED"}]}',
        )
        self.assertEqual(chosen["id"], "collab_encrypted")
        fallback = offline_fixtures.select_fixture(fixtures, '{"input":[]}')
        self.assertEqual(fallback["id"], "assistant_message")

    def test_targeted_fixture_is_bounded_and_falls_through(self):
        # A targeted fixture answers once, then the conversation falls through
        # to the fallback so a fixture run can terminate instead of looping.
        fixtures = offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")
        request = '{"input":[{"type":"message","text":"JEV_FIXTURE_COLLAB_ENCRYPTED"}]}'
        served = {}
        first = offline_fixtures.select_fixture(fixtures, request, served)
        self.assertEqual(first["id"], "collab_encrypted")
        served[first["id"]] = served.get(first["id"], 0) + 1
        second = offline_fixtures.select_fixture(fixtures, request, served)
        self.assertEqual(second["id"], "assistant_message")
        # The fallback itself carries no bound and keeps answering.
        self.assertIsNone(second["max_matches"])

    def test_rendered_sse_is_deterministic(self):
        fixtures = offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")
        first = offline_fixtures.render_sse(fixtures[0])
        second = offline_fixtures.render_sse(
            offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")[0]
        )
        self.assertEqual(first, second)
        for line in first.splitlines():
            if line.startswith("event: "):
                self.assertIn(
                    line.split("event: ", 1)[1],
                    {
                        "response.created",
                        "response.output_item.done",
                        "response.completed",
                    },
                )

    def test_service_is_loopback_only_and_serves_fixtures(self):
        fixtures = offline_fixtures.load_fixtures(REPO_ROOT / "jev" / "fixtures")
        server = offline_fixtures.build_server(fixtures, port=0)
        self.addCleanup(server.server_close)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["fixtures"], len(fixtures))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)

    def test_routable_bind_is_refused(self):
        stderr = sys.stderr
        try:
            sys.stderr = open(  # noqa: SIM115 - restored in finally
                "/dev/null", "w", encoding="utf-8"
            )
            exit_code = offline_fixtures.main(["--host", "0.0.0.0"])
        finally:
            sys.stderr.close()
            sys.stderr = stderr
        self.assertEqual(exit_code, 2)


class LaunchPlanTests(unittest.TestCase):
    def test_dry_run_resolves_without_a_binary(self):
        with tempfile.TemporaryDirectory() as work:
            env_dir = Path(work) / "isolated"
            isolated_env.init_env(env_dir=env_dir, root=REPO_ROOT)
            result = launch_isolated.run(env_dir, ["exec", "hello"], dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertIn("exec", result["command"])
            self.assertEqual(result["switch_env"]["SENTINEL_SHADOW"], "0")
            self.assertEqual(result["codex_home"], str(env_dir / "home"))


class ProvenanceTests(unittest.TestCase):
    def test_provenance_pins_patches_components_and_fixtures(self):
        provenance = build_provenance.collect(REPO_ROOT)
        manifest = isolated_env.load_manifest(REPO_ROOT)
        self.assertEqual(
            provenance["host_pinned_base_commit"], manifest["host"]["base_commit"]
        )
        self.assertEqual(
            provenance["patches"],
            [{"id": p["id"], "sha256": p["sha256"]} for p in manifest["patches"]],
        )
        self.assertEqual(
            provenance["components"],
            [
                {"id": c["id"], "revision": c["revision"]}
                for c in manifest["components"]
            ],
        )
        self.assertEqual(provenance["fixture_tier"], "offline-fixture")
        self.assertTrue(provenance["fixture_catalog_digest"])
        self.assertIn("rustc", provenance)


if __name__ == "__main__":
    unittest.main()
