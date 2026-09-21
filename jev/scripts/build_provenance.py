#!/usr/bin/env python3
"""Build the pinned host with the repository recipe and record provenance.

Two subcommands:

``build``  run ``cargo build -p codex-cli`` (the recipe the repository's own
           ``app-server-test-client`` target uses), tee the log, then record
           provenance for the produced binary.
``record`` record provenance for a binary that already exists.

The record pins the host commit, toolchain versions, every manifest patch
digest, the fixture catalog digest, and the binary hash, so an isolated run can
be tied back to exact inputs.

Exit codes: 0 = ok, 1 = failure, 2 = usage error.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env
import jev_manifest
import offline_fixtures


def command_output(argv, cwd=None):
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"<unavailable: {error}>"
    text = (completed.stdout or completed.stderr or "").strip()
    return text or f"<exited {completed.returncode}>"


def git_revision(root):
    return command_output(["git", "-C", str(root), "rev-parse", "HEAD"])


def collect(root=None, binary=None, build_log=None):
    root = Path(root or isolated_env.repository_root())
    # The pinned toolchain is declared in codex-rs/rust-toolchain.toml, so
    # resolve rustc/cargo from there rather than from the ambient shell.
    codex_rs = root / "codex-rs"
    manifest = isolated_env.load_manifest(root)
    binary = Path(binary) if binary else isolated_env.default_binary(root)
    catalog = offline_fixtures.load_fixtures(root / "jev" / "fixtures")
    catalog_digest = hashlib.sha256(
        json.dumps(offline_fixtures.fixture_catalog(catalog), sort_keys=True).encode()
    ).hexdigest()
    provenance = {
        "provenance_version": 1,
        "host_repository": manifest["integration"]["repository"],
        "host_pinned_base_commit": manifest["host"]["base_commit"],
        "host_checkout_commit": git_revision(root),
        "host_worktree_dirty": bool(
            command_output(["git", "-C", str(root), "status", "--porcelain"])
            not in ("", "<exited 0>")
        ),
        "rust_toolchain_pin": manifest["host"]["rust_toolchain"],
        "rustc": command_output(["rustc", "--version"], cwd=codex_rs),
        "cargo": command_output(["cargo", "--version"], cwd=codex_rs),
        "python": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "patches": [
            {"id": patch["id"], "sha256": patch["sha256"]}
            for patch in manifest["patches"]
        ],
        "components": [
            {"id": component["id"], "revision": component["revision"]}
            for component in manifest["components"]
        ],
        "fixture_catalog_digest": catalog_digest,
        "fixture_tier": offline_fixtures.FIXTURE_TIER,
        "openssl": {
            "static_link": os.environ.get("OPENSSL_STATIC"),
            "dir_recorded": bool(os.environ.get("OPENSSL_DIR")),
        },
        "binary": {"path": str(binary)},
        "build_log": str(build_log) if build_log else None,
        "recorded_at_unix_ms": int(time.time() * 1000),
    }
    if binary.is_file():
        provenance["binary"]["sha256"] = jev_manifest.sha256_file(binary)
        provenance["binary"]["size_bytes"] = binary.stat().st_size
        provenance["binary"]["present"] = True
    else:
        provenance["binary"]["present"] = False
    return provenance


def build(root=None, log_path=None, release=False):
    root = Path(root or isolated_env.repository_root())
    codex_rs = root / "codex-rs"
    log_path = Path(log_path) if log_path else root / ".jev" / "build-cli.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["cargo", "build", "-p", "codex-cli"]
    if release:
        argv.append("--release")
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(argv)}\n")
        handle.flush()
        completed = subprocess.run(
            argv, cwd=codex_rs, stdout=handle, stderr=subprocess.STDOUT, check=False
        )
    binary = codex_rs / "target" / ("release" if release else "debug") / "codex"
    if completed.returncode != 0:
        raise isolated_env.EnvError(
            f"build failed with exit {completed.returncode}; see {log_path}"
        )
    return collect(root, binary, build_log=log_path)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="Build the pinned CLI.")
    build_parser.add_argument("--root", default=None)
    build_parser.add_argument("--log", default=None)
    build_parser.add_argument("--release", action="store_true")
    record_parser = sub.add_parser("record", help="Record an existing binary.")
    record_parser.add_argument("--root", default=None)
    record_parser.add_argument("--binary", default=None)
    for parser_ in (build_parser, record_parser):
        parser_.add_argument("--out", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "build":
            provenance = build(args.root, args.log, args.release)
        else:
            provenance = collect(args.root, args.binary)
    except isolated_env.EnvError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(provenance, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
