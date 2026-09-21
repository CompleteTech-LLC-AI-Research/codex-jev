#!/usr/bin/env python3
"""Resolve and verify the JEV Codex integration manifest.

Offline and dependency-free. Two jobs:

``check``
    Validate the manifest itself: pins, patch order, stage order, ownership,
    feature switches and the OmniRoute exclusion.

``resolve``
    Bind the manifest to checkouts on this machine: every recorded revision must
    match, every recorded artifact hash must match, every source patch must apply
    to the pinned host revision, and the vendored jev-bus copies must agree.

Any unsupported combination fails explicitly with a nonzero status rather than
being ignored. Exit codes: 0 = verified, 2 = unsupported/invalid input,
3 = on-disk mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "jev-codex.integration-manifest.v1"
HOST_ID = "codex-jev"
EXCLUDED_ID = "omniroute-codex-docker"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UnsupportedCombination(Exception):
    """Input that this manifest deliberately refuses (exit status 2)."""


class Mismatch(Exception):
    """State on disk that disagrees with the manifest (exit status 3)."""


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "manifest.integration.json"


def load_manifest(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UnsupportedCombination(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UnsupportedCombination(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise UnsupportedCombination("manifest root must be a JSON object")
    return document


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UnsupportedCombination(message)


def _walk_strings(node, path="") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_walk_strings(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk_strings(value, f"{path}[{index}]"))
    elif isinstance(node, str):
        found.append((path, node))
    return found


def validate(document: dict) -> dict:
    """Validate the manifest. Returns a summary of what was checked."""
    _require(document.get("schema") == SCHEMA, f"unsupported schema: {document.get('schema')!r} (expected {SCHEMA!r})")

    host = document.get("integration_host")
    _require(isinstance(host, dict), "integration_host is required")
    _require(host.get("id") == HOST_ID, f"integration host id must be {HOST_ID!r}")
    _require(bool(SHA40.match(host.get("revision", ""))), "integration_host.revision must be a 40-hex commit")

    exclusions = document.get("exclusions")
    _require(isinstance(exclusions, list) and exclusions, "exclusions list is required")
    excluded_ids = {entry.get("id") for entry in exclusions if isinstance(entry, dict)}
    excluded_repos = {entry.get("repository") for entry in exclusions if isinstance(entry, dict)}
    _require(EXCLUDED_ID in excluded_ids, f"{EXCLUDED_ID} must be recorded as excluded")

    components = document.get("components")
    _require(isinstance(components, list) and components, "components list is required")
    seen_ids: set[str] = set()
    seen_switches: set[str] = set()
    for component in components:
        _require(isinstance(component, dict), "each component must be an object")
        cid = component.get("id")
        _require(isinstance(cid, str) and cid, "each component needs an id")
        _require(cid not in seen_ids, f"duplicate component id: {cid}")
        seen_ids.add(cid)
        _require(cid != EXCLUDED_ID, f"{cid} is excluded and must not appear as a component")
        _require(component.get("repository") not in excluded_repos, f"{cid} points at an excluded repository")
        _require(bool(SHA40.match(component.get("revision", ""))), f"{cid}: revision must be a 40-hex commit")
        _require(isinstance(component.get("license"), str) and component["license"], f"{cid}: license is required")
        switch = component.get("feature_switch")
        _require(isinstance(switch, str) and switch, f"{cid}: feature_switch is required")
        _require(switch not in seen_switches, f"duplicate feature switch: {switch}")
        seen_switches.add(switch)
        targets = component.get("applies_to")
        if targets is not None:
            _require(targets == HOST_ID, f"{cid}: applies_to must be {HOST_ID!r}")
        artifact = component.get("artifact")
        if artifact is not None:
            _require(bool(SHA256.match(artifact.get("sha256", ""))), f"{cid}: artifact.sha256 must be a sha256")
            _require(artifact.get("base_revision") == host["revision"], f"{cid}: artifact.base_revision must equal the integration pin")
            _require(bool(artifact.get("targets")), f"{cid}: artifact.targets must list the files it changes")

    order = document.get("patch_order")
    _require(isinstance(order, list) and order, "patch_order is required")
    known = seen_ids | {HOST_ID}
    seen_order: set[int] = set()
    for step in order:
        _require(isinstance(step, dict), "each patch_order entry must be an object")
        index = step.get("order")
        _require(isinstance(index, int) and not isinstance(index, bool), "patch_order.order must be an integer")
        _require(index not in seen_order, f"duplicate patch_order.order: {index}")
        seen_order.add(index)
        _require(step.get("component") in known, f"patch_order references unknown component: {step.get('component')!r}")
        _require(step.get("base_revision") == host["revision"], f"patch_order step {index}: base_revision must equal the integration pin")
        dependency = step.get("depends_on_order")
        if dependency is not None:
            _require(isinstance(dependency, int) and dependency in seen_order, f"patch_order step {index}: depends_on_order must name an earlier step")
            _require(dependency < index, f"patch_order step {index}: depends_on_order must precede it")
    _require(sorted(seen_order) == list(range(1, len(order) + 1)), "patch_order must be a contiguous sequence starting at 1")

    bus = document.get("bus")
    _require(isinstance(bus, dict), "bus section is required")
    stages = bus.get("stages")
    _require(isinstance(stages, list) and len(stages) >= 2, "bus.stages must list the dedup and view stages")
    priorities: dict[int, dict] = {}
    claims_owner: dict[str, str] = {}
    for stage in stages:
        _require(isinstance(stage, dict), "each bus stage must be an object")
        priority = stage.get("priority")
        _require(isinstance(priority, int) and not isinstance(priority, bool), "bus stage priority must be an integer")
        _require(priority not in priorities, f"duplicate stage priority: {priority}")
        priorities[priority] = stage
        _require(stage.get("package") in seen_ids, f"stage {stage.get('id')!r} names an unknown package")
        for claim in stage.get("claims", []):
            _require(claim not in claims_owner or claims_owner[claim] == stage["package"],
                     f"claim {claim!r} is held by two packages")
            claims_owner[claim] = stage["package"]
        transport = stage.get("transport")
        _require(isinstance(transport, dict) and transport.get("kind") == "subprocess-json",
                 f"stage {stage.get('id')!r} must use the subprocess-json transport")
        _require(bool(transport.get("argv")), f"stage {stage.get('id')!r} needs an argv")
    dedup = min(priorities)
    view = max(priorities)
    _require(dedup < view, "duplicate-read pruning must run before the approved prose view")
    _require(priorities[dedup]["package"] == "jev-prune-kit", "the lowest-priority stage must be jev-prune-kit's dedup stage")
    _require(priorities[view]["package"] == "jev-context-fabric", "the view stage must belong to jev-context-fabric")
    for entry in bus.get("vendored_files", []):
        _require(bool(SHA256.match(entry.get("sha256", ""))), "vendored jev-bus hashes must be sha256 values")
    _require(len(bus.get("vendored_files", [])) >= 2, "both vendored jev-bus files must be recorded")
    _require("codex" not in bus.get("package_carrier_hosts", []),
             "no package may claim a carrier for host codex; the integration host carries it")
    carrier = bus.get("native_carrier")
    _require(isinstance(carrier, dict), "bus.native_carrier is required")
    _require(carrier.get("host") == "codex", "the native carrier must be the codex host")
    _require(carrier.get("owner") == HOST_ID, f"the native carrier owner must be {HOST_ID!r}")
    _require(carrier.get("invocations_per_turn") == 1, "the native adapter must invoke the chain once per turn")
    _require(isinstance(carrier.get("boundary"), str) and carrier["boundary"], "the native carrier must name its boundary")

    switches = document.get("feature_switches")
    _require(isinstance(switches, list) and switches, "feature_switches are required")
    for switch in switches:
        _require(isinstance(switch.get("name"), str) and switch["name"], "each feature switch needs a name")
        _require("default" in switch, f"feature switch {switch['name']!r} needs a default")
        _require("disable" in switch, f"feature switch {switch['name']!r} needs a disable path")

    event_model = document.get("event_model")
    _require(isinstance(event_model, dict), "event_model is required")
    _require(bool(event_model.get("correlation_fields")), "event_model.correlation_fields are required")
    for producer in event_model.get("produced_by", []):
        _require(producer.get("component") in known, f"event producer {producer.get('component')!r} is unknown")

    ownership = document.get("ownership")
    _require(isinstance(ownership, dict), "ownership is required")
    by_path: dict[str, dict] = {}
    for entry in ownership.get("entries", []):
        path = entry.get("path")
        _require(isinstance(path, str) and path, "each ownership entry needs a path")
        _require(bool(entry.get("writers")), f"ownership entry {path} needs writers")
        for writer in entry["writers"]:
            _require(writer in known, f"ownership entry {path} names unknown writer {writer!r}")
        previous = by_path.get(path)
        if previous is not None:
            _require(previous.get("mode") == "shared" and entry.get("mode") == "shared",
                     f"only shared interfaces may have more than one owner: {path}")
        by_path[path] = entry

    platforms = document.get("supported_platforms")
    _require(isinstance(platforms, list) and platforms, "supported_platforms are required")
    _require(any(p.get("status") == "reference" for p in platforms), "one platform must be the reference platform")
    _require(isinstance(document.get("runtime_requirements"), dict), "runtime_requirements are required")

    lowered = [(path, value) for path, value in _walk_strings(document) if EXCLUDED_ID in value.lower()]
    for path, value in lowered:
        allowed = path.startswith("exclusions") or path.startswith("unsupported_combinations")
        _require(allowed, f"the excluded component is referenced outside the exclusion records: {path} = {value!r}")
    _require(any(value for _, value in lowered), "the excluded component must be recorded with a reason")

    return {
        "schema": SCHEMA,
        "integration_revision": host["revision"],
        "components": [{"id": c["id"], "revision": c["revision"]} for c in components],
        "patch_order": [step["order"] for step in order],
        "stage_order": [priorities[p]["id"] for p in sorted(priorities)],
        "feature_switches": [s["name"] for s in switches],
        "platforms": [p["platform"] for p in platforms],
        "checks": [
            "pins",
            "patch order",
            "stage order and claim disjointness",
            "native carrier ownership",
            "feature switches",
            "event correlation fields",
            "file ownership",
            "exclusion of the out-of-scope component",
        ],
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Mismatch(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(document: dict, host_repo: Path | None, component_dirs: dict[str, Path]) -> dict:
    """Bind the manifest to checkouts on disk."""
    host = document["integration_host"]
    resolved: dict = {"integration_revision": host["revision"], "components": {}, "artifacts": {}, "bus": {}}

    if host_repo is not None:
        head = _git(host_repo, "rev-parse", "HEAD")
        if head != host["revision"]:
            raise Mismatch(f"integration host HEAD is {head}, manifest pins {host['revision']}")
        resolved["host"] = str(host_repo.resolve())

    for component in document["components"]:
        cid = component["id"]
        directory = component_dirs.get(cid)
        if directory is None:
            continue
        directory = Path(directory)
        if not (directory / ".git").exists():
            raise Mismatch(f"{cid}: {directory} is not a git checkout")
        head = _git(directory, "rev-parse", "HEAD")
        if head != component["revision"]:
            raise Mismatch(f"{cid}: HEAD is {head}, manifest pins {component['revision']}")
        resolved["components"][cid] = {"path": str(directory.resolve()), "revision": head}

        artifact = component.get("artifact")
        if artifact is not None:
            artifact_path = directory / artifact["path"]
            if not artifact_path.is_file():
                raise Mismatch(f"{cid}: artifact missing at {artifact_path}")
            digest = _sha256(artifact_path)
            if digest != artifact["sha256"]:
                raise Mismatch(f"{cid}: artifact sha256 is {digest}, manifest records {artifact['sha256']}")
            resolved["artifacts"][cid] = {"path": str(artifact_path), "sha256": digest}
            if host_repo is not None and artifact.get("base_revision") == host["revision"]:
                check = subprocess.run(
                    ["git", "-C", str(host_repo), "apply", "--check", str(artifact_path)],
                    capture_output=True, text=True, check=False,
                )
                if check.returncode != 0:
                    raise Mismatch(f"{cid}: patch does not apply to the pinned host revision: {check.stderr.strip()}")
                resolved["artifacts"][cid]["applies_to_host"] = True

        adapter = component.get("native_adapter")
        if adapter is not None:
            source = directory / adapter["source"]
            if not source.is_file():
                raise Mismatch(f"{cid}: native adapter source missing at {source}")
            resolved["components"][cid]["native_adapter"] = {
                "source": str(source),
                "installs_as": adapter["installs_as"],
            }
            if host_repo is not None:
                for path, expected in adapter.get("expected_blobs", {}).items():
                    observed = _git(host_repo, "rev-parse", f"HEAD:{path}")
                    if observed != expected:
                        raise Mismatch(f"{cid}: {path} is {observed} at the host pin, adapter expects {expected}")
                conflicting = host_repo / adapter["installs_as"]
                if conflicting.exists():
                    raise UnsupportedCombination(f"{cid}: {adapter['installs_as']} already exists in the host checkout")

    fabric = component_dirs.get("jev-context-fabric")
    prune = component_dirs.get("jev-prune-kit")
    if fabric is not None or prune is not None:
        recorded = {entry["sha256"] for entry in document["bus"]["vendored_files"]}
        copies: dict[str, list[str]] = {}
        for label, directory, relative in (
            ("fabric/bus.py", fabric, "src/jev_context/bus.py"),
            ("fabric/bus.mjs", fabric, "adapters/bus.mjs"),
            ("prune/bus.py", prune, "jev_prune/bus.py"),
            ("prune/bus.mjs", prune, "adapters/bus.mjs"),
        ):
            if directory is None:
                continue
            path = Path(directory) / relative
            if not path.is_file():
                raise Mismatch(f"vendored jev-bus file missing: {path}")
            digest = _sha256(path)
            if digest not in recorded:
                raise Mismatch(f"vendored jev-bus file {relative} has sha256 {digest}, which the manifest does not record")
            copies.setdefault(digest, []).append(label)
        for digest, labels in copies.items():
            if len(labels) > 1 and len({label.split("/")[1] for label in labels}) > 1:
                raise Mismatch(f"vendored jev-bus copies disagree: {labels}")
        resolved["bus"] = {label: True for labels in copies.values() for label in labels}

    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "resolve"), nargs="?", default="check")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest to read (defaults to the sibling file)")
    parser.add_argument("--host-repo", type=Path, default=None, help="integration host checkout to bind against")
    parser.add_argument("--component", action="append", default=[], metavar="ID=PATH", help="repeat per component checkout")
    parser.add_argument("--platform", default=None, help="requested platform; unsupported values fail explicitly")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = parser.parse_args(argv)

    path = args.manifest or default_manifest_path()
    try:
        document = load_manifest(path)
        summary = validate(document)
        if args.platform is not None and args.platform not in summary["platforms"]:
            raise UnsupportedCombination(
                f"platform {args.platform!r} is not in the validated matrix {summary['platforms']}; "
                "add it deliberately rather than assuming it works"
            )
        component_dirs: dict[str, Path] = {}
        for item in args.component:
            if "=" not in item:
                raise UnsupportedCombination(f"--component expects ID=PATH, got {item!r}")
            cid, _, location = item.partition("=")
            component_dirs[cid] = Path(location)
        report = {"manifest": str(path.resolve()), "validation": summary}
        if args.command == "resolve":
            report["resolution"] = resolve(document, args.host_repo, component_dirs)
        elif args.host_repo is not None or component_dirs:
            report["resolution"] = resolve(document, args.host_repo, component_dirs)
    except UnsupportedCombination as exc:
        payload = {"ok": False, "kind": "unsupported-combination", "message": str(exc)}
        print(json.dumps(payload, indent=2) if args.json else f"unsupported combination: {exc}", file=sys.stderr)
        return 2
    except Mismatch as exc:
        payload = {"ok": False, "kind": "mismatch", "message": str(exc)}
        print(json.dumps(payload, indent=2) if args.json else f"mismatch: {exc}", file=sys.stderr)
        return 3

    report["ok"] = True
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"manifest OK: {path}")
        print(f"  integration pin: {summary['integration_revision']}")
        for component in summary["components"]:
            print(f"  component: {component['id']} @ {component['revision']}")
        print(f"  patch order: {summary['patch_order']}")
        print(f"  stage order: {summary['stage_order']}")
        if "resolution" in report:
            resolution = report["resolution"]
            print(f"  host checkout: {resolution.get('host', 'not supplied')}")
            for cid, record in resolution["components"].items():
                print(f"  bound: {cid} -> {record['revision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
