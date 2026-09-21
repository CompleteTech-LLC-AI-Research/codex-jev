#!/usr/bin/env python3
"""Build and verify the codex-jev release candidate artifact.

This is the packaging half of phase 6.3: one archive that carries the
integration package for a pinned revision, plus a record that pins exactly what
went into it.

``build``   select the integration package from the *tracked* files of a
            checkout, refuse anything credential-shaped or captured, and write a
            deterministic ``.tar.gz`` carrying a generated
            ``jev/release-record.json``.
``verify``  read an artifact back and fail closed: required members present,
            forbidden members absent, no secret-shaped content outside the one
            declared fixture exemption, every member digest equal to the record,
            and the record's pins equal to the manifest the artifact itself
            carries.

Only git-tracked paths are eligible, so the isolated environment (``.jev/``),
build output (``codex-rs/target/``), component checkouts, and any untracked
capture or credential are excluded structurally rather than by guessing at
names. The deny-list below then fails closed on a *tracked* file that is still
credential-shaped - the case a structural rule cannot catch - and a content
scan refuses an archive whose bytes contain a secret.

The content scan has exactly one escape hatch, and it is per-finding rather than
per-path: a match is allowed only when the matched text itself carries a
synthetic marker (``FIXTURE``, ``EXAMPLE``, ``AAAA``, ...). Checked-in fixtures
that exist to prove the redaction path - the capture fixtures and the two
required-CI harnesses that plant a fake key - are the only things that trip it.
Every allowance is named in the record with its path, digest, and pattern, and
``verify`` recomputes the allowance set, so it cannot be widened silently and no
directory is blanket-exempt.

Exit codes: 0 = ok, 1 = refusal or verification failure, 2 = usage error.
"""

import argparse
import fnmatch
import hashlib
import gzip
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_manifest
import offline_fixtures

ARTIFACT_VERSION = 1
RECORD_PATH = "jev/release-record.json"
DEFAULT_ARCHIVE_NAME = "codex-jev-candidate.tar.gz"

# Everything the integration owns lives under `jev/`, plus the two native
# modules the host itself compiles and the required-CI harnesses that validate
# the package. Nothing else is shipped.
INCLUDE_PREFIXES = ("jev/",)
INCLUDE_EXACT = (
    "codex-rs/core/src/jev_bus.rs",
    "codex-rs/core/src/jev_bus_tests.rs",
    "codex-rs/core/src/guardian/jev.rs",
)
INCLUDE_TEST_PREFIX = ".github/scripts/test_jev_"

# A tracked file that matches one of these is a refusal, not a warning.
FORBIDDEN_PATTERNS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.token",
    "id_rsa*",
    "id_ed25519*",
    ".env",
    ".env.*",
    "auth.json",
    "credentials.json",
    ".netrc",
    "tokens.json",
)
FORBIDDEN_SEGMENTS = (".git", ".jev", "__pycache__", "node_modules", "rollouts", "sessions")

# Lengths are deliberately conservative: only a realistically long value is
# treated as a credential, so a short illustrative token in documentation is not
# a finding. Provider-prefixed keys are the primary signal; the header and JWT
# shapes are length-bounded because a header name is not a credential.
SECRET_PATTERNS = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"[Bb]earer [A-Za-z0-9._~+/-]{48,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}")),
)

# A match that carries one of these is a labelled fixture, not a credential.
# The marker must be inside the matched text, so it cannot be sprinkled
# somewhere else in the file to launder a real key.
SYNTHETIC_MARKERS = (
    "fixture",
    "notareal",
    "not-a-real",
    "example",
    "placeholder",
    "redacted",
    "dummy",
    "sample",
    "aaaa",
)

REQUIRED_MEMBERS = (
    "jev/README.md",
    "jev/RELEASE.md",
    "jev/ROLLBACK.md",
    "jev/compatibility-manifest.json",
    "jev/patches/0001-disable-collab-message-encryption.patch",
    "jev/patches/0002-jev-bus-boundary.patch",
    "jev/profiles/isolated-offline.json",
    "jev/scripts/isolated_env.py",
    "jev/scripts/jev_diagnostics.py",
    "jev/scripts/jev_manifest.py",
    "jev/scripts/release_artifact.py",
    "jev/scripts/verify-manifest.py",
    "codex-rs/core/src/jev_bus.rs",
    "codex-rs/core/src/guardian/jev.rs",
    RECORD_PATH,
)


class ArtifactError(Exception):
    """The artifact cannot be built or read as requested."""


def eligible(path):
    return (
        path.startswith(INCLUDE_PREFIXES)
        or path in INCLUDE_EXACT
        or path.startswith(INCLUDE_TEST_PREFIX)
    )


def forbidden_reason(path):
    """Return the rule that forbids ``path``, or ``None`` when it is allowed."""
    name = path.rsplit("/", 1)[-1]
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern):
            return pattern
    for segment in path.split("/")[:-1]:
        if segment in FORBIDDEN_SEGMENTS:
            return f"*/{segment}/*"
    return None


def scan_secrets(data):
    """Return ``(refusals, allowances)`` for the secret patterns ``data`` matches.

    Each entry is ``(pattern_id, marker)``: ``marker`` is ``None`` when nothing
    in the matched text says the value is synthetic, and the matched text itself
    is never returned, so the scan can report on a credential without copying it.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], []
    refusals, allowances = [], []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).lower()
            marker = next((m for m in SYNTHETIC_MARKERS if m in value), None)
            entry = (name, marker)
            (allowances if marker else refusals).append(entry)
    return refusals, allowances


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    """Run git in ``root``; return stdout, or ``None`` when it cannot run."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def tracked_paths(root):
    listing = git(root, "ls-files", "-z")
    if listing is None:
        raise ArtifactError(
            f"E_ARTIFACT_CHECKOUT: {root} is not a readable git checkout; the "
            "candidate is selected from tracked files"
        )
    return sorted(path for path in listing.split("\0") if path)


def checkout_state(root):
    revision = (git(root, "rev-parse", "HEAD") or "").strip() or None
    porcelain = git(root, "status", "--porcelain")
    return {
        "revision": revision,
        "worktree_dirty": None if porcelain is None else bool(porcelain.strip()),
    }


def fixture_catalog_digest(root):
    catalog = offline_fixtures.load_fixtures(Path(root) / "jev" / "fixtures")
    payload = json.dumps(offline_fixtures.fixture_catalog(catalog), sort_keys=True)
    return {
        "tier": offline_fixtures.FIXTURE_TIER,
        "count": len(catalog),
        "catalog_digest": hashlib.sha256(payload.encode()).hexdigest(),
    }


def release_record(root, manifest, payloads, exempt):
    state = checkout_state(root)
    record = {
        "release_record_version": ARTIFACT_VERSION,
        "artifact": {
            "generator": "jev/scripts/release_artifact.py",
            "name": DEFAULT_ARCHIVE_NAME,
            "member_rule": (
                "git-tracked paths under jev/, the two native modules the host "
                "compiles, and the required-CI jev harnesses"
            ),
        },
        "source": {
            "repository": manifest["integration"]["repository"],
            "revision": state["revision"],
            "revision_source": "git rev-parse HEAD",
            "worktree_dirty": state["worktree_dirty"],
        },
        "host": {
            "base_commit": manifest["host"]["base_commit"],
            "rust_toolchain": manifest["host"]["rust_toolchain"],
            "python_requirement": manifest["host"]["python_requirement"],
            "supported_platforms": manifest["host"]["platforms"]["supported"],
            "reference_platform": manifest["host"]["platforms"]["reference"],
        },
        "integration": {
            "epic_issue": manifest["integration"]["epic_issue"],
            "phase_issues": manifest["integration"]["phase_issues"],
            "sub_issues": manifest["integration"]["sub_issues"],
            "excluded_repositories": manifest["integration"]["excluded_repositories"],
        },
        "interfaces": manifest["interfaces"],
        "patches": [
            {"id": patch["id"], "sha256": patch["sha256"], "order": patch["order"]}
            for patch in manifest["patches"]
        ],
        "components": [
            {
                "id": component["id"],
                "revision": component["revision"],
                "python_requirement": component.get("python_requirement"),
            }
            for component in manifest["components"]
        ],
        "profiles": sorted(
            path for path in payloads if path.startswith("jev/profiles/")
        ),
        "feature_defaults": {
            name: bool(spec.get("default"))
            for name, spec in sorted(manifest.get("features", {}).items())
        },
        "credentials": manifest["credentials"],
        "fixtures": fixture_catalog_digest(root),
        "manifest_sha256": sha256_bytes(payloads["jev/compatibility-manifest.json"]),
        "member_count": len(payloads),
        "members_note": "sha256 of every archived member except this record itself",
        "members": {path: sha256_bytes(data) for path, data in sorted(payloads.items())},
        "exclusions": {
            "rule": "only git-tracked paths are eligible; the deny-list fails closed",
            "forbidden_patterns": list(FORBIDDEN_PATTERNS),
            "forbidden_segments": list(FORBIDDEN_SEGMENTS),
            "secret_patterns": [name for name, _ in SECRET_PATTERNS],
            "synthetic_markers": list(SYNTHETIC_MARKERS),
            "synthetic_findings": exempt,
        },
    }
    return record


def file_mode(path):
    try:
        mode = Path(path).stat().st_mode & 0o777
    except OSError:
        return 0o644
    return 0o755 if mode & 0o100 else 0o644


def write_archive(archive, root, payloads):
    """Write a reproducible gzip'd tar: sorted members, zeroed metadata."""
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for path in sorted(payloads):
                    data = payloads[path]
                    info = tarfile.TarInfo(path)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = file_mode(Path(root) / path)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.type = tarfile.REGTYPE
                    tar.addfile(info, io.BytesIO(data))
    return archive


def read_archive(archive):
    members = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArtifactError(
                        f"E_ARTIFACT_MEMBER: {member.name} is not a regular file"
                    )
                handle = tar.extractfile(member)
                if handle is None:
                    raise ArtifactError(
                        f"E_ARTIFACT_MEMBER: {member.name} has no readable content"
                    )
                members[member.name] = handle.read()
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ArtifactError(f"E_ARTIFACT_UNREADABLE: {archive}: {error}") from error
    return members


def select_members(root):
    """Return the eligible payloads, refusing a forbidden or secret-bearing one."""
    payloads = {}
    refusals = []
    allowed_findings = {}
    for path in tracked_paths(root):
        if not eligible(path):
            continue
        reason = forbidden_reason(path)
        if reason is not None:
            refusals.append(
                f"E_ARTIFACT_FORBIDDEN: {path} matches {reason!r}; a credential or "
                "private state file cannot enter the candidate artifact"
            )
            continue
        try:
            data = (Path(root) / path).read_bytes()
        except OSError as error:
            refusals.append(f"E_ARTIFACT_UNREADABLE: {path}: {error}")
            continue
        refusals_found, allowed = scan_secrets(data)
        if refusals_found:
            refusals.append(
                f"E_ARTIFACT_CREDENTIAL: {path} contains secret-shaped content "
                f"matching {[name for name, _ in refusals_found]} with no synthetic "
                "marker; refusing to package it"
            )
            continue
        payloads[path] = data
        for name, marker in allowed:
            entry = allowed_findings.setdefault(
                (path, name, marker),
                {
                    "path": path,
                    "pattern": name,
                    "marker": marker,
                    "sha256": sha256_bytes(data),
                    "size_bytes": len(data),
                    "occurrences": 0,
                    "reason": (
                        "labelled fixture: the matched text carries a synthetic "
                        "marker, so it is a fake value, not a credential"
                    ),
                },
            )
            entry["occurrences"] += 1
    if refusals:
        raise ArtifactError("\n".join(refusals))
    if not payloads:
        raise ArtifactError(
            f"E_ARTIFACT_EMPTY: no tracked file under {', '.join(INCLUDE_PREFIXES)} "
            f"was found in {root}"
        )
    return payloads, [allowed_findings[key] for key in sorted(allowed_findings)]


def build(root=None, out=None):
    root = Path(root or jev_manifest.repository_root())
    manifest = jev_manifest.load_json(
        root / "jev" / "compatibility-manifest.json", "compatibility manifest"
    )
    payloads, exempt = select_members(root)
    record = release_record(root, manifest, payloads, exempt)
    payloads[RECORD_PATH] = (
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    archive = Path(out) if out else root / ".jev" / "dist" / DEFAULT_ARCHIVE_NAME
    write_archive(archive, root, payloads)
    return {
        "archive": str(archive),
        "sha256": jev_manifest.sha256_file(archive),
        "member_count": record["member_count"],
        "archived_member_count": len(payloads),
        "synthetic_finding_count": len(exempt),
        "source_revision": record["source"]["revision"],
        "host_base_commit": record["host"]["base_commit"],
        "record": record,
    }


def verify(archive, expect_revision=None):
    """Return every verification error for ``archive`` (empty means it passes)."""
    members = read_archive(archive)
    record_bytes = members.get(RECORD_PATH)
    if record_bytes is None:
        return [
            f"E_ARTIFACT_RECORD: {archive} carries no {RECORD_PATH}; the candidate "
            "artifact must pin its own contents"
        ]
    try:
        record = json.loads(record_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"E_ARTIFACT_RECORD: {RECORD_PATH} is not valid JSON: {error}"]

    errors = []
    if record.get("release_record_version") != ARTIFACT_VERSION:
        errors.append(
            f"E_ARTIFACT_RECORD: release_record_version is "
            f"{record.get('release_record_version')!r}, expected {ARTIFACT_VERSION}"
        )
    for required in REQUIRED_MEMBERS:
        if required not in members:
            errors.append(f"E_ARTIFACT_MISSING: {required} is not in the artifact")

    exclusions = record.get("exclusions") or {}
    declared = {
        (entry.get("path"), entry.get("pattern"))
        for entry in exclusions.get("synthetic_findings") or []
        if isinstance(entry, dict)
    }
    markers = tuple(exclusions.get("synthetic_markers") or ())
    actual = set()
    for path in sorted(members):
        reason = forbidden_reason(path)
        if reason is not None:
            errors.append(
                f"E_ARTIFACT_FORBIDDEN: {path} matches {reason!r}; the artifact "
                "must not carry credentials or private state"
            )
        refusals_found, allowed = scan_secrets(members[path])
        if refusals_found:
            errors.append(
                f"E_ARTIFACT_CREDENTIAL: {path} contains secret-shaped content "
                f"matching {[name for name, _ in refusals_found]} with no synthetic "
                "marker"
            )
        for name, marker in allowed:
            if not markers or marker not in markers:
                errors.append(
                    f"E_ARTIFACT_EXEMPTION: {path} allows the {name} finding on "
                    f"marker {marker!r}, which the record does not declare"
                )
            actual.add((path, name))
    undeclared = sorted(actual - declared)
    unearned = sorted(declared - actual)
    if undeclared or unearned:
        errors.append(
            "E_ARTIFACT_EXEMPTION: the record's synthetic findings do not match the "
            f"artifact; undeclared={undeclared[:5]} unearned={unearned[:5]}"
        )

    recorded = record.get("members")
    if not isinstance(recorded, dict):
        errors.append("E_ARTIFACT_RECORD: the record declares no member digests")
    else:
        expected_members = set(members) - {RECORD_PATH}
        if set(recorded) != expected_members:
            missing = sorted(expected_members - set(recorded))[:5]
            extra = sorted(set(recorded) - expected_members)[:5]
            errors.append(
                "E_ARTIFACT_DIGEST: the record pins "
                f"{len(recorded)} members but the artifact carries "
                f"{len(expected_members)}; unpinned={missing} absent={extra}"
            )
        for path in sorted(set(recorded) & expected_members):
            if recorded[path] != sha256_bytes(members[path]):
                errors.append(
                    f"E_ARTIFACT_DIGEST: {path} does not match the digest the "
                    "record pins"
                )

    manifest_bytes = members.get("jev/compatibility-manifest.json")
    if manifest_bytes is not None:
        if record.get("manifest_sha256") != sha256_bytes(manifest_bytes):
            errors.append(
                "E_ARTIFACT_MANIFEST: the record's manifest_sha256 is not the "
                "digest of the manifest the artifact carries"
            )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"E_ARTIFACT_MANIFEST: the manifest is not valid JSON: {error}")
        else:
            base = (manifest.get("host") or {}).get("base_commit")
            pinned = (record.get("host") or {}).get("base_commit")
            if base != pinned:
                errors.append(
                    f"E_ARTIFACT_MANIFEST: the record pins host base {pinned!r} but "
                    f"the manifest it carries pins {base!r}"
                )
            repository = (manifest.get("integration") or {}).get("repository")
            recorded_repository = (record.get("source") or {}).get("repository")
            if repository != recorded_repository:
                errors.append(
                    f"E_ARTIFACT_MANIFEST: the record names source "
                    f"{recorded_repository!r} but the manifest it carries names "
                    f"{repository!r}"
                )

    if expect_revision is not None:
        actual = (record.get("source") or {}).get("revision")
        if actual != expect_revision:
            errors.append(
                f"E_ARTIFACT_REVISION: the artifact was built from {actual!r}, "
                f"not the expected {expect_revision!r}"
            )
    return errors


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser("build", help="Build the candidate artifact.")
    build_parser.add_argument("--root", default=None)
    build_parser.add_argument("--out", default=None)
    build_parser.add_argument(
        "--record-out", default=None, help="Also write the release record here."
    )
    build_parser.add_argument("--json", action="store_true")

    verify_parser = sub.add_parser("verify", help="Verify a candidate artifact.")
    verify_parser.add_argument("archive")
    verify_parser.add_argument("--expect-revision", default=None)
    verify_parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "build":
        try:
            result = build(args.root, args.out)
        except (ArtifactError, jev_manifest.ManifestError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if args.record_out:
            out = Path(args.record_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(result["record"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        summary = {key: value for key, value in result.items() if key != "record"}
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(
                f"built {result['archive']}\n"
                f"  sha256        {result['sha256']}\n"
                f"  members       {result['member_count']} pinned "
                f"({result['archived_member_count']} archived with the record)\n"
                f"  synthetic     {result['synthetic_finding_count']} labelled findings\n"
                f"  revision      {result['source_revision']}\n"
                f"  host pin      {result['host_base_commit']}"
            )
        return 0
    try:
        errors = verify(args.archive, args.expect_revision)
    except ArtifactError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"archive": args.archive, "errors": errors}, indent=2))
    elif errors:
        for error in errors:
            print(error, file=sys.stderr)
    else:
        print(f"ok: {args.archive} verifies against its own record")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
