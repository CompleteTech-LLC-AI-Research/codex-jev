#!/usr/bin/env python3
"""Apply or reverse the manifest patches against the pinned host source.

Patches are applied in the order recorded in the compatibility manifest. The
script is idempotent: a patch that is already in the requested state is left
alone, so re-running a build never rewrites the tree twice.

Exit codes: 0 = request satisfied, 1 = a patch did not apply, 2 = usage or
unreadable input.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_manifest


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Apply or reverse manifest patches in recorded order."
    )
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse the patches instead of applying them.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report the current state without modifying the tree.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def git_apply(repo_root, patch_file, reverse=False, check=False):
    args = ["git", "apply"]
    if check:
        args.append("--check")
    if reverse:
        args.append("--reverse")
    args.append(str(patch_file))
    return subprocess.run(
        args, cwd=repo_root, capture_output=True, text=True, check=False
    )


def patch_state(repo_root, patch_file):
    """Return ``applied``, ``absent``, or ``conflicted`` for one patch."""
    if git_apply(repo_root, patch_file, reverse=True, check=True).returncode == 0:
        return "applied"
    if git_apply(repo_root, patch_file, check=True).returncode == 0:
        return "absent"
    return "conflicted"


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else jev_manifest.repository_root()
    )
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else repo_root / "jev" / "compatibility-manifest.json"
    )
    try:
        manifest = jev_manifest.load_json(manifest_path, "compatibility manifest")
    except jev_manifest.ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    desired = "absent" if args.reverse else "applied"
    results = []
    failed = False
    for patch in sorted(manifest.get("patches", []), key=lambda item: item["order"]):
        patch_file = repo_root / patch["file"]
        if not patch_file.is_file():
            results.append({"id": patch["id"], "state": "missing", "action": "none"})
            failed = True
            continue
        state = patch_state(repo_root, patch_file)
        action = "none"
        if state == desired or args.check:
            pass
        elif state == "conflicted":
            failed = True
        else:
            result = git_apply(repo_root, patch_file, reverse=args.reverse)
            if result.returncode == 0:
                action = "reversed" if args.reverse else "applied"
                state = desired
            else:
                detail = (result.stderr or result.stdout).strip().splitlines()
                results.append(
                    {
                        "id": patch["id"],
                        "state": state,
                        "action": "failed",
                        "detail": detail[0] if detail else "git apply failed",
                    }
                )
                failed = True
                continue
        results.append({"id": patch["id"], "state": state, "action": action})
        if state != desired and not args.check:
            failed = True

    report = {
        "repo_root": str(repo_root),
        "desired_state": desired,
        "check_only": bool(args.check),
        "patches": results,
        "ok": not failed,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for entry in results:
            print(
                f"{entry['id']}: {entry['state']}"
                + (
                    f" ({entry['action']})"
                    if entry.get("action") not in (None, "none")
                    else ""
                )
                + (f" - {entry['detail']}" if entry.get("detail") else "")
            )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
