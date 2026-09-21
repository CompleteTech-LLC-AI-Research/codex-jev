#!/usr/bin/env python3
"""Bind the pinned JEV context fabric to the isolated Codex workspace.

The fabric ships an additive installer that writes an MCP server entry, native
lifecycle hooks, a skill, and its own runtime under a prefix. This driver runs
that installer **against the isolated environment only**: ``HOME``,
``CODEX_HOME``, ``XDG_CONFIG_HOME``, ``JEV_CONTEXT_HOME`` and ``JEV_BUS_HOME``
are all redirected into the environment directory, and every path the installer
reports as changed must live inside it. Anything else fails closed.

Subcommands:

``install``   run the pinned fabric installer for the Codex harness;
``status``    report what is installed and whether it is contained;
``verify``    prove memory-home and workspace identity on the installed copy;
``uninstall`` remove the integration and confirm unrelated settings survived.

The runtime that is bound - component, revision, interpreter requirement,
harness, and MCP server name - comes from the ``fabric`` pin in the integration
profile, cross-checked against the manifest component table, so the profile and
the manifest cannot drift apart unnoticed.

Exit codes: 0 = ok, 1 = validation failure, 2 = usage error.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env
import jev_manifest

HARNESS = "codex"
FABRIC_COMPONENT = "jev-context-fabric"
RECORD_NAME = "fabric-env.json"
RECORD_VERSION = 1
SKILL_REL = Path(".agents") / "skills" / "jev-context" / "SKILL.md"
MCP_SERVER_NAME = "jev-context"
REQUIREMENT_RE = re.compile(r"^>=\s*(\d+)\.(\d+)$")


class FabricError(Exception):
    """The fabric cannot be bound to the isolated environment as requested."""


def component_revision(root=None):
    manifest = isolated_env.load_manifest(root)
    for component in manifest["components"]:
        if component["id"] == FABRIC_COMPONENT:
            return component["revision"]
    raise FabricError(f"manifest does not pin {FABRIC_COMPONENT}")


def satisfies_python_requirement(requirement, version_info=None):
    """Check an interpreter against a component's ``>=X.Y`` requirement.

    Only the form the manifest uses is understood; any other spelling fails
    closed instead of being treated as satisfied.
    """
    version = version_info or sys.version_info
    match = REQUIREMENT_RE.match((requirement or "").strip())
    if match is None:
        raise FabricError(f"unsupported python requirement: {requirement!r}")
    return (version[0], version[1]) >= (int(match.group(1)), int(match.group(2)))


def pinned_runtime(plan, root=None):
    """Resolve the profile's fabric pin and cross-check it against the manifest."""
    profile = isolated_env.load_profile(plan["profile"], root)
    pin = profile.get("fabric")
    if not isinstance(pin, dict):
        raise FabricError(f"profile {plan['profile']} does not pin the fabric runtime")
    revision = component_revision(root)
    if pin.get("component") != FABRIC_COMPONENT:
        raise FabricError(
            f"profile {plan['profile']} pins {pin.get('component')!r}, "
            f"not {FABRIC_COMPONENT}"
        )
    if pin.get("revision") != revision:
        raise FabricError(
            f"profile {plan['profile']} pins {FABRIC_COMPONENT}@"
            f"{pin.get('revision')} but the manifest pins {revision}"
        )
    if pin.get("harness") != HARNESS:
        raise FabricError(
            f"profile pins harness {pin.get('harness')!r}, not {HARNESS!r}"
        )
    if pin.get("mcp_server") != MCP_SERVER_NAME:
        raise FabricError(
            f"profile pins MCP server {pin.get('mcp_server')!r}, "
            f"not {MCP_SERVER_NAME!r}"
        )
    if not satisfies_python_requirement(pin.get("python_requirement")):
        raise FabricError(
            f"the pinned fabric needs python {pin.get('python_requirement')}, but "
            f"this interpreter is {'.'.join(str(p) for p in sys.version_info[:3])}"
        )
    return pin, revision


def fabric_prefix(env_dir):
    return Path(env_dir) / "fabric"


def record_path(env_dir):
    return Path(env_dir) / RECORD_NAME


def read_record(env_dir):
    path = record_path(env_dir)
    if not path.is_file():
        raise FabricError(f"no fabric binding at {env_dir}; run `install` first")
    return json.loads(path.read_text(encoding="utf-8"))


def child_env(plan, prefix):
    """Environment that keeps every fabric write inside the isolated directory."""
    home = Path(plan["home"])
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "xdg")
    env["JEV_CONTEXT_HOME"] = str(prefix)
    env["JEV_BUS_HOME"] = str(home / ".jev" / "bus")
    return env


def ensure_contained(paths, env_dir):
    """Fail closed if the installer reports a path outside the environment."""
    env_dir = Path(env_dir).resolve()
    outside = []
    for raw in paths:
        path = Path(raw)
        try:
            path.resolve().relative_to(env_dir)
        except ValueError:
            outside.append(str(path))
    if outside:
        raise FabricError(
            "the fabric installer would write outside the isolated environment: "
            + ", ".join(sorted(outside))
        )


def run_installer(fabric_root, argv, env):
    entry = Path(fabric_root) / "install.py"
    if not entry.is_file():
        raise FabricError(f"fabric installer not found: {entry}")
    completed = subprocess.run(
        [sys.executable, str(entry), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode not in (0, 2):
        raise FabricError(
            f"fabric installer exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FabricError(f"fabric installer did not report JSON: {error}") from error


def install(env_dir, fabric_root, workspace=None, dry_run=False, root=None):
    env_dir = Path(env_dir)
    plan = isolated_env.read_env(env_dir)
    pin, revision = pinned_runtime(plan, root)
    prefix = fabric_prefix(env_dir)
    workspace = (
        Path(workspace).resolve()
        if workspace
        else Path(root or isolated_env.repository_root()).resolve()
    )
    if not workspace.is_dir():
        raise FabricError(f"workspace must be an existing directory: {workspace}")
    argv = [
        "--harness",
        HARNESS,
        "--prefix",
        str(prefix),
        "--workspace",
        str(workspace),
    ]
    if dry_run:
        argv.append("--dry-run")
    env = child_env(plan, prefix)
    result = run_installer(fabric_root, argv, env)
    changed = [change.get("path") for change in result.get("changes", [])]
    ensure_contained(changed, env_dir)
    roots = [harness.get("config_root") for harness in result.get("harnesses", [])]
    ensure_contained([entry for entry in roots if entry], env_dir)
    record = {
        "record_version": RECORD_VERSION,
        "component": FABRIC_COMPONENT,
        "component_revision": revision,
        "profile": plan["profile"],
        "runtime": {
            "component": pin["component"],
            "revision": revision,
            "python_requirement": pin["python_requirement"],
            "harness": pin["harness"],
            "mcp_server": pin["mcp_server"],
            "interpreter": sys.executable,
            "interpreter_version": ".".join(str(p) for p in sys.version_info[:3]),
            "interpreter_satisfies_requirement": satisfies_python_requirement(
                pin["python_requirement"]
            ),
        },
        "harness": HARNESS,
        "prefix": str(prefix),
        "workspace": str(workspace),
        "codex_home": plan["home"],
        "contained": True,
        "dry_run": bool(dry_run),
        "changed_files": sorted(changed),
        "config_roots": sorted(entry for entry in roots if entry),
        "installed_surfaces": sorted(
            {
                surface
                for harness in result.get("harnesses", [])
                for surface in harness.get("installed_surfaces", [])
            }
        ),
        "notes": result.get("notes", []),
        "tier": "real-fabric-installer",
        "recorded_at_unix_ms": int(time.time() * 1000),
    }
    if not dry_run:
        record_path(env_dir).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return record


def codex_config(plan):
    return Path(plan["home"]) / "config.toml"


def status(env_dir):
    env_dir = Path(env_dir)
    record = read_record(env_dir)
    plan = isolated_env.read_env(env_dir)
    home = Path(plan["home"])
    config = codex_config(plan)
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    hooks = home / "hooks.json"
    skill = home / SKILL_REL
    present = [Path(path) for path in record["changed_files"] if Path(path).is_file()]
    return {
        "env_dir": str(env_dir),
        "prefix": record["prefix"],
        "workspace": record["workspace"],
        "profile": record.get("profile"),
        "component_revision": record["component_revision"],
        "runtime": record.get("runtime", {}),
        "mcp_server_in_config": f"[mcp_servers.{MCP_SERVER_NAME}]" in text,
        "hooks_present": hooks.is_file(),
        "skill_present": skill.is_file(),
        "changed_files_present": len(present),
        "changed_files_recorded": len(record["changed_files"]),
        "installed_surfaces": record["installed_surfaces"],
        "ambient_home": isolated_env.ambient_home_fingerprint(),
    }


def fabric_cli(record, plan, argv, workspace=None, env=None):
    """Run the *installed* runner, so verification exercises the installed copy."""
    runner = Path(record["prefix"]) / "runner.py"
    if not runner.is_file():
        raise FabricError(f"installed fabric runner is missing: {runner}")
    workspace = workspace or record["workspace"]
    command = [
        sys.executable,
        str(runner),
        "--home",
        record["prefix"],
        "--workspace",
        str(workspace),
        *argv,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env or child_env(plan, Path(record["prefix"])),
        check=False,
    )
    if completed.returncode != 0:
        raise FabricError(
            f"fabric command failed ({argv}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FabricError(f"fabric command did not report JSON: {error}") from error


def verify(env_dir, workspace_a=None, workspace_b=None):
    env_dir = Path(env_dir)
    record = read_record(env_dir)
    plan = isolated_env.read_env(env_dir)
    marker_a = f"jev-fabric-marker-a-{int(time.time())}"
    marker_b = f"jev-fabric-marker-b-{int(time.time())}"
    work_a = Path(workspace_a).resolve() if workspace_a else Path(record["workspace"])
    work_b = (
        Path(workspace_b).resolve()
        if workspace_b
        else work_a.parent / "second-worktree"
    )
    if work_b == work_a:
        raise FabricError(
            "verify needs two distinct workspaces; "
            f"{work_a} was used for both (pass --workspace-b)"
        )
    work_b.mkdir(parents=True, exist_ok=True)

    fabric_cli(
        record,
        plan,
        ["capture", "--text", marker_a, "--origin", "verify"],
        workspace=work_a,
    )
    fabric_cli(
        record,
        plan,
        ["capture", "--text", marker_b, "--origin", "verify"],
        workspace=work_b,
    )
    fabric_cli(
        record,
        plan,
        [
            "capture",
            "--text",
            f"{marker_a}-child",
            "--origin",
            "verify",
            "--session",
            "child",
        ],
        workspace=work_a,
    )

    seen_a = fabric_cli(record, plan, ["retrieve", marker_a], workspace=work_a)
    seen_b = fabric_cli(record, plan, ["retrieve", marker_a], workspace=work_b)
    child = fabric_cli(
        record, plan, ["retrieve", f"{marker_a}-child"], workspace=work_a
    )
    status_a = fabric_cli(record, plan, ["status"], workspace=work_a)

    database = Path(record["prefix"]) / "memory.sqlite3"
    ambient_home = Path(os.path.expanduser("~")) / ".jev-context-fabric"
    checks = {
        "capture_visible_in_own_workspace": marker_a in json.dumps(seen_a),
        "capture_hidden_from_other_workspace": marker_a not in json.dumps(seen_b),
        "child_session_capture_visible_in_parent_workspace": f"{marker_a}-child"
        in json.dumps(child),
        "memory_home_inside_environment": database.is_file()
        and str(database).startswith(str(env_dir)),
        "ambient_memory_home_untouched": not ambient_home.exists(),
        "status_reports_workspace": str(work_a) in json.dumps(status_a),
    }
    return {
        "record_version": RECORD_VERSION,
        "env_dir": str(env_dir),
        "workspaces": {"primary": str(work_a), "secondary": str(work_b)},
        "memory_home": str(Path(record["prefix"])),
        "database": str(database),
        "checks": checks,
        "ok": all(checks.values()),
    }


def uninstall(env_dir, fabric_root=None, root=None):
    env_dir = Path(env_dir)
    record = read_record(env_dir)
    plan = isolated_env.read_env(env_dir)
    prefix = Path(record["prefix"])
    entry = Path(fabric_root) / "install.py" if fabric_root else None
    if entry is None or not entry.is_file():
        raise FabricError("uninstall needs --fabric pointing at the fabric checkout")
    result = run_installer(
        fabric_root, ["--uninstall", "--prefix", str(prefix)], child_env(plan, prefix)
    )
    record["uninstalled_at_unix_ms"] = int(time.time() * 1000)
    record["uninstall"] = {
        "uninstalled": result.get("uninstalled"),
        "restored": sorted(result.get("restored", [])),
        "conflicts": sorted(
            item.get("path", "") for item in result.get("conflicts", [])
        ),
    }
    record_path(env_dir).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record["uninstall"]


def parse_args(argv):
    # Abbreviations would let `verify --workspace X` silently mean
    # `--workspace-b X`, which reads as if it changed the primary workspace.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--env-dir", default=None)
    parser.add_argument("--fabric", default=None, help="fabric checkout root")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", allow_abbrev=False)
    install_parser.add_argument("--dry-run", action="store_true")
    verify_parser = sub.add_parser("verify", allow_abbrev=False)
    verify_parser.add_argument("--workspace-b", default=None)
    sub.add_parser("status", allow_abbrev=False)
    sub.add_parser("uninstall", allow_abbrev=False)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    # Resolve once, up front: containment is decided by comparing absolute
    # paths, so a relative --env-dir must not depend on the caller's cwd.
    env_dir = (
        Path(args.env_dir) if args.env_dir else isolated_env.default_env_dir(args.root)
    ).resolve()
    try:
        if args.command == "install":
            result = install(
                env_dir,
                args.fabric,
                workspace=args.workspace,
                dry_run=args.dry_run,
                root=args.root,
            )
        elif args.command == "status":
            result = status(env_dir)
        elif args.command == "verify":
            result = verify(
                env_dir, workspace_a=args.workspace, workspace_b=args.workspace_b
            )
            if not result["ok"]:
                print(json.dumps(result, indent=2, sort_keys=True))
                return 1
        else:
            result = uninstall(env_dir, fabric_root=args.fabric, root=args.root)
    except (FabricError, isolated_env.EnvError, jev_manifest.ManifestError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
