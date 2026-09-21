#!/usr/bin/env python3
"""Print a redacted, machine-readable report of this integration checkout.

Phase 6.3 asks the package to ship diagnostics, so ``report`` describes what
this checkout *is*: the host pin and the revision it was read from, the manifest
and every profile's validation result, the state of each ordered patch and of
the native approval adapter, the feature-switch defaults, the component pins,
the recorded binary hash, the fixture catalog, and the isolated environment.

Two properties matter more than the field list:

* The document is assembled from allow-listed fields. No file content, no
  credential value, and no captured payload is read into it. The ambient Codex
  home is described by path and existence only.
* ``redaction`` is part of the document rather than a promise about it: the
  finished report is re-scanned for secret-shaped strings and every finding is
  listed by pattern and JSON pointer - never by value. ``--check`` fails a lane
  on a finding, on a manifest or profile error, or on a checkout whose patch
  state is internally inconsistent, so a caller does not have to trust the
  author.

Exit codes: 0 = report produced (and, under ``--check``, clean), 1 = a check
failed, 2 = usage error.
"""

import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env
import jev_manifest
import release_artifact

DIAGNOSTICS_VERSION = 1
MANIFEST_REL = "jev/compatibility-manifest.json"


def scrub(value, path=()):
    """Find secret-shaped strings in a report; return findings, never values."""
    findings = []
    if isinstance(value, dict):
        for key in sorted(value):
            findings += scrub(value[key], (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings += scrub(item, (*path, str(index)))
    elif isinstance(value, str):
        for name, pattern in release_artifact.SECRET_PATTERNS:
            if pattern.search(value):
                findings.append({"pointer": "/".join(path), "pattern": name})
    return findings


def manifest_block(root):
    path = Path(root) / MANIFEST_REL
    manifest = jev_manifest.load_json(path, "compatibility manifest")
    errors = jev_manifest.validate_manifest(manifest, repo_root=root)
    return manifest, {
        "path": MANIFEST_REL,
        "sha256": jev_manifest.sha256_file(path),
        "ok": not errors,
        "errors": errors,
    }


def profile_blocks(root, manifest):
    blocks = []
    for path in sorted((Path(root) / "jev" / "profiles").glob("*.json")):
        profile = jev_manifest.load_json(path, f"profile {path.stem}")
        errors = jev_manifest.validate_manifest(
            manifest, repo_root=root, profile=profile
        )
        blocks.append(
            {
                "id": profile.get("id"),
                "file": str(path.relative_to(Path(root))),
                "sha256": jev_manifest.sha256_file(path),
                "ok": not errors,
                "errors": errors,
            }
        )
    return blocks


def _two_way_state(check, manifest, root):
    """Report which of ``applied``/``absent`` (or neither) describes this tree."""
    applied = check(manifest, root, "applied")
    absent = check(manifest, root, "absent")
    if not applied:
        state = "applied"
    elif not absent:
        state = "absent"
    else:
        state = "mixed"
    errors = [] if state != "mixed" else (applied + absent)[:8]
    return state, errors


def patch_block(root, manifest):
    state, errors = _two_way_state(jev_manifest.check_patch_state, manifest, root)
    patches = []
    for patch in sorted(manifest.get("patches", []), key=lambda item: item["order"]):
        path = Path(root) / patch["file"]
        patches.append(
            {
                "id": patch["id"],
                "order": patch["order"],
                "file": patch["file"],
                "declared_sha256": patch["sha256"],
                "file_sha256": jev_manifest.sha256_file(path)
                if path.is_file()
                else None,
                "applies_to_host_base": patch.get("applies_to_host_base"),
            }
        )
    return {"state": state, "errors": errors, "patches": patches}


def native_adapter_block(root, manifest):
    state, errors = _two_way_state(jev_manifest.check_native_adapter, manifest, root)
    return {"state": state, "errors": errors}


def component_blocks(manifest, components_root):
    resolution = {}
    comparison_errors = []
    if components_root:
        resolution = jev_manifest.resolve_component_revisions(components_root)
        comparison_errors = jev_manifest.check_component_revisions(
            manifest, components_root
        )
    blocks = []
    for component in manifest.get("components", []):
        component_id = component.get("id")
        local = resolution.get(component_id)
        blocks.append(
            {
                "id": component_id,
                "pinned_revision": component.get("revision"),
                "python_requirement": component.get("python_requirement"),
                "local_revision": local,
                "local_state": (
                    "not-compared"
                    if local is None
                    else ("match" if local == component.get("revision") else "drift")
                ),
            }
        )
    return {
        "components_root": str(components_root) if components_root else None,
        "comparison": "not-compared" if not components_root else "compared",
        "errors": comparison_errors,
        "components": blocks,
    }


def binary_block(root, binary):
    binary = Path(binary) if binary else isolated_env.default_binary(root)
    present = binary.is_file()
    return {
        "path": str(binary),
        "present": present,
        "sha256": jev_manifest.sha256_file(binary) if present else None,
        "size_bytes": binary.stat().st_size if present else None,
    }


def isolated_block(root, env_dir):
    env_dir = Path(env_dir) if env_dir else isolated_env.default_env_dir(root)
    if not (env_dir / "isolated-env.json").is_file():
        return {"env_dir": str(env_dir), "present": False, "status": None}
    try:
        status = isolated_env.status(env_dir=env_dir, root=root)
    except (isolated_env.EnvError, jev_manifest.ManifestError) as error:
        return {"env_dir": str(env_dir), "present": True, "error": str(error)}
    return {"env_dir": str(env_dir), "present": True, "status": status}


def feature_block(manifest):
    return {
        name: {
            "default": bool(spec.get("default")),
            "components": list(spec.get("components") or []),
            "requires": list(spec.get("requires") or []),
            "requires_patches": list(spec.get("requires_patches") or []),
            "requires_consent": spec.get("requires_consent"),
            "requires_budget": spec.get("requires_budget"),
        }
        for name, spec in sorted(manifest.get("features", {}).items())
    }


def report(root=None, components_root=None, binary=None, env_dir=None):
    root = Path(root or jev_manifest.repository_root())
    manifest, manifest_info = manifest_block(root)
    state = release_artifact.checkout_state(root)
    document = {
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "checkout": {
            "repository": manifest["integration"]["repository"],
            "path": str(root),
            "revision": state["revision"],
            "worktree_dirty": state["worktree_dirty"],
            "platform": f"{sys.platform}-{platform.machine()}",
            "python": platform.python_version(),
            "reference_platform": manifest["host"]["platforms"]["reference"],
            "is_reference_platform": f"{sys.platform}-{platform.machine()}"
            == manifest["host"]["platforms"]["reference"],
        },
        "host": manifest["host"],
        "manifest": manifest_info,
        "profiles": profile_blocks(root, manifest),
        "patches": patch_block(root, manifest),
        "native_adapter": native_adapter_block(root, manifest),
        "components": component_blocks(manifest, components_root),
        "features": feature_block(manifest),
        "fixtures": release_artifact.fixture_catalog_digest(root),
        "binary": binary_block(root, binary),
        "isolated_env": isolated_block(root, env_dir),
        "ambient_home": isolated_env.ambient_home_fingerprint(),
        "credentials": {
            "remote_inference": {
                "consent": manifest["credentials"]["remote_inference"]["consent"],
                "budget_usd_max": manifest["credentials"]["remote_inference"][
                    "budget_usd_max"
                ],
            }
        },
        "redaction": {
            "patterns": [name for name, _ in release_artifact.SECRET_PATTERNS],
            "note": (
                "the report is assembled from allow-listed fields only; this "
                "block re-scans the finished document and names no value"
            ),
            "findings": [],
        },
    }
    document["redaction"]["findings"] = scrub(document)
    return document


def evaluate(document, expect_patch_state=None, expect_native_adapter=None):
    """Return the reasons ``--check`` would fail on this report."""
    failures = []
    for finding in document["redaction"]["findings"]:
        failures.append(
            f"E_DIAGNOSTICS_REDACTION: the report contains secret-shaped content at "
            f"{finding['pointer']} matching {finding['pattern']}"
        )
    if not document["manifest"]["ok"]:
        failures += [
            f"E_DIAGNOSTICS_MANIFEST: {error}"
            for error in document["manifest"]["errors"]
        ]
    for profile in document["profiles"]:
        if not profile["ok"]:
            failures += [
                f"E_DIAGNOSTICS_PROFILE: {profile['id']}: {error}"
                for error in profile["errors"]
            ]
    for name, block in (
        ("patches", document["patches"]),
        ("native_adapter", document["native_adapter"]),
    ):
        if block["state"] == "mixed":
            failures.append(
                f"E_DIAGNOSTICS_{name.upper()}: this checkout is neither fully "
                f"applied nor fully absent for {name}"
            )
    failures += [
        f"E_DIAGNOSTICS_COMPONENT: {error}"
        for error in document["components"]["errors"]
    ]
    for expected, block, label in (
        (expect_patch_state, document["patches"], "patch state"),
        (expect_native_adapter, document["native_adapter"], "native adapter state"),
    ):
        if expected is not None and block["state"] != expected:
            failures.append(
                f"E_DIAGNOSTICS_EXPECT: the {label} is {block['state']!r}, not the "
                f"expected {expected!r}"
            )
    return failures


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report_parser = sub.add_parser("report", help="Print the diagnostics report.")
    report_parser.add_argument("--root", default=None)
    report_parser.add_argument("--components-root", default=None)
    report_parser.add_argument("--binary", default=None)
    report_parser.add_argument("--env-dir", default=None)
    report_parser.add_argument("--out", default=None)
    report_parser.add_argument("--check", action="store_true")
    report_parser.add_argument(
        "--expect-patch-state", choices=["applied", "absent"], default=None
    )
    report_parser.add_argument(
        "--expect-native-adapter", choices=["applied", "absent"], default=None
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        document = report(
            args.root,
            components_root=args.components_root,
            binary=args.binary,
            env_dir=args.env_dir,
        )
    except (jev_manifest.ManifestError, isolated_env.EnvError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    failures = (
        evaluate(document, args.expect_patch_state, args.expect_native_adapter)
        if args.check
        else []
    )
    document["check"] = {"requested": args.check, "failures": failures}
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text, end="")
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
