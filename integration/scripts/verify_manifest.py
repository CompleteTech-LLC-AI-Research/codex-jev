#!/usr/bin/env python3
"""Resolve the JEV integration pins against real checkouts and fail explicitly.

This verifier is the executable form of ``integration/manifest.json``. It proves
that a clean checkout resolves to exactly the pinned inputs, and it refuses --
with a non-zero exit and a specific reason -- any unsupported combination rather
than proceeding on an assumption.

Only the Python standard library is used so a fresh checkout can run it with no
installation step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_UNSUPPORTED = 2


class UnsupportedCombination(Exception):
    """A combination the manifest refuses explicitly."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UnsupportedCombination(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def current_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return f"{system}-{machine}"


def check_platform(manifest: dict) -> list[str]:
    here = current_platform()
    for entry in manifest["supported_platforms"]:
        if entry["platform"] == here:
            if entry["status"] == "unsupported":
                raise UnsupportedCombination(
                    f"platform {here!r} is declared unsupported: {entry['notes']}"
                )
            return [f"platform {here!r} accepted (status={entry['status']})"]
    raise UnsupportedCombination(
        f"platform {here!r} is not listed in supported_platforms"
    )


def check_component_pins(manifest: dict, component_root: Path) -> list[str]:
    notes: list[str] = []
    for pin in manifest["component_pins"]:
        repo = component_root / pin["name"]
        if not (repo / ".git").exists():
            raise UnsupportedCombination(f"component checkout missing: {repo}")
        head = git(repo, "rev-parse", "HEAD")
        if head != pin["commit"]:
            raise UnsupportedCombination(
                f"{pin['name']}: HEAD {head} != pinned {pin['commit']}"
            )
        git(repo, "cat-file", "-e", pin["commit"])
        for artifact in pin["artifacts"]:
            path = repo / artifact["path"]
            if not path.is_file():
                raise UnsupportedCombination(
                    f"{pin['name']}: pinned artifact missing: {artifact['path']}"
                )
            actual = sha256_file(path)
            if actual != artifact["sha256"]:
                raise UnsupportedCombination(
                    f"{pin['name']}: {artifact['path']} sha256 {actual} != pinned "
                    f"{artifact['sha256']}"
                )
        notes.append(
            f"{pin['name']} @ {pin['commit'][:12]} ({len(pin['artifacts'])} artifact(s) verified)"
        )
    return notes


def check_bus_vendored_identical(manifest: dict, component_root: Path) -> list[str]:
    fabric = component_root / "jev-context-fabric"
    prune = component_root / "jev-prune-kit"
    pairs = [
        (fabric / "src/jev_context/bus.py", prune / "jev_prune/bus.py"),
        (fabric / "adapters/bus.mjs", prune / "adapters/bus.mjs"),
    ]
    for left, right in pairs:
        if not left.is_file() or not right.is_file():
            raise UnsupportedCombination(f"jev-bus vendored copy missing: {left} / {right}")
        if sha256_file(left) != sha256_file(right):
            raise UnsupportedCombination(
                f"jev-bus vendored copies drifted: {left} != {right}"
            )
    return ["jev-bus vendored copies byte-identical (bus.py, bus.mjs)"]


def check_codex_pin(manifest: dict, codex_repo: Path) -> list[str]:
    host = manifest["integration_host"]
    pinned = host["codex_pin"]["commit"]
    if not (codex_repo / ".git").exists():
        raise UnsupportedCombination(f"codex checkout missing: {codex_repo}")
    head = git(codex_repo, "rev-parse", "HEAD")
    if head != pinned:
        raise UnsupportedCombination(
            f"codex-jev HEAD {head} != codex_pin.commit {pinned}"
        )
    git(codex_repo, "cat-file", "-e", pinned)
    notes = [f"codex pin {pinned[:12]} present at HEAD"]
    for step in manifest["patch_order"]:
        for path, expected in step.get("verified_blobs_before", {}).items():
            actual = git(codex_repo, "rev-parse", f"{pinned}:{path}")
            if actual != expected:
                raise UnsupportedCombination(
                    f"patch order {step['order']} ({step['name']}): blob for {path} "
                    f"is {actual} but the adapter was pinned against {expected}"
                )
            notes.append(f"blob anchor ok: {path} @ {step['name']}")
    return notes


def check_exclusions(manifest: dict, integration_dir: Path) -> list[str]:
    excluded = [entry["name"] for entry in manifest["excluded_components"]]
    # The declaration files name the exclusion in order to state and enforce it.
    declaring_files = {"README.md", "manifest.json", "scripts/verify_manifest.py"}
    offenders: list[str] = []
    for path in integration_dir.rglob("*"):
        if not path.is_file():
            continue
        if str(path.relative_to(integration_dir)) in declaring_files:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in excluded:
            if name in text:
                offenders.append(f"{path.relative_to(integration_dir)} references {name}")
    if offenders:
        raise UnsupportedCombination(
            "excluded component referenced: " + "; ".join(offenders)
        )
    return [f"excluded components absent: {', '.join(excluded)}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--component-root",
        type=Path,
        required=True,
        help="Directory containing the five supporting component checkouts.",
    )
    parser.add_argument(
        "--codex-repo",
        type=Path,
        default=None,
        help="codex-jev checkout; defaults to two levels above the manifest.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema") != "jev.codex.integration.manifest.v1":
        raise UnsupportedCombination(f"unexpected manifest schema: {manifest.get('schema')}")

    codex_repo = args.codex_repo or args.manifest.resolve().parents[2]
    integration_dir = args.manifest.resolve().parent

    checks = [
        *check_platform(manifest),
        *check_codex_pin(manifest, codex_repo),
        *check_component_pins(manifest, args.component_root),
        *check_bus_vendored_identical(manifest, args.component_root),
        *check_exclusions(manifest, integration_dir),
    ]

    if not args.quiet:
        for line in checks:
            print(f"ok: {line}")
        print(f"\nRESOLVED: {len(checks)} checks passed for manifest {manifest['manifest_version']}")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UnsupportedCombination as exc:
        print(f"UNSUPPORTED: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_UNSUPPORTED)
