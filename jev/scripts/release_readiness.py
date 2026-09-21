#!/usr/bin/env python3
"""Packaging, upgrade, and rollback release workflow for phase 6.3 (#26).

Phase 6.3 asks for a packaged release workflow: manifest verification, build and
profile instructions, feature controls, diagnostics, install/upgrade order, state
ownership, backups, rollback, separate component maintenance, a validated
platform matrix with known limitations, and **release gates plus a candidate
artifact that excludes credentials and captured private data**.

The point of this module is that release readiness follows *actual evidence*
rather than issue completion. Every gate is re-derived here, at run time, from
this checkout or from a recorded run; nothing is satisfiable by asserting that a
tracking issue is closed. A gate therefore carries:

``status``    ``pass`` - the check ran here and held.
              ``fail`` - the check ran here and did not hold.
              ``not-run`` - the evidence does not exist yet (reported, never
              mistaken for a pass).
``evidence``  ``verified-here`` - re-derived in this process.
              ``recorded`` - a checked-in run record, with its revision and tier.
              ``claimed`` - an assertion with no run behind it. This value exists
              only so a claim can be *rejected*; no gate passes as ``claimed``.

``release_ready`` is true only when every gate passes, so a partially validated
matrix produces a candidate artifact and a ``release_ready: false`` verdict with
the missing platforms named as blockers. That is the intended failure mode.

Evidence tiers
--------------
The tier vocabulary is the one the phase-6.1/6.2 harnesses established, and the
candidate report keeps it per gate: ``offline-fixture`` (checked-in inputs and
shipped modules), ``bus-stage-stub`` / ``component-stub``, ``real-component``,
``real-host-binary`` (a launched host process against a loopback mock), and
``live-provider`` (never run by this phase: paid inference is outside the
authorization and needs explicit consent and a positive budget).

What the candidate artifact excludes
------------------------------------
The inclusion set is declared, the exclusion set is enforced twice - once on the
path and once on the bytes - and a refusal is fail-closed: a candidate with a
refusal is not written, and the refusal names the rule and the file. The rules
cover the isolated environment and its state, private captures and logs,
credential files, and credential-shaped content (provider keys, private keys,
GitHub and AWS tokens, bearer headers, and secret-shaped assignments).

Usage
-----
    python3 jev/scripts/release_readiness.py gates --json /tmp/readiness.json
    python3 jev/scripts/release_readiness.py candidate --out /tmp/jev-6.3.tar.gz
    python3 jev/scripts/release_readiness.py candidate --json /tmp/candidate.json

Exit codes: 0 the checks held (``release_ready`` may still be false when evidence
is missing), 1 a check failed or the candidate was refused, 2 the inputs were
unusable.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_provenance  # noqa: E402
import isolated_env  # noqa: E402
import jev_manifest  # noqa: E402

SCHEMA = "jev-release-readiness.v1"
CANDIDATE_SCHEMA = "jev-release-candidate.v1"

SUPPORTED_PROFILE = "integrated-offline"
SAFE_PROFILE = "isolated-offline"
PINNED_BINARY = "codex-rs/target/debug/codex"
PLATFORM_EVIDENCE = "jev/evidence/platform-matrix.json"

TIER_OFFLINE = "offline-fixture"
TIER_REAL_HOST = "real-host-binary"
TIER_LIVE = "live-provider"

#: The declared inclusion set. Paths are repository-relative; ``tree`` walks a
#: directory, ``glob`` matches a pattern, and ``file`` is a single path.
INCLUDE_RULES = (
    ("tree", "jev"),
    ("glob", ".github/scripts/test_jev_*.py"),
    ("file", "codex-rs/core/src/jev_bus.rs"),
)

DENY_PATH_SEGMENTS = {
    ".git",
    ".jev",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "superseded",
    "target",
    "venv",
}

#: Checked-in fixtures whose subject *is* a credential shape, so the content
#: rules would refuse them. They are synthetic placeholders in the repository,
#: not captures, and each entry has to declare why. `.github/scripts/test_jev_release.py`
#: pins this mapping to exactly these four paths, so a fifth exemption cannot be
#: added without review, and the candidate manifest lists every one applied.
SYNTHETIC_FIXTURE_EXEMPTIONS = {
    ".github/scripts/test_jev_capture.py": (
        "synthetic placeholder key in the capture-redaction assertion"
    ),
    ".github/scripts/test_jev_screening.py": (
        "synthetic placeholder leak constant for the screening assertion"
    ),
    "jev/tests/capture_fixtures/04-secret-bearing.jsonl": (
        "synthetic, labelled redaction fixture: placeholder key, token, and bearer header"
    ),
    "jev/tests/capture_fixtures/README.md": (
        "synthetic placeholder credential shapes documented for the redaction fixture"
    ),
}

DENY_NAME_PATTERNS = (
    re.compile(r"^\.env(\.|$)"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.key$"),
    re.compile(r"\.log$"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)"),
    re.compile(r"(^|[-_.])credential"),
    re.compile(r"^auth\.json$"),
    re.compile(r"^hosts\.ya?ml$"),
    re.compile(r"^\.netrc$"),
)

#: Content rules are fail-closed: a match refuses the whole candidate rather
#: than silently dropping a file, so a reviewer sees what was excluded and why.
CONTENT_RULES = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----")),
    ("provider_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("provider_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    (
        "scm_token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("cloud_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("chat_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("bearer_header", re.compile(r"(?i)authorization:\s*bearer\s+\S")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|passphrase)\b['\"]?\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
)


class ReadinessError(Exception):
    """Unusable inputs: a missing checkout, manifest, or evidence file."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "macos-aarch64"
    if system == "windows" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    return f"{system}-{machine}"


def load_manifest(repo_root: Path) -> dict:
    return jev_manifest.load_json(
        repo_root / "jev" / "compatibility-manifest.json", "compatibility manifest"
    )


def load_profile(repo_root: Path, profile_id: str) -> dict:
    return jev_manifest.load_json(
        repo_root / "jev" / "profiles" / f"{profile_id}.json", f"profile {profile_id}"
    )


def _gate(gate_id, requirement, status, evidence, tier=None, detail=None):
    return {
        "id": gate_id,
        "requirement": requirement,
        "status": status,
        "evidence": evidence,
        "tier": tier,
        "detail": detail,
    }


# --------------------------------------------------------------------------
# Candidate artifact
# --------------------------------------------------------------------------


def included_paths(repo_root: Path) -> list[str]:
    """Resolve the declared inclusion set to sorted repository-relative paths."""
    found: set[str] = set()
    for kind, value in INCLUDE_RULES:
        if kind == "file":
            if (repo_root / value).is_file():
                found.add(value)
        elif kind == "tree":
            base = repo_root / value
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_file():
                        found.add(path.relative_to(repo_root).as_posix())
        elif kind == "glob":
            for path in repo_root.glob(value):
                if path.is_file():
                    found.add(path.relative_to(repo_root).as_posix())
        else:  # pragma: no cover - a typo in INCLUDE_RULES
            raise ReadinessError(f"unknown include rule kind {kind!r}")
    return sorted(found)


def _path_exclusion(relative: str) -> tuple[str, str] | None:
    """Classify a path rule hit as ``build-artifact`` (benign) or ``sensitive``."""
    parts = relative.split("/")
    for part in parts[:-1]:
        if part in DENY_PATH_SEGMENTS:
            return "build-artifact", f"segment:{part}"
    name = parts[-1]
    if name in DENY_PATH_SEGMENTS:
        return "build-artifact", f"segment:{name}"
    for pattern in DENY_NAME_PATTERNS:
        if pattern.search(name):
            return "sensitive", f"name:{pattern.pattern}"
    return None


def _content_refusals(relative: str, data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    refusals = []
    for rule, pattern in CONTENT_RULES:
        match = pattern.search(text)
        if match:
            refusals.append(
                {
                    "path": relative,
                    "code": f"E_CANDIDATE_CONTENT:{rule}",
                    "detail": f"matched {pattern.pattern!r} at offset {match.start()}",
                }
            )
    return refusals


def collect_candidate(repo_root: Path) -> dict:
    """Scan the inclusion set; returns entries, exclusions, refusals, exemptions."""
    entries: list[dict] = []
    refusals: list[dict] = []
    excluded: list[dict] = []
    denied: list[dict] = []
    exemptions: list[dict] = []
    total_bytes = 0
    for relative in included_paths(repo_root):
        path = repo_root / relative
        verdict = _path_exclusion(relative)
        if verdict is not None:
            kind, code = verdict
            record = {"path": relative, "code": code}
            if kind == "build-artifact":
                excluded.append(record)
            else:
                denied.append(record)
            continue
        data = path.read_bytes()
        content_refusals = _content_refusals(relative, data)
        if content_refusals:
            reason = SYNTHETIC_FIXTURE_EXEMPTIONS.get(relative)
            if reason:
                exemptions.append(
                    {
                        "path": relative,
                        "reason": reason,
                        "codes": sorted({r["code"] for r in content_refusals}),
                    }
                )
            else:
                refusals.extend(content_refusals)
            continue
        total_bytes += len(data)
        entries.append(
            {
                "path": relative,
                "sha256": sha256_bytes(data),
                "bytes": len(data),
            }
        )
    digest_source = json.dumps(
        [
            {"path": e["path"], "sha256": e["sha256"], "bytes": e["bytes"]}
            for e in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "included": entries,
        "included_count": len(entries),
        "total_bytes": total_bytes,
        "excluded": excluded,
        "denied": denied,
        "exemptions": exemptions,
        "refusals": refusals,
        "bundle_digest": sha256_bytes(digest_source),
    }


def write_archive(out_path: Path, repo_root: Path, entries: list[dict]) -> dict:
    """Write a reproducible tar.gz: sorted members, zeroed metadata."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stage = out_path.with_name(out_path.name + ".partial")
    with stage.open("wb") as raw:
        # ``filename=""`` keeps the gzip header free of the member name, so the
        # archive digest does not depend on where the operator wrote it.
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT
            ) as tar:
                for entry in entries:
                    info = tarfile.TarInfo(entry["path"])
                    info.size = entry["bytes"]
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with (repo_root / entry["path"]).open("rb") as handle:
                        tar.addfile(info, handle)
    stage.replace(out_path)
    data = out_path.read_bytes()
    return {"path": str(out_path), "bytes": len(data), "sha256": sha256_bytes(data)}


def build_candidate(repo_root: Path, out_path: Path | None = None) -> dict:
    """Build the candidate document, and the archive when no refusal is found."""
    scan = collect_candidate(repo_root)
    report = {
        "schema": CANDIDATE_SCHEMA,
        "inclusion_rules": [
            {"kind": kind, "value": value} for kind, value in INCLUDE_RULES
        ],
        "deny_path_segments": sorted(DENY_PATH_SEGMENTS),
        "deny_name_patterns": sorted(p.pattern for p in DENY_NAME_PATTERNS),
        "content_rules": sorted(rule for rule, _ in CONTENT_RULES),
        "declared_exemptions": dict(sorted(SYNTHETIC_FIXTURE_EXEMPTIONS.items())),
        "included": scan["included"],
        "included_count": scan["included_count"],
        "total_bytes": scan["total_bytes"],
        "excluded": scan["excluded"],
        "denied": scan["denied"],
        "exemptions": scan["exemptions"],
        "refusals": scan["refusals"],
        "bundle_digest": scan["bundle_digest"],
        "archive": None,
    }
    if scan["refusals"]:
        report["built"] = False
        return report
    if out_path is not None:
        report["archive"] = write_archive(out_path, repo_root, scan["included"])
    report["built"] = True
    return report


# --------------------------------------------------------------------------
# Release gates
# --------------------------------------------------------------------------


def _status(errors, ok_detail, fail_detail):
    if errors:
        return "fail", fail_detail.format(errors=errors)
    return "pass", ok_detail


def evaluate_manifest_gates(repo_root: Path, manifest: dict) -> list[dict]:
    gates = []
    errors = jev_manifest.validate_manifest(manifest, repo_root=repo_root)
    for profile_id in (SUPPORTED_PROFILE, SAFE_PROFILE, "baseline"):
        profile = load_profile(repo_root, profile_id)
        errors += jev_manifest.validate_manifest(
            manifest, repo_root=repo_root, profile=profile
        )
    status, detail = _status(
        errors,
        "manifest, pins, and profiles validate with no error code",
        "validation errors: {errors}",
    )
    gates.append(
        _gate(
            "manifest.pins",
            "The manifest, its pins, and every declared profile validate.",
            status,
            "verified-here",
            TIER_OFFLINE,
            detail,
        )
    )

    applied = jev_manifest.check_patch_state(manifest, repo_root, "applied")
    status, detail = _status(
        applied,
        "every manifest patch is applied to this checkout",
        "patch-state applied errors: {errors}",
    )
    gates.append(
        _gate(
            "host.patch.applied",
            "The pinned patches are applied, so the release tree is the integrated tree.",
            status,
            "verified-here",
            TIER_OFFLINE,
            detail,
        )
    )

    # The rollback direction has to *discriminate*: an operator only trusts
    # `--patch-state absent` if it reports the applied tree as still patched.
    absent = jev_manifest.check_patch_state(manifest, repo_root, "absent")
    status = "pass" if absent else "fail"
    gates.append(
        _gate(
            "host.patch.rollback-detected",
            "The rollback check reports this patched tree as patched instead of passing vacuously.",
            status,
            "verified-here",
            TIER_OFFLINE,
            (
                f"`--patch-state absent` reports {len(absent)} error(s) on the "
                "integrated tree, so it can detect a failed rollback"
                if absent
                else "`--patch-state absent` passed on a patched tree: the check does not discriminate"
            ),
        )
    )

    adapter = jev_manifest.check_native_adapter(manifest, repo_root, "applied")
    status, detail = _status(
        adapter,
        "the declared native approval adapter is installed and wired",
        "native-adapter applied errors: {errors}",
    )
    gates.append(
        _gate(
            "host.adapter.applied",
            "The native approval adapter is installed at the declared anchors.",
            status,
            "verified-here",
            TIER_OFFLINE,
            detail,
        )
    )

    adapter_absent = jev_manifest.check_native_adapter(manifest, repo_root, "absent")
    status = "pass" if adapter_absent else "fail"
    gates.append(
        _gate(
            "host.adapter.rollback-detected",
            "The adapter-removal check detects the installed adapter instead of passing vacuously.",
            status,
            "verified-here",
            TIER_OFFLINE,
            (
                f"`--native-adapter absent` reports {len(adapter_absent)} error(s) on the "
                "integrated tree, so it can detect an incomplete removal"
                if adapter_absent
                else "`--native-adapter absent` passed with the adapter installed"
            ),
        )
    )
    return gates


def evaluate_control_gates(repo_root: Path, manifest: dict) -> list[dict]:
    gates = []
    enforcement = jev_manifest.check_approval_enforcement(manifest, "disabled")
    status, detail = _status(
        enforcement,
        "every enforcement switch still defaults to false",
        "approval-enforcement errors: {errors}",
    )
    gates.append(
        _gate(
            "approval.enforcement.disabled",
            "Approval enforcement is declared but still disabled by default.",
            status,
            "verified-here",
            TIER_OFFLINE,
            detail,
        )
    )

    credential = manifest["credentials"]["remote_inference"]
    feature = manifest["features"].get("remote_inference.enabled", {})
    problems = []
    if credential.get("consent") is not False:
        problems.append(
            f"credentials.remote_inference.consent is {credential.get('consent')!r}"
        )
    if credential.get("budget_usd_max") is not None:
        problems.append(f"budget_usd_max is {credential.get('budget_usd_max')!r}")
    if feature.get("default") is not False:
        problems.append(
            f"remote_inference.enabled defaults to {feature.get('default')!r}"
        )
    safe_features = jev_manifest.effective_features(
        manifest, load_profile(repo_root, SAFE_PROFILE)
    )
    if safe_features.get("remote_inference.enabled"):
        problems.append(
            "the isolated-offline profile resolves remote inference to enabled"
        )
    status = "pass" if not problems else "fail"
    gates.append(
        _gate(
            "remote_inference.disabled",
            "Remote inference stays disabled with no consent and no budget.",
            status,
            "verified-here",
            TIER_OFFLINE,
            (
                "consent=false, budget=null, default=false, and the isolated profile keeps it off"
                if not problems
                else "; ".join(problems)
            ),
        )
    )
    return gates


def evaluate_isolated_roundtrip(repo_root: Path, scratch: Path) -> dict:
    """Create a fresh isolated environment twice, then roll both back."""
    binary = scratch / "pinned-codex-stub"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"jev-release-gate-pinned-binary\n")
    binary.chmod(0o755)
    # The validated configuration is a profile plus a host pin; record the
    # digest of every declared profile so the round trip pins the whole set
    # rather than only the profile the isolated environment happens to use.
    profile_digests = {
        path.stem: jev_manifest.sha256_file(path)
        for path in sorted((repo_root / "jev" / "profiles").glob("*.json"))
    }
    ambient_before = isolated_env.ambient_home_fingerprint()
    plans = []
    rollbacks = []
    for index in (1, 2):
        env_dir = scratch / f"isolated-{index}" / ".jev" / "isolated"
        plan = isolated_env.init_env(
            env_dir=env_dir,
            profile_id=SAFE_PROFILE,
            root=repo_root,
            binary=binary,
        )
        plans.append(
            {
                "profile": plan["profile"],
                "profile_digest": plan["profile_digest"],
                "host_commit": plan["host_commit"],
                "features": plan["features"],
                "switch_env": plan["switch_env"],
                "binary_sha256": plan["binary_sha256"],
                "config_sha256": jev_manifest.sha256_file(Path(plan["config"])),
                "optional_features_enabled": plan["optional_features_enabled"],
            }
        )
        status = isolated_env.status(env_dir=env_dir)
        moved = isolated_env.rollback(
            env_dir=env_dir,
            stamp=f"20260101T00000{index}Z",
            target_root=scratch / f"isolated-{index}" / ".jev" / "superseded",
        )
        restored = json.loads(
            (Path(moved["moved_to"]) / "isolated-env.json").read_text(encoding="utf-8")
        )
        rollbacks.append(
            {
                "env_dir": moved["env_dir"],
                "moved_to": moved["moved_to"],
                "state": moved["state"],
                "source_absent": not Path(moved["env_dir"]).exists(),
                "plan_preserved": restored["profile"] == plan["profile"],
                "binary_matched_plan": status["binary_matches_plan"],
                "optional_features_enabled": status["optional_features_enabled"],
                "remote_inference_enabled": status["remote_inference_enabled"],
            }
        )
    ambient_after = isolated_env.ambient_home_fingerprint()
    reproducible = plans[0] == plans[1]
    rolled_back = all(
        record["state"] == "moved"
        and record["source_absent"]
        and record["plan_preserved"]
        and record["binary_matched_plan"]
        and not record["optional_features_enabled"]
        and not record["remote_inference_enabled"]
        for record in rollbacks
    )
    untouched = ambient_before == ambient_after
    if reproducible and rolled_back and untouched:
        status_value, detail = (
            "pass",
            (
                "two fresh environments resolved to the same plan digest and rolled "
                "back to a preserved record; the ambient home fingerprint is unchanged"
            ),
        )
    else:
        status_value, detail = (
            "fail",
            (
                f"reproducible={reproducible} rolled_back={rolled_back} ambient_untouched={untouched}"
            ),
        )
    return {
        "gate": _gate(
            "isolated.roundtrip",
            "A fresh isolated setup reproduces the validated configuration and rolls back.",
            status_value,
            "verified-here",
            TIER_OFFLINE,
            f"{detail}; profile digests: {len(profile_digests)}",
        ),
        "plan": plans[0],
        "runs": rollbacks,
        "profile_digests": profile_digests,
    }


def evaluate_candidate_gate(repo_root: Path) -> dict:
    candidate = collect_candidate(repo_root)
    problems = []
    if candidate["included_count"] == 0:
        problems.append("the inclusion set resolved to no files")
    if candidate["refusals"]:
        problems.append(f"{len(candidate['refusals'])} content refusal(s)")
    if candidate["denied"]:
        problems.append(
            f"{len(candidate['denied'])} credential-path(s) inside the inclusion set"
        )
    status = "pass" if not problems else "fail"
    exempted = ", ".join(entry["path"] for entry in candidate["exemptions"]) or "none"
    return {
        "gate": _gate(
            "candidate.exclusions",
            "The candidate artifact excludes credentials and captured private data.",
            status,
            "verified-here",
            TIER_OFFLINE,
            (
                f"{candidate['included_count']} files scanned with no refusal and no "
                f"credential path; bundle digest {candidate['bundle_digest'][:16]}; "
                f"credential-shaped synthetic fixtures withheld: {exempted}"
                if not problems
                else "; ".join(problems)
            ),
        ),
        "bundle_digest": candidate["bundle_digest"],
        "included_count": candidate["included_count"],
        "excluded": candidate["excluded"],
        "denied": candidate["denied"],
        "exemptions": candidate["exemptions"],
        "refusals": candidate["refusals"],
    }


def load_platform_evidence(repo_root: Path) -> dict | None:
    path = repo_root / PLATFORM_EVIDENCE
    if not path.is_file():
        return None
    return jev_manifest.load_json(path, "platform matrix")


#: The inputs whose change would invalidate a recorded platform run. Evidence
#: documents and this module's own report are excluded, so recording a run does
#: not invalidate the run.
PINNED_INPUT_RULES = (
    ("file", "jev/compatibility-manifest.json"),
    ("file", "codex-rs/core/src/jev_bus.rs"),
    ("glob", "jev/profiles/*.json"),
    ("glob", "jev/patches/*.patch"),
    ("glob", "jev/scripts/*.py"),
    ("glob", "jev/smoke/*.sh"),
    ("glob", "jev/smoke/*.py"),
)

#: The host smokes that make up a `real-host-binary` platform record.
SMOKE_HARNESSES = (
    ("plaintext-collaboration", "run-plaintext-smoke.sh"),
    ("projection-reset", "run-projection-reset-smoke.sh"),
)


def derive_pinned_inputs(repo_root: Path) -> dict:
    """Digest the behavioral inputs a platform run depends on."""
    found: set[str] = set()
    for kind, value in PINNED_INPUT_RULES:
        if kind == "file":
            if (repo_root / value).is_file():
                found.add(value)
        else:
            for path in repo_root.glob(value):
                if path.is_file():
                    found.add(path.relative_to(repo_root).as_posix())
    entries = [
        {"path": relative, "sha256": jev_manifest.sha256_file(repo_root / relative)}
        for relative in sorted(found)
    ]
    digest = sha256_bytes(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {"digest": digest, "files": entries}


def revision_reachable(repo_root: Path, revision) -> bool | None:
    """Is ``revision`` present and an ancestor of HEAD?

    ``None`` means the revision is not resolvable in this clone, so the question
    cannot be answered here. ``None`` is **not** a verification: callers must
    credit a record only on ``True`` and refuse it on anything else, so a shallow
    or invented revision can never inherit a pass.
    """
    if not revision:
        return False
    try:
        known = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if known.returncode != 0:
            return None
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "merge-base",
                "--is-ancestor",
                revision,
                "HEAD",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return ancestor.returncode == 0


def classify_platform_record(
    record: dict, current_inputs: str, reachable: bool | None
) -> tuple[str, str]:
    """Decide one platform record's row status from its evidence alone.

    Returns ``(status, reason)``; only ``verified`` is evidence a release may
    credit. ``reachable`` is the outcome of :func:`revision_reachable` for the
    record's revision, and anything other than ``True`` refuses the record: a
    revision that is not an ancestor of HEAD is ``stale``, and one that is not
    resolvable in this clone is ``unverifiable`` - which is not a pass.
    """
    harness = record.get("harness") or {}
    claims_live = any(
        isinstance(entry, dict) and entry.get("tier") == TIER_LIVE
        for entry in harness.values()
    )
    if not harness or claims_live:
        return "fail", "claims a live-provider tier or records no harness result"
    if record.get("pinned_inputs_digest") != current_inputs:
        return "stale", "the record predates the current pinned inputs"
    if reachable is False:
        return "stale", "the recorded revision is not an ancestor of HEAD"
    if reachable is None:
        return (
            "unverifiable",
            "the recorded revision is not resolvable in this clone; fetch the "
            "object or re-record the run before release",
        )
    ok = bool(record.get("revision")) and all(
        isinstance(entry, dict) and entry.get("ok") is True
        for entry in harness.values()
    )
    if ok:
        return "verified", "revision reachable, pinned inputs current, harness ok"
    return "fail", "the record is missing a revision or a harness result is not ok"


def evaluate_platform_gate(repo_root: Path, manifest: dict) -> dict:
    supported = manifest["host"]["platforms"]["supported"]
    reference = manifest["host"]["platforms"]["reference"]
    here = current_platform()
    evidence = load_platform_evidence(repo_root)
    current_inputs = derive_pinned_inputs(repo_root)["digest"]
    rows: list[dict] = []
    blockers: list[str] = []
    malformed: list[str] = []
    stale: list[str] = []
    unverifiable: list[str] = []
    verified: list[str] = []
    for name in supported:
        record = None
        if evidence and isinstance(evidence.get("platforms"), dict):
            record = evidence["platforms"].get(name)
        if record is None:
            rows.append(
                {
                    "platform": name,
                    "status": "not-run",
                    "tier": None,
                    "harness": [],
                    "revision": None,
                    "binary_sha256": None,
                }
            )
            blockers.append(name)
            continue
        harness = record.get("harness") or {}
        row = {
            "platform": name,
            "tier": TIER_REAL_HOST,
            "harness": sorted(harness),
            "revision": record.get("revision"),
            "binary_sha256": record.get("binary_sha256"),
        }
        status, reason = classify_platform_record(
            record,
            current_inputs,
            revision_reachable(repo_root, record.get("revision")),
        )
        rows.append({**row, "status": status, "reason": reason})
        if status == "verified":
            verified.append(name)
            continue
        blockers.append(name)
        if status == "stale":
            stale.append(name)
        elif status == "unverifiable":
            unverifiable.append(name)
        else:
            malformed.append(name)
    if malformed:
        status = "fail"
        detail = "; ".join(
            f"{name} claims a live-provider tier or records no harness result"
            for name in malformed
        )
    elif stale:
        status = "not-run"
        detail = (
            "the recorded run for "
            f"{', '.join(stale)} is stale (pinned inputs changed or the revision "
            "is not an ancestor of HEAD); re-record it before release"
        )
    elif unverifiable:
        status = "not-run"
        detail = (
            "the recorded revision for "
            f"{', '.join(unverifiable)} is not resolvable in this clone; fetch the "
            "object or re-record the run before release"
        )
    elif blockers:
        status = "not-run"
        detail = (
            f"the reference platform is {reference} and this host is {here}; "
            f"no current recorded real-host run exists for {', '.join(blockers)}"
        )
    else:
        status, detail = "pass", "every supported platform has a current real-host run"
    return {
        "gate": _gate(
            "platform.matrix",
            "Every supported platform has current recorded real-host evidence.",
            status,
            "recorded" if evidence else "not-run",
            TIER_REAL_HOST,
            detail,
        ),
        "platforms": rows,
        "current_host": here,
        "pinned_inputs_digest": current_inputs,
        "blockers": sorted(set(blockers)),
    }


def record_platform(
    repo_root: Path,
    codex_binary: Path,
    kit: Path | None = None,
    scratch: Path | None = None,
    timeout: int | None = None,
) -> dict:
    """Run the host smokes for this platform and record the result.

    This is the only way a platform becomes ``verified``: the record carries the
    binary digest, the repository revision, and a digest of the pinned inputs the
    run depended on, so a later change to those inputs makes the record stale
    rather than silently inheriting a pass.
    """
    repo_root = Path(repo_root).resolve()
    binary = Path(codex_binary).resolve()
    if not binary.is_file():
        raise ReadinessError(f"codex binary not found: {binary}")
    scratch = (
        Path(scratch)
        if scratch
        else Path(tempfile.mkdtemp(prefix="jev-platform-record-"))
    )
    scratch.mkdir(parents=True, exist_ok=True)
    harness: dict[str, dict] = {}
    for name, script in SMOKE_HARNESSES:
        workdir = scratch / name
        argv = [
            "bash",
            str(repo_root / "jev" / "smoke" / script),
            "--codex",
            str(binary),
            "--workdir",
            str(workdir),
            # The harness deletes its scratch unless the verdict is kept, and the
            # verdict is the record's evidence.
            "--keep",
        ]
        if kit is not None and name == "projection-reset":
            argv += ["--kit", str(kit)]
        if timeout is not None:
            argv += ["--timeout", str(timeout)]
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, cwd=str(repo_root)
        )
        verdict_path = workdir / "verdict.json"
        verdict: dict = {}
        if verdict_path.is_file():
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        harness[name] = {
            "ok": completed.returncode == 0 and bool(verdict.get("ok")),
            "exit_code": completed.returncode,
            "tier": TIER_REAL_HOST,
        }
    binary_text = binary.as_posix()
    return {
        "platform": current_platform(),
        "tier": TIER_REAL_HOST,
        "revision": build_provenance.git_revision(repo_root),
        "rust_toolchain": load_manifest(repo_root)["host"]["rust_toolchain"],
        "binary_kind": "release" if "/release/" in binary_text else "debug",
        "binary_sha256": jev_manifest.sha256_file(binary),
        "harness": harness,
        "pinned_inputs_digest": derive_pinned_inputs(repo_root)["digest"],
        "recorded_at_unix_ms": int(time.time() * 1000),
    }


def write_platform_evidence(
    repo_root: Path, record: dict, path: Path | None = None
) -> Path:
    target = Path(path) if path else Path(repo_root) / PLATFORM_EVIDENCE
    if target.is_file():
        document = jev_manifest.load_json(target, "platform matrix")
    else:
        document = {
            "schema": "jev-platform-matrix.v1",
            "note": (
                "Real-host-binary runs recorded by "
                "`jev/scripts/release_readiness.py record-platform`. A record is "
                "evidence for the exact binary digest, revision, and pinned inputs it "
                "names; the release gate treats it as stale once those inputs change."
            ),
            "platforms": {},
        }
    document["platforms"][record["platform"]] = record
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


GATE_STATUSES = ("pass", "fail", "not-run")
#: ``not-run`` is an evidence kind as well as a status: a gate whose evidence
#: does not exist yet (a skipped round trip, an absent platform record) reports
#: it here rather than dropping out of the conjunction (issue #98), so it must
#: be in the vocabulary the gate tests check against.
GATE_EVIDENCE = ("verified-here", "recorded", "claimed", "not-run")


def summarize_gates(gates: list[dict]) -> dict:
    """Normalize the gate list, then decide readiness.

    A ``claimed`` assertion is never a pass: it is rewritten to ``fail`` so an
    issue that is merely closed cannot move a gate.
    """
    normalized = []
    for gate in gates:
        if gate["status"] == "pass" and gate["evidence"] not in (
            "verified-here",
            "recorded",
        ):
            normalized.append(
                {
                    **gate,
                    "status": "fail",
                    "detail": (
                        f"{gate['evidence']} evidence is not evidence: {gate['detail']}"
                    ),
                }
            )
            continue
        normalized.append(dict(gate))
    failed = [gate["id"] for gate in normalized if gate["status"] == "fail"]
    not_run = [gate["id"] for gate in normalized if gate["status"] == "not-run"]
    return {
        "gates": normalized,
        "failed": failed,
        "not_run": not_run,
        "release_ready": not failed and not not_run,
    }


def evaluate_gates(
    repo_root: Path,
    scratch: Path | None = None,
    run_roundtrip: bool = True,
) -> dict:
    """Derive every release gate from this checkout and from recorded runs."""
    repo_root = Path(repo_root).resolve()
    manifest = load_manifest(repo_root)
    gates = evaluate_manifest_gates(repo_root, manifest)
    gates += evaluate_control_gates(repo_root, manifest)

    roundtrip_record = None
    if run_roundtrip:
        owned_scratch = scratch is None
        scratch = (
            Path(scratch)
            if scratch
            else Path(tempfile.mkdtemp(prefix="jev-release-gate-"))
        )
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            roundtrip_record = evaluate_isolated_roundtrip(repo_root, scratch)
        finally:
            if owned_scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        gates.append(roundtrip_record["gate"])
    else:
        # A skipped round trip is not a pass and not an omission: it is emitted
        # as `not-run`, so it blocks `release_ready` instead of disappearing from
        # the conjunction (issue #98). The flag's own contract is "not-run".
        gates.append(
            _gate(
                "isolated.roundtrip",
                "A fresh isolated setup reproduces the validated configuration and rolls back.",
                "not-run",
                "not-run",
                TIER_OFFLINE,
                "the round trip was skipped (--skip-roundtrip); run it before crediting release readiness",
            )
        )

    candidate_record = evaluate_candidate_gate(repo_root)
    gates.append(candidate_record["gate"])
    platform_record = evaluate_platform_gate(repo_root, manifest)
    gates.append(platform_record["gate"])

    outcome = summarize_gates(gates)
    gates = outcome["gates"]
    document = {
        "schema": SCHEMA,
        "evaluated_at_unix_ms": int(time.time() * 1000),
        "repo_root": str(repo_root),
        "revision": build_provenance.git_revision(repo_root),
        "host_pin": manifest["host"]["base_commit"],
        "rust_toolchain": manifest["host"]["rust_toolchain"],
        "python_requirement": manifest["host"]["python_requirement"],
        "supported_profile": SUPPORTED_PROFILE,
        "safe_profile": SAFE_PROFILE,
        "pinned_binary": PINNED_BINARY,
        "gates": gates,
        "failed": outcome["failed"],
        "not_run": outcome["not_run"],
        "release_ready": outcome["release_ready"],
        "blocking": (
            sorted(
                set(platform_record["blockers"])
                | set(outcome["failed"])
                | set(outcome["not_run"])
            )
            if not outcome["release_ready"]
            else []
        ),
        "platforms": platform_record["platforms"],
        "current_host": platform_record["current_host"],
        "candidate_bundle_digest": candidate_record["bundle_digest"],
        "isolated_roundtrip": (
            {
                "plan": roundtrip_record["plan"],
                "runs": roundtrip_record["runs"],
                "profile_digests": roundtrip_record["profile_digests"],
            }
            if roundtrip_record
            else None
        ),
    }
    return document


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _emit(document, json_path, quiet, summary):
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not quiet:
        print(summary)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Release gates and the candidate artifact for the JEV integration."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gates = sub.add_parser(
        "gates", help="Derive every release gate from real evidence."
    )
    gates.add_argument("--root", default=None, help="integration host checkout root")
    gates.add_argument(
        "--scratch", default=None, help="scratch root for the isolated round trip"
    )
    gates.add_argument(
        "--skip-roundtrip",
        action="store_true",
        help="skip the isolated create/rollback round trip (it is then not-run)",
    )
    gates.add_argument("--json", default="", help="write the readiness document here")
    gates.add_argument("--quiet", action="store_true")

    candidate = sub.add_parser(
        "candidate", help="Build the candidate artifact and its integrity manifest."
    )
    candidate.add_argument(
        "--root", default=None, help="integration host checkout root"
    )
    candidate.add_argument("--out", default="", help="write the .tar.gz candidate here")
    candidate.add_argument("--json", default="", help="write the manifest here")
    candidate.add_argument("--quiet", action="store_true")

    record = sub.add_parser(
        "record-platform",
        help="Run the host smokes and record a real-host platform entry.",
    )
    record.add_argument("--root", default=None, help="integration host checkout root")
    record.add_argument("--codex", required=True, help="codex binary to exercise")
    record.add_argument("--kit", default=None, help="pinned jev-prune-kit checkout")
    record.add_argument("--scratch", default=None, help="scratch root for the runs")
    record.add_argument("--timeout", type=int, default=None)
    record.add_argument(
        "--evidence", default=None, help=f"record path (default: {PLATFORM_EVIDENCE})"
    )
    record.add_argument("--json", default="", help="also write the record here")
    record.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    repo_root = (
        Path(args.root).resolve() if args.root else jev_manifest.repository_root()
    )
    try:
        if args.command == "gates":
            document = evaluate_gates(
                repo_root,
                scratch=Path(args.scratch).resolve() if args.scratch else None,
                run_roundtrip=not args.skip_roundtrip,
            )
            summary = (
                f"release_ready={str(document['release_ready']).lower()} "
                f"gates={len(document['gates'])} "
                f"failed={len(document['failed'])} not_run={len(document['not_run'])} "
                f"revision={document['revision']}"
            )
            if document["blocking"]:
                summary += f" blocking={','.join(document['blocking'])}"
            _emit(document, args.json, args.quiet, summary)
            return 0 if not document["failed"] else 1

        if args.command == "record-platform":
            record = record_platform(
                repo_root,
                Path(args.codex),
                kit=Path(args.kit).resolve() if args.kit else None,
                scratch=Path(args.scratch).resolve() if args.scratch else None,
                timeout=args.timeout,
            )
            target = write_platform_evidence(
                repo_root, record, Path(args.evidence) if args.evidence else None
            )
            failed_harness = sorted(
                name for name, entry in record["harness"].items() if not entry["ok"]
            )
            summary = (
                f"platform={record['platform']} revision={record['revision']} "
                f"binary_sha256={record['binary_sha256']} written={target}"
            )
            if failed_harness:
                summary += f" failed_harness={','.join(failed_harness)}"
            _emit(record, args.json, args.quiet, summary)
            return 1 if failed_harness else 0

        out_path = Path(args.out).resolve() if args.out else None
        document = build_candidate(repo_root, out_path)
        archive = document["archive"]
        summary = (
            f"candidate built={str(document['built']).lower()} "
            f"files={document['included_count']} bytes={document['total_bytes']} "
            f"bundle_digest={document['bundle_digest']}"
        )
        if archive:
            summary += f" archive={archive['path']} archive_sha256={archive['sha256']}"
        if document["refusals"]:
            summary += f" refusals={len(document['refusals'])}"
        _emit(document, args.json, args.quiet, summary)
        if document["refusals"]:
            for refusal in document["refusals"]:
                print(
                    f"error: {refusal['code']}: {refusal['path']}: {refusal['detail']}",
                    file=sys.stderr,
                )
            return 1
        return 0
    except (ReadinessError, jev_manifest.ManifestError, isolated_env.EnvError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
