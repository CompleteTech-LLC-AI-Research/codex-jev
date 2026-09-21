#!/usr/bin/env python3
"""Validate the JEV compatibility manifest, an integration profile, and this checkout.

Exit codes: 0 = valid, 1 = validation failed, 2 = usage or internal error.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_manifest


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Validate the JEV compatibility manifest and integration profile."
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to compatibility-manifest.json (default: jev/compatibility-manifest.json).",
    )
    parser.add_argument(
        "--profile", default=None, help="Path to an integration profile to validate."
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Integration host checkout root (default: repository containing this script).",
    )
    parser.add_argument(
        "--components-root",
        default=None,
        help="Directory of component checkouts to compare against the pinned revisions.",
    )
    parser.add_argument(
        "--patch-state",
        choices=["applied", "absent"],
        default=None,
        help="Assert that every manifest patch is applied to (or absent from) this checkout.",
    )
    parser.add_argument(
        "--native-adapter",
        choices=["applied", "absent"],
        default=None,
        help="Assert that the declared native source adapter is installed and wired (or removed).",
    )
    parser.add_argument(
        "--approval-enforcement",
        choices=["disabled"],
        default=None,
        help=(
            "Assert that the approval enforcement gate is declared and still disabled "
            "by default."
        ),
    )
    parser.add_argument(
        "--check-checkout",
        action="store_true",
        help="Assert that HEAD is the pinned host base commit.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    parser.add_argument(
        "--quiet", action="store_true", help="Print nothing on success."
    )
    return parser.parse_args(argv)


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
        profile = (
            jev_manifest.load_json(args.profile, "integration profile")
            if args.profile
            else None
        )
    except jev_manifest.ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    errors = jev_manifest.validate_manifest(
        manifest, repo_root=repo_root, profile=profile
    )
    if args.patch_state:
        errors += jev_manifest.check_patch_state(manifest, repo_root, args.patch_state)
    if args.native_adapter:
        errors += jev_manifest.check_native_adapter(
            manifest, repo_root, args.native_adapter
        )
    if args.approval_enforcement:
        errors += jev_manifest.check_approval_enforcement(
            manifest, args.approval_enforcement
        )
    if args.check_checkout:
        errors += jev_manifest.check_checkout(manifest, repo_root)
    if args.components_root:
        errors += jev_manifest.check_component_revisions(manifest, args.components_root)

    report = {
        "ok": not errors,
        "manifest": str(manifest_path),
        "profile": str(Path(args.profile).resolve()) if args.profile else None,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print(
            f"{len(errors)} validation error(s); this combination is not supported by the pin.",
            file=sys.stderr,
        )
    elif not args.quiet:
        profile_note = f" with profile {report['profile']}" if report["profile"] else ""
        print(f"ok: {manifest_path}{profile_note}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
