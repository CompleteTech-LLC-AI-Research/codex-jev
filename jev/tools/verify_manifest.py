#!/usr/bin/env python3
"""Resolve a checkout against the JEV compatibility manifest.

The manifest is the single place that records pins, runtimes, platforms, feature
switches, and the combinations this integration refuses. This tool turns those
records into an explicit decision:

* exit 0 - the checkout resolves to the pinned inputs
* exit 2 - a named unsupported combination matched (the request is refused)
* exit 3 - the manifest or a required input is missing or malformed

There is no override flag. A refused combination is fixed by changing the
manifest through review, not by passing an argument here.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

EXIT_OK = 0
EXIT_UNSUPPORTED = 2
EXIT_MANIFEST_ERROR = 3

DEFAULT_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "manifest.json"


class ManifestError(Exception):
    """The manifest or a required input could not be resolved."""


def load_manifest(path: pathlib.Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError("cannot read manifest at {}: {}".format(path, error))
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManifestError("manifest at {} is not valid JSON: {}".format(path, error))
    if not isinstance(manifest, dict):
        raise ManifestError("manifest at {} must be a JSON object".format(path))
    for key in ("host", "components", "feature_switches", "unsupported_combinations"):
        if key not in manifest:
            raise ManifestError("manifest at {} is missing '{}'".format(path, key))
    return manifest


def parse_version(value: str) -> tuple:
    parts = re.findall(r"\d+", str(value))
    if not parts:
        raise ManifestError("cannot parse version '{}'".format(value))
    return tuple(int(part) for part in parts)


def compare_python(requirement: str, actual: str) -> bool:
    """Return True when ``actual`` satisfies a requirement such as '>=3.11'."""
    match = re.fullmatch(r"\s*(>=|>|<=|<|==)\s*([0-9][0-9.]*)\s*", requirement)
    if not match:
        raise ManifestError("unsupported runtime requirement '{}'".format(requirement))
    operator, wanted = match.group(1), match.group(2)
    actual_parts = parse_version(actual)
    wanted_parts = parse_version(wanted)
    size = max(len(actual_parts), len(wanted_parts))
    actual_parts = actual_parts + (0,) * (size - len(actual_parts))
    wanted_parts = wanted_parts + (0,) * (size - len(wanted_parts))
    if operator == ">=":
        return actual_parts >= wanted_parts
    if operator == ">":
        return actual_parts > wanted_parts
    if operator == "<=":
        return actual_parts <= wanted_parts
    if operator == "<":
        return actual_parts < wanted_parts
    return actual_parts == wanted_parts


def git_revision(checkout: pathlib.Path) -> str:
    try:
        output = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ManifestError(
            "cannot read a revision from {}: {}".format(checkout, error)
        )
    return output.stdout.strip()


def default_flags(manifest: dict) -> dict:
    flags = {switch["id"]: switch["default"] for switch in manifest["feature_switches"]}
    for switch in manifest.get("remote_controls", {}).get("switches", []):
        flags[switch["id"]] = switch["default"]
    return flags


def default_state() -> dict:
    return {
        "jev.approval.shadow_evaluated": False,
        "jev.sentinel.coverage_confirmed": False,
    }


def _rule_matches(rule: dict, request: dict) -> bool:
    """Return True when every condition in ``rule['match']`` holds for ``request``."""
    match = rule.get("match", {})
    if "provider" in match:
        if request["provider"] != match["provider"]:
            return False
    for flag, expected in match.get("flags", {}).items():
        if request["flags"].get(flag) != expected:
            return False
    if "env_absent" in match and match["env_absent"] in request["env"]:
        return False
    for name, expected in match.get("state", {}).items():
        if request["state"].get(name) != expected:
            return False
    if "runtime" in match:
        for name, requirement in match["runtime"].items():
            if name != "python":
                raise ManifestError("unsupported runtime name '{}'".format(name))
            # The rule refuses a runtime that does *not* satisfy the stated
            # requirement: a satisfied requirement means "no violation".
            if compare_python(requirement, request["python_version"]):
                return False
    if "platform" in match:
        if match["platform"] == "not-in-supported_platforms":
            ids = [
                entry["id"]
                for entry in request["manifest"].get("supported_platforms", [])
            ]
            if request["platform"] in ids:
                return False
        elif request["platform"] != match["platform"]:
            return False
    if "checkout" in match:
        for name, expected in match["checkout"].items():
            if name == "host_revision":
                if expected != "not-equal-to-host.revision":
                    raise ManifestError(
                        "unsupported checkout matcher '{}'".format(expected)
                    )
                if request["host_revision"] == request["manifest"]["host"]["revision"]:
                    return False
            elif name == "component_revision":
                if expected != "not-equal-to-components[].revision":
                    raise ManifestError(
                        "unsupported checkout matcher '{}'".format(expected)
                    )
                if not _has_component_drift(request):
                    return False
            else:
                raise ManifestError("unknown checkout matcher '{}'".format(name))
    return True


def _has_component_drift(request: dict) -> bool:
    pinned = {
        entry["name"]: entry["revision"] for entry in request["manifest"]["components"]
    }
    for name, revision in request["component_revisions"].items():
        if name in pinned and pinned[name] != revision:
            return True
    return False


def evaluate(manifest: dict, request: dict) -> list:
    """Return the list of violation messages; an empty list means the request resolves."""
    violations = []

    host = manifest["host"]
    if request["host_revision"] != host["revision"]:
        violations.append(
            "host revision mismatch: checkout is {} but the manifest pins {}".format(
                request["host_revision"], host["revision"]
            )
        )

    pinned = {entry["name"]: entry["revision"] for entry in manifest["components"]}
    for name, revision in sorted(request["component_revisions"].items()):
        if name not in pinned:
            violations.append(
                "unknown component '{}' is not in the manifest".format(name)
            )
        elif pinned[name] != revision:
            violations.append(
                "component revision mismatch for {}: installed {} but the manifest pins {}".format(
                    name, revision, pinned[name]
                )
            )

    excluded = {entry["component"] for entry in manifest.get("excluded", [])}
    for name in sorted(request["component_revisions"]):
        if name in excluded:
            violations.append(
                "excluded component '{}' must not be part of the integration".format(
                    name
                )
            )
    if request["provider"] in {entry["id"] for entry in manifest.get("excluded", [])}:
        violations.append(
            "excluded provider '{}' must not be selected".format(request["provider"])
        )

    platform_ids = [entry["id"] for entry in manifest.get("supported_platforms", [])]
    if request["platform"] not in platform_ids:
        violations.append(
            "platform '{}' is not in the supported matrix {}".format(
                request["platform"], platform_ids
            )
        )

    python_requirement = (
        manifest.get("runtime_requirements", {}).get("python", {}).get("minimum")
    )
    if python_requirement and not compare_python(
        ">={}".format(python_requirement), request["python_version"]
    ):
        violations.append(
            "python {} does not satisfy the manifest minimum {}".format(
                request["python_version"], python_requirement
            )
        )

    switches = {switch["id"]: switch for switch in manifest["feature_switches"]}
    for switch_id, switch in switches.items():
        value = request["flags"].get(switch_id, switch["default"])
        if switch_id not in request["flags"]:
            continue
        if value == switch["default"]:
            continue
        if "values" in switch:
            allowed = list(switch["values"])
            if value not in allowed:
                violations.append(
                    "{}={} is not one of {}".format(switch_id, value, allowed)
                )
                continue
        for required in switch.get("requires", []):
            required_value = request["flags"].get(
                required, switches[required]["default"]
            )
            if required_value != True:  # noqa: E712 - manifest booleans are JSON true
                violations.append(
                    "{} is enabled but its requirement {} is not".format(
                        switch_id, required
                    )
                )

    for rule in manifest["unsupported_combinations"]:
        if _rule_matches(rule, request):
            violations.append("{}: {}".format(rule["id"], rule["message"]))

    return violations


def build_request(manifest: dict, args) -> dict:
    flags = default_flags(manifest)
    for assignment in args.enable or []:
        if "=" not in assignment:
            raise ManifestError(
                "--enable expects NAME=VALUE, got '{}'".format(assignment)
            )
        name, raw = assignment.split("=", 1)
        name = name.strip()
        if name not in flags:
            raise ManifestError("'{}' is not a manifest feature switch".format(name))
        flags[name] = _coerce(raw.strip(), flags[name])

    env = dict(_parse_pairs(args.env or [], "--env"))
    state = default_state()
    state.update(
        {
            key: _coerce(value, None)
            for key, value in _parse_pairs(args.state or [], "--state").items()
        }
    )

    component_revisions = {}
    for entry in manifest["components"]:
        installed = args.components_root / entry["name"]
        if installed.is_dir():
            component_revisions[entry["name"]] = git_revision(installed)

    return {
        "manifest": manifest,
        "flags": flags,
        "env": env,
        "state": state,
        "provider": args.provider,
        "platform": args.platform,
        "python_version": args.python_version,
        "host_revision": git_revision(args.checkout)
        if args.checkout
        else manifest["host"]["revision"],
        "component_revisions": component_revisions,
    }


def _parse_pairs(pairs, flag: str) -> dict:
    parsed = {}
    for pair in pairs:
        if "=" not in pair:
            raise ManifestError("{} expects NAME=VALUE, got '{}'".format(flag, pair))
        name, value = pair.split("=", 1)
        parsed[name.strip()] = value.strip()
    return parsed


def _coerce(raw: str, like):
    if isinstance(like, bool):
        lowered = raw.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ManifestError("cannot read '{}' as a boolean".format(raw))
    return raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--checkout", help="host checkout whose HEAD must match the pin"
    )
    parser.add_argument(
        "--components-root",
        default=".",
        help="directory holding component checkouts named after manifest components",
    )
    parser.add_argument("--enable", action="append", metavar="NAME=VALUE")
    parser.add_argument("--env", action="append", metavar="NAME=VALUE")
    parser.add_argument("--state", action="append", metavar="NAME=VALUE")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--platform", default="linux-x86_64-gnu")
    parser.add_argument(
        "--python-version",
        default="{}.{}.{}".format(*sys.version_info[:3]),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the resolution as JSON"
    )
    args = parser.parse_args(argv)

    components_root = pathlib.Path(args.components_root)
    if not components_root.is_absolute():
        components_root = pathlib.Path.cwd() / components_root
    args.components_root = components_root

    try:
        manifest = load_manifest(pathlib.Path(args.manifest))
        if args.checkout:
            args.checkout = pathlib.Path(args.checkout).resolve()
        request = build_request(manifest, args)
    except ManifestError as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        else:
            print("manifest error: {}".format(error), file=sys.stderr)
        return EXIT_MANIFEST_ERROR

    violations = evaluate(manifest, request)
    summary = {
        "ok": not violations,
        "host_revision": request["host_revision"],
        "pinned_host_revision": manifest["host"]["revision"],
        "platform": request["platform"],
        "python_version": request["python_version"],
        "components": request["component_revisions"],
        "enabled_switches": {
            key: value
            for key, value in request["flags"].items()
            if value != default_flags(manifest)[key]
        },
        "violations": violations,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif violations:
        for violation in violations:
            print("refused: {}".format(violation), file=sys.stderr)
    else:
        print(
            "ok: host {} on {} resolves against {}".format(
                request["host_revision"][:12],
                request["platform"],
                pathlib.Path(args.manifest).name,
            )
        )
    return EXIT_OK if not violations else EXIT_UNSUPPORTED


if __name__ == "__main__":
    sys.exit(main())
