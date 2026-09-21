#!/usr/bin/env python3
"""A test double for the pinned fabric installer's public CLI.

The real `jev-context-fabric` installer is a separate repository that is not
available in CI, so the driver's contract - which paths it redirects and which
paths it refuses - is pinned against this stub instead. The stub mirrors the
real installer's surface (`--harness`, `--prefix`, `--workspace`, `--dry-run`,
`--uninstall`) and its JSON report, but it implements none of the real
behaviour. Every run it produces is tier `fabric-stub`, never real evidence.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path


def receipt_path(prefix):
    return prefix / "install-receipt.json"


def planned_files(prefix, home):
    return {
        prefix / "runner.py": "runtime",
        prefix / "generic-mcp.json": "configuration-template",
        home / "config.toml": "configuration",
        home / "hooks.json": "configuration",
        home / ".agents" / "skills" / "jev-context" / "SKILL.md": "skill",
    }


def render_config(path):
    existing = path.read_text("utf-8") if path.exists() else ""
    if "[mcp_servers.jev-context]" in existing:
        return existing
    return (
        existing.rstrip("\n")
        + "\n\n[mcp_servers.jev-context]\n"
        + 'command = "python3"\nargs = ["runner.py", "mcp"]\n'
    )


def install(prefix, workspace, dry_run):
    home = Path(os.environ["HOME"])
    changes = []
    for path, purpose in planned_files(prefix, home).items():
        after = (
            render_config(path).encode("utf-8")
            if path.suffix == ".toml"
            else f"stub:{path.name}\n".encode("utf-8")
        )
        before = path.read_bytes() if path.is_file() else None
        if before == after:
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(after)
        changes.append(
            {
                "path": str(path),
                "action": "create" if before is None else "update",
                "purpose": purpose,
                "original_b64": base64.b64encode(before).decode("ascii")
                if before is not None
                else None,
            }
        )
    if not dry_run:
        receipt_path(prefix).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "files": {
                        change["path"]: {"original_b64": change["original_b64"]}
                        for change in changes
                    },
                }
            ),
            encoding="utf-8",
        )
    return {
        "installed": True,
        "changed_files": len(changes) if not dry_run else 0,
        "prefix": str(prefix),
        "changes": changes,
        "harnesses": [
            {
                "harness": "codex",
                "config_root": str(home),
                "installed_surfaces": ["MCP", "capture-hooks", "prompt-retrieval"],
                "live_runtime_tested": False,
            }
        ],
        "notes": ["fabric-stub: no real behaviour was executed"],
        "workspace": str(workspace),
    }


def uninstall(prefix):
    receipt = receipt_path(prefix)
    if not receipt.is_file():
        return {"uninstalled": False, "reason": "No installation receipt"}
    recorded = json.loads(receipt.read_text("utf-8"))["files"]
    restored = []
    for path_str, item in recorded.items():
        path = Path(path_str)
        if path.is_relative_to(prefix):
            continue
        if item.get("original_b64"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["original_b64"]))
        elif path.is_file():
            path.unlink()
        restored.append(path_str)
    receipt.unlink()
    return {
        "uninstalled": True,
        "restored": restored,
        "conflicts": [],
        "dry_run": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", nargs="+")
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    args.prefix.mkdir(parents=True, exist_ok=True)
    if args.uninstall:
        result = uninstall(args.prefix)
        print(json.dumps(result, indent=2))
        return 0 if result.get("uninstalled") else 2
    result = install(args.prefix, args.workspace or Path.cwd(), args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
