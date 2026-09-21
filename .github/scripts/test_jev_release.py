#!/usr/bin/env python3
"""Required-CI coverage for the release package (#26, phase 6.3).

These tests are stdlib-only and never launch a Codex binary or contact a
provider. They pin what the release package has to hold: the candidate artifact
carries the package and no credential or private state, it verifies against its
own record, it is byte-reproducible, and it *refuses* the cases that matter - a
tampered member, a missing required file, a record that pins a different host
base, an undeclared synthetic allowance, a tracked credential, a real key in a
product file, and an untracked one. The diagnostics report is pinned the same
way: clean on this checkout, never read from the ambient home's contents, and a
failing gate when a secret is planted in it.

Scratch space defaults to the system temp directory; set `JEV_TEST_SCRATCH` to
point it elsewhere when the default is small.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import jev_diagnostics  # noqa: E402
import jev_manifest  # noqa: E402
import release_artifact  # noqa: E402

SCRATCH_ROOT = Path(os.environ.get("JEV_TEST_SCRATCH") or tempfile.gettempdir())

# A realistic-looking key that carries no synthetic marker. It is assembled at
# run time rather than written out, because this harness is itself a packaged
# member: a literal key here would make the artifact builder refuse the harness
# it is testing.
PLANTED_KEY = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "abcdef"


def required_placeholder(relative):
    """Content for a required member the fake repo does not supply itself."""
    if relative.endswith(".rs"):
        return "// placeholder member of the fake package\n"
    if relative.endswith(".json"):
        return "{}\n"
    return "# placeholder member of the fake package\n"


def scratch():
    return tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)


def git(root, *args):
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def write_fake_repo(root, tracked=None, untracked=None, drop=()):
    """A minimal but faithful package: manifest, a profile, and the fixtures."""
    (root / "jev" / "profiles").mkdir(parents=True)
    (root / "jev" / "fixtures").mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "jev" / "compatibility-manifest.json",
        root / "jev" / "compatibility-manifest.json",
    )
    shutil.copy2(
        REPO_ROOT / "jev" / "profiles" / "isolated-offline.json",
        root / "jev" / "profiles" / "isolated-offline.json",
    )
    for fixture in sorted((REPO_ROOT / "jev" / "fixtures").glob("*.json")):
        shutil.copy2(fixture, root / "jev" / "fixtures" / fixture.name)
    # `build` refuses a missing required member, so a fake repo that omitted one
    # would test the refusal instead of the package. `drop` is how a test asks
    # for that refusal on purpose.
    for relative in release_artifact.REQUIRED_SOURCE_MEMBERS:
        if relative in drop:
            continue
        path = root / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(required_placeholder(relative), encoding="utf-8")
    for relative, text in (tracked or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    # Written after the add on purpose: these stay untracked.
    for relative, text in (untracked or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def rewrite_archive(source, target, mutate_member=None, drop=None, mutate_record=None):
    """Copy an archive, optionally editing one member, dropping one, or editing the record."""
    members = release_artifact.read_archive(source)
    if drop is not None:
        members.pop(drop, None)
    if mutate_member is not None:
        members[mutate_member[0]] = mutate_member[1]
    if mutate_record is not None:
        record = json.loads(members[release_artifact.RECORD_PATH].decode("utf-8"))
        mutate_record(record)
        members[release_artifact.RECORD_PATH] = (
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(members):
            data = members[path]
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return Path(target)


def codes(errors):
    return {error.split(":", 1)[0] for error in errors}


class CandidateArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        cls.archive = Path(cls.work.name) / "candidate.tar.gz"
        cls.result = release_artifact.build(REPO_ROOT, cls.archive)
        cls.record = cls.result["record"]

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_the_candidate_verifies_against_its_own_record(self):
        self.assertEqual(release_artifact.verify(self.archive), [])
        self.assertGreater(self.result["member_count"], 100)
        self.assertEqual(
            self.result["host_base_commit"],
            jev_manifest.load_json(
                REPO_ROOT / "jev" / "compatibility-manifest.json", "manifest"
            )["host"]["base_commit"],
        )
        self.assertEqual(
            self.result["source_revision"], git(REPO_ROOT, "rev-parse", "HEAD").strip()
        )

    def test_verify_refuses_a_revision_the_caller_did_not_expect(self):
        errors = release_artifact.verify(self.archive, expect_revision="0" * 40)
        self.assertIn("E_ARTIFACT_REVISION", codes(errors))

    def test_the_candidate_carries_the_package_and_no_private_state(self):
        members = set(self.record["members"])
        for required in release_artifact.REQUIRED_MEMBERS:
            if required == release_artifact.RECORD_PATH:
                continue
            self.assertIn(required, members)
        for path in members:
            self.assertFalse(path.startswith(".jev/"), path)
            self.assertNotIn("/target/", path)
            self.assertNotIn("__pycache__", path)
        self.assertEqual(
            self.record["manifest_sha256"],
            self.record["members"]["jev/compatibility-manifest.json"],
        )

    def test_a_synthetic_allowance_is_only_ever_a_fixture_or_a_harness(self):
        # A product file must never need an allowance: the scan's escape hatch is
        # for labelled fixtures, not for shipping code.
        allowed_roots = ("jev/tests/", ".github/scripts/test_jev_")
        for entry in self.record["exclusions"]["synthetic_findings"]:
            self.assertTrue(entry["path"].startswith(allowed_roots), entry["path"])
            self.assertIn(entry["marker"], release_artifact.SYNTHETIC_MARKERS)
            self.assertGreaterEqual(entry["occurrences"], 1)
            self.assertIn(
                entry["pattern"], [name for name, _ in release_artifact.SECRET_PATTERNS]
            )

    def test_the_candidate_is_byte_reproducible(self):
        other = Path(self.work.name) / "again.tar.gz"
        again = release_artifact.build(REPO_ROOT, other)
        self.assertEqual(again["sha256"], self.result["sha256"])

    def test_verify_rejects_a_tampered_member(self):
        tampered = rewrite_archive(
            self.archive,
            Path(self.work.name) / "tampered.tar.gz",
            mutate_member=("jev/README.md", b"# replaced\n"),
        )
        self.assertIn("E_ARTIFACT_DIGEST", codes(release_artifact.verify(tampered)))

    def test_verify_rejects_a_missing_required_member(self):
        holed = rewrite_archive(
            self.archive,
            Path(self.work.name) / "holed.tar.gz",
            drop="jev/RELEASE.md",
        )
        self.assertIn("E_ARTIFACT_MISSING", codes(release_artifact.verify(holed)))

    def test_verify_rejects_a_record_that_pins_a_different_host_base(self):
        def retarget(record):
            record["host"]["base_commit"] = "0" * 40

        mispinned = rewrite_archive(
            self.archive,
            Path(self.work.name) / "mispinned.tar.gz",
            mutate_record=retarget,
        )
        self.assertIn("E_ARTIFACT_MANIFEST", codes(release_artifact.verify(mispinned)))

    def test_verify_rejects_an_undeclared_synthetic_allowance(self):
        def drop_allowances(record):
            record["exclusions"]["synthetic_findings"] = []

        laundered = rewrite_archive(
            self.archive,
            Path(self.work.name) / "laundered.tar.gz",
            mutate_record=drop_allowances,
        )
        self.assertIn("E_ARTIFACT_EXEMPTION", codes(release_artifact.verify(laundered)))

    def test_verify_rejects_a_credential_hidden_in_the_record(self):
        def plant(record):
            record["source"]["note"] = f"leaked {PLANTED_KEY}"

        planted = rewrite_archive(
            self.archive,
            Path(self.work.name) / "planted-in-record.tar.gz",
            mutate_record=plant,
        )
        self.assertIn("E_ARTIFACT_CREDENTIAL", codes(release_artifact.verify(planted)))

    def test_the_cli_reports_ok_and_not_ok(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(release_artifact.main(["verify", str(self.archive)]), 0)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                release_artifact.main(
                    ["verify", str(self.archive), "--expect-revision", "0" * 40]
                ),
                1,
            )


class ArtifactRefusalTests(unittest.TestCase):
    def test_a_tracked_credential_by_name_is_refused(self):
        with scratch() as work:
            root = write_fake_repo(
                Path(work), tracked={"jev/scripts/auth.json": "{}\n"}
            )
            with self.assertRaises(release_artifact.ArtifactError) as caught:
                release_artifact.build(root, Path(work) / "out.tar.gz")
            self.assertIn("E_ARTIFACT_FORBIDDEN", str(caught.exception))

    def test_a_missing_required_member_is_refused_at_build_time(self):
        with scratch() as work:
            root = write_fake_repo(Path(work), drop=("jev/RELEASE.md",))
            with self.assertRaises(release_artifact.ArtifactError) as caught:
                release_artifact.build(root, Path(work) / "out.tar.gz")
            self.assertIn("E_ARTIFACT_MISSING", str(caught.exception))
            self.assertIn("jev/RELEASE.md", str(caught.exception))

    def test_a_real_key_in_a_product_file_is_refused(self):
        with scratch() as work:
            root = write_fake_repo(
                Path(work),
                tracked={"jev/scripts/leaky.py": f'KEY = "{PLANTED_KEY}"\n'},
            )
            with self.assertRaises(release_artifact.ArtifactError) as caught:
                release_artifact.build(root, Path(work) / "out.tar.gz")
            self.assertIn("E_ARTIFACT_CREDENTIAL", str(caught.exception))

    def test_a_key_with_a_synthetic_marker_is_an_allowance_not_a_refusal(self):
        with scratch() as work:
            root = write_fake_repo(
                Path(work),
                tracked={
                    "jev/scripts/fake.py": 'KEY = "sk-FIXTURE-NOT-A-REAL-KEY-0000"\n'
                },
            )
            result = release_artifact.build(root, Path(work) / "out.tar.gz")
            allowances = result["record"]["exclusions"]["synthetic_findings"]
            self.assertEqual(
                [entry["path"] for entry in allowances], ["jev/scripts/fake.py"]
            )
            self.assertEqual(release_artifact.verify(Path(work) / "out.tar.gz"), [])

    def test_untracked_state_never_enters_the_candidate(self):
        with scratch() as work:
            root = write_fake_repo(
                Path(work),
                untracked={
                    ".jev/isolated/isolated-env.json": "{}\n",
                    "jev/scripts/untracked-leak.py": f'KEY = "{PLANTED_KEY}"\n',
                },
            )
            result = release_artifact.build(root, Path(work) / "out.tar.gz")
            members = set(result["record"]["members"])
            self.assertNotIn(".jev/isolated/isolated-env.json", members)
            self.assertNotIn("jev/scripts/untracked-leak.py", members)

    def test_the_cli_refuses_with_exit_one(self):
        with scratch() as work:
            root = write_fake_repo(
                Path(work), tracked={"jev/scripts/auth.json": "{}\n"}
            )
            with contextlib.redirect_stderr(io.StringIO()):
                exit_code = release_artifact.main(
                    [
                        "build",
                        "--root",
                        str(root),
                        "--out",
                        str(Path(work) / "out.tar.gz"),
                    ]
                )
            self.assertEqual(exit_code, 1)


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = jev_diagnostics.report(REPO_ROOT)

    def test_the_report_is_clean_for_this_checkout(self):
        self.assertTrue(
            self.document["manifest"]["ok"], self.document["manifest"]["errors"]
        )
        self.assertTrue(self.document["profiles"])
        for profile in self.document["profiles"]:
            self.assertTrue(profile["ok"], (profile["id"], profile["errors"]))
        self.assertEqual(self.document["redaction"]["findings"], [])
        self.assertEqual(jev_diagnostics.evaluate(self.document), [])

    def test_the_report_describes_state_without_reading_contents(self):
        self.assertIn(self.document["patches"]["state"], ("applied", "absent"))
        self.assertIn(self.document["native_adapter"]["state"], ("applied", "absent"))
        self.assertEqual(
            sorted(self.document["ambient_home"]), ["config_present", "exists", "path"]
        )
        self.assertLessEqual(
            set(self.document["isolated_env"]),
            {"env_dir", "present", "status", "error"},
        )

    def test_the_report_never_reads_the_ambient_home_contents(self):
        with scratch() as work:
            home = Path(work) / "ambient-codex-home"
            home.mkdir()
            (home / "config.toml").write_text(
                f'api_key = "{PLANTED_KEY}"\n', encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                document = jev_diagnostics.report(REPO_ROOT)
        text = json.dumps(document, sort_keys=True)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", text)
        self.assertTrue(document["ambient_home"]["config_present"])
        self.assertEqual(document["redaction"]["findings"], [])

    def test_a_planted_secret_fails_the_check(self):
        document = json.loads(json.dumps(self.document))
        document["notes"] = f"a leaked value {PLANTED_KEY}"
        document["redaction"]["findings"] = jev_diagnostics.scrub(document)
        failures = jev_diagnostics.evaluate(document)
        self.assertIn("E_DIAGNOSTICS_REDACTION", codes(failures))

    def test_the_check_honours_an_explicit_expected_state(self):
        expected = (
            "absent" if self.document["patches"]["state"] == "applied" else "applied"
        )
        failures = jev_diagnostics.evaluate(self.document, expect_patch_state=expected)
        self.assertIn("E_DIAGNOSTICS_EXPECT", codes(failures))

    def test_a_component_drift_fails_the_check(self):
        with scratch() as work:
            components = Path(work) / "checkouts"
            checkout = components / "jev-sentinel"
            checkout.mkdir(parents=True)
            (checkout / "README.md").write_text("a local checkout\n", encoding="utf-8")
            git(checkout, "init", "-q", "-b", "main")
            git(checkout, "add", "-A")
            git(
                checkout,
                "-c",
                "user.email=jev@test",
                "-c",
                "user.name=jev",
                "commit",
                "-q",
                "-m",
                "local",
            )
            document = jev_diagnostics.report(REPO_ROOT, components_root=components)
        self.assertIn(
            "E_DIAGNOSTICS_COMPONENT", codes(jev_diagnostics.evaluate(document))
        )


class ReleaseDocumentTests(unittest.TestCase):
    def test_the_release_document_names_every_gate(self):
        text = (REPO_ROOT / "jev" / "RELEASE.md").read_text(encoding="utf-8")
        for gate in range(1, 10):
            self.assertIn(f"| G{gate} |", text)
        # The live-provider gate is the one the package deliberately leaves open.
        live_row = next(line for line in text.splitlines() if line.startswith("| G8 |"))
        self.assertIn("open", live_row)

    def test_the_release_document_names_the_install_order_and_rollback(self):
        text = (REPO_ROOT / "jev" / "RELEASE.md").read_text(encoding="utf-8")
        for heading in (
            "## What ships",
            "## The candidate artifact",
            "## Diagnostics",
            "## Install order",
            "## State ownership",
            "## Upgrade order",
            "## Backups",
            "## Rollback",
            "## Supported platform matrix",
            "## Known limitations",
            "## Release gates",
        ):
            self.assertIn(heading, text)

    def test_build_and_verify_refuse_the_same_required_members(self):
        # The record is generated by `build`, so the two halves differ by it and
        # by nothing else; a drift here would let an archive build that cannot
        # verify.
        self.assertEqual(
            set(release_artifact.REQUIRED_SOURCE_MEMBERS),
            set(release_artifact.REQUIRED_MEMBERS) - {release_artifact.RECORD_PATH},
        )
        self.assertIn(release_artifact.RECORD_PATH, release_artifact.REQUIRED_MEMBERS)


class ReleaseEvidenceTests(unittest.TestCase):
    """The gates in RELEASE.md point at an evidence document that has to hold."""

    EVIDENCE = "evidence/release-package-fresh-setup.md"

    def text(self, relative):
        return (REPO_ROOT / "jev" / relative).read_text(encoding="utf-8")

    def test_the_documents_that_claim_it_point_at_it_and_it_exists(self):
        for name in ("RELEASE.md", "ROLLBACK.md"):
            self.assertIn(self.EVIDENCE, self.text(name))
        self.assertTrue((REPO_ROOT / "jev" / self.EVIDENCE).is_file())

    def test_the_evidence_records_the_sections_a_release_gate_needs(self):
        text = self.text(self.EVIDENCE)
        for heading in (
            "## Revisions",
            "## What was run",
            "## Results",
            "## What this does not show",
            "## Reproduce",
        ):
            self.assertIn(heading, text)
        self.assertIn("G5", text)
        self.assertIn("#26", text)
        # It has to state the limits rather than only the passes.
        self.assertIn("G8 is open", text)

    def test_the_evidence_revision_is_a_real_commit_in_this_history(self):
        match = re.search(
            r"\| Verified revision of this repository \| `([0-9a-f]{40})`",
            self.text(self.EVIDENCE),
        )
        self.assertIsNotNone(match, "the evidence document records no revision")
        revision = match.group(1)
        present = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if present.returncode != 0:
            self.skipTest(f"{revision} is not in this clone, so it cannot be checked")
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "merge-base",
                "--is-ancestor",
                revision,
                "HEAD",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            ancestor.returncode,
            0,
            f"{revision} is not an ancestor of this checkout; the recorded run "
            "has to belong to the history it claims",
        )


if __name__ == "__main__":
    unittest.main()
