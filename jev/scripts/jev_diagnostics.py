#!/usr/bin/env python3
"""Print one redacted, machine-readable diagnostics report for this checkout.

Phase 6.3 asks the package to ship "manifest verification, build/profile
instructions, feature controls, and diagnostics". The first three are
`RELEASE.md` sections; this is the fourth, as a single JSON document an operator
or a CI lane can capture and diff, rather than a table a human reads.

What it describes: the host pin and the revision it was read from, the manifest
and every profile's validation result, the ordered patch states and the native
approval adapter, the resolved feature switches, the remote-inference state, the
component pins, the recorded binary digest, the fixture catalog, the ambient
Codex home, and the isolated environment.

Two properties matter more than the field list:

* The document is **allow-listed**. No file content, no credential value, and no
  captured payload is read into it. The ambient home and the isolated
  environment are described by path and existence only.
* `redaction` is part of the document rather than a promise about it. The
  assembled report is scanned with the same credential-shape rules the candidate
  gate refuses on (`release_readiness.CONTENT_RULES`), every match is replaced,
  and each finding is listed by rule and JSON pointer - never by value. A lane
  therefore does not have to trust the author: `--check` fails on any finding.

Exit codes: 0 = a report was produced (and, under `--check`, every check held);
1 = a check failed; 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env  # noqa: E402
import jev_manifest  # noqa: E402
import release_readiness  # noqa: E402

SCHEMA = "jev-diagnostics.v1"
MANIFEST_REL = "jev/compatibility-manifest.json"

#: What a matched value is replaced with. The placeholder names the rule and
#: contains no part of the original, so a report can be pasted anywhere.
PLACEHOLDER = "[redacted:{rule}]"


def redact(value, path=()):
    """Replace every credential-shaped string; return (value, findings).

    A finding is a rule name and a JSON pointer. The matched text is never
    returned, so a caller cannot leak the input by printing the findings.
    """
    if isinstance(value, dict):
        out = {}
        findings = []
        for key in sorted(value):
            replaced, found = redact(value[key], (*path, str(key)))
            out[key] = replaced
            findings += found
        return out, findings
    if isinstance(value, list):
        out = []
        findings = []
        for index, item in enumerate(value):
            replaced, found = redact(item, (*path, str(index)))
            out.append(replaced)
            findings += found
        return out, findings
    if isinstance(value, str):
        findings = []
        text = value
        for rule, pattern in release_readiness.CONTENT_RULES:
            if pattern.search(text):
                findings.append({"pointer": "/".join(path), "rule": rule})
                text = pattern.sub(PLACEHOLDER.format(rule=rule), text)
        return text, findings
    return value, []


def revision_of(root):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def host_block(manifest):
    host = manifest["host"]
    return {
        "base_ref": host["base_ref"],
        "base_commit": host["base_commit"],
        "rust_toolchain": host["rust_toolchain"],
        "python_requirement": host["python_requirement"],
        "platforms_supported": list(host["platforms"]["supported"]),
        "platforms_reference": host["platforms"]["reference"],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def manifest_block(root, manifest):
    path = Path(root) / MANIFEST_REL
    errors = jev_manifest.validate_manifest(manifest, repo_root=root)
    return {
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
                "features": jev_manifest.effective_features(manifest, profile),
            }
        )
    return blocks


def _two_way_state(check, manifest, root):
    """Which of `applied` / `absent` (or neither) describes this tree."""
    applied = check(manifest, root, "applied")
    absent = check(manifest, root, "absent")
    if not applied:
        return "applied", []
    if not absent:
        return "absent", []
    return "mixed", (applied + absent)[:8]


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
                "matches_declared": path.is_file()
                and jev_manifest.sha256_file(path) == patch["sha256"],
                "applies_to_host_base": patch.get("applies_to_host_base"),
            }
        )
    return {
        "state": state,
        "ok": state != "mixed",
        "errors": errors,
        "patches": patches,
    }


def native_adapter_block(root, manifest):
    state, errors = _two_way_state(jev_manifest.check_native_adapter, manifest, root)
    return {"state": state, "ok": state != "mixed", "errors": errors}


def component_blocks(manifest, components_root):
    resolution = {}
    errors = []
    if components_root:
        resolution = jev_manifest.resolve_component_revisions(components_root)
        errors = jev_manifest.check_component_revisions(manifest, components_root)
    blocks = []
    for component in manifest.get("components", []):
        component_id = component.get("id")
        local = resolution.get(component_id)
        blocks.append(
            {
                "id": component_id,
                "pinned_revision": component.get("revision"),
                "local_revision": local,
                "matches_pin": None
                if local is None
                else local == component.get("revision"),
                "repository": component.get("repository"),
            }
        )
    return {
        "components_root": str(components_root) if components_root else None,
        "checked": bool(components_root),
        "ok": not errors,
        "errors": errors,
        "components": blocks,
    }


def features_block(manifest):
    return {
        "defaults": {
            name: bool(spec.get("default"))
            for name, spec in sorted(manifest.get("features", {}).items())
        }
    }


def remote_inference_block(manifest):
    credential = manifest.get("credentials", {}).get("remote_inference", {})
    return {
        "feature_default": bool(
            manifest.get("features", {})
            .get("remote_inference.enabled", {})
            .get("default")
        ),
        "consent": credential.get("consent"),
        "budget_usd_max": credential.get("budget_usd_max"),
        "enabled_in_any_profile": any(
            bool(profile.get("features", {}).get("remote_inference.enabled"))
            for profile in manifest.get("profiles", {}).values()
        ),
    }


def binary_block(binary):
    if not binary:
        return {"path": None, "sha256": None, "present": None, "kind": None}
    path = Path(binary)
    return {
        "path": str(path),
        "sha256": jev_manifest.sha256_file(path) if path.is_file() else None,
        "present": path.is_file(),
        "kind": "debug" if "debug" in path.parts else "release",
    }


def isolated_block(root, env_dir):
    resolved = Path(env_dir) if env_dir else isolated_env.default_env_dir(root)
    record = {
        "env_dir": str(resolved),
        "exists": resolved.exists(),
        "initialized": (resolved / "isolated-env.json").is_file(),
    }
    if record["initialized"]:
        state = isolated_env.status(env_dir=resolved, root=root)
        record.update(
            {
                "profile": state["profile"],
                "host_commit": state["host_commit"],
                "binary_present": state["binary_present"],
                "binary_matches_plan": state["binary_matches_plan"],
                "optional_features_enabled": state["optional_features_enabled"],
                "remote_inference_enabled": state["remote_inference_enabled"],
            }
        )
    return record


def fixtures_block(root):
    fixture_root = Path(root) / "jev" / "tests"
    blocks = []
    for path in sorted(fixture_root.glob("*_fixtures")):
        if path.is_dir():
            blocks.append(
                {
                    "directory": str(path.relative_to(Path(root))),
                    "files": len([item for item in path.rglob("*") if item.is_file()]),
                }
            )
    return {"ok": bool(blocks), "directories": blocks}


def build(root, components_root=None, binary=None, env_dir=None):
    """Assemble the report; redaction is applied to the finished document."""
    root = Path(root).resolve()
    manifest = jev_manifest.load_json(
        Path(root) / MANIFEST_REL, "compatibility manifest"
    )
    document = {
        "schema": SCHEMA,
        "revision": revision_of(root),
        "host": host_block(manifest),
        "manifest": manifest_block(root, manifest),
        "profiles": profile_blocks(root, manifest),
        "patches": patch_block(root, manifest),
        "native_adapter": native_adapter_block(root, manifest),
        "features": features_block(manifest),
        "remote_inference": remote_inference_block(manifest),
        "components": component_blocks(manifest, components_root),
        "binary": binary_block(binary),
        "ambient_home": isolated_env.ambient_home_fingerprint(),
        "isolated": isolated_block(root, env_dir),
        "fixtures": fixtures_block(root),
    }
    redacted, findings = redact(document)
    redacted["redaction"] = {
        "rules": [rule for rule, _ in release_readiness.CONTENT_RULES],
        "findings": findings,
        "clean": not findings,
    }
    redacted["checks"] = run_checks(redacted)
    redacted["ok"] = not redacted["checks"]["failures"]
    return redacted


def run_checks(document):
    """Name every reason this report should not be credited."""
    failures = []
    if not document["redaction"]["clean"]:
        failures.append(
            f"redaction: {len(document['redaction']['findings'])} credential-shaped value(s) found"
        )
    if not document["manifest"]["ok"]:
        failures.append(f"manifest: {len(document['manifest']['errors'])} error(s)")
    for profile in document["profiles"]:
        if not profile["ok"]:
            failures.append(
                f"profile {profile['id']}: {len(profile['errors'])} error(s)"
            )
    if not document["patches"]["ok"]:
        failures.append("patch state is mixed: neither applied nor absent")
    if not document["native_adapter"]["ok"]:
        failures.append("native adapter state is mixed")
    for patch in document["patches"]["patches"]:
        if not patch["matches_declared"]:
            failures.append(f"patch {patch['id']}: file hash does not match the pin")
    if document["components"]["checked"] and not document["components"]["ok"]:
        failures.append(
            f"component pins: {len(document['components']['errors'])} mismatch(es)"
        )
    isolated = document["isolated"]
    if isolated["initialized"] and not isolated["binary_matches_plan"]:
        failures.append("isolated env: the binary does not match the recorded plan")
    if document["binary"]["present"] is False:
        failures.append("binary: the requested path does not exist")
    return {"ok": not failures, "failures": failures}


def print_document(document):
    failures = document["checks"]["failures"]
    print(
        f"diagnostics schema={document['schema']} "
        f"revision={document['revision']} ok={str(document['ok']).lower()} "
        f"redaction_findings={len(document['redaction']['findings'])} "
        f"failures={len(failures)}"
    )
    for failure in failures:
        print(f"  - {failure}")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    node = sub.add_parser("report", help="print the diagnostics report")
    node.add_argument("--root", default="", help="integration checkout root")
    node.add_argument(
        "--components-root", default="", help="pinned component checkouts"
    )
    node.add_argument("--binary", default="", help="host binary to hash")
    node.add_argument("--env-dir", default="", help="isolated environment directory")
    node.add_argument("--json", default="", help="write the document here")
    node.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any check fails, including a redaction finding",
    )
    node.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve() if args.root else jev_manifest.repository_root()
    document = build(
        root,
        components_root=Path(args.components_root) if args.components_root else None,
        binary=Path(args.binary) if args.binary else None,
        env_dir=Path(args.env_dir) if args.env_dir else None,
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        print_document(document)
    if args.check and not document["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
