# Binding the context fabric to the isolated workspace

Phase #4 (#12) binds the pinned context fabric to the isolated environment
created in phase #3. `jev/scripts/fabric_env.py` runs the fabric's own additive
installer for the Codex harness, but only against the isolated home, and it
fails closed if the installer would write anywhere else.

## The pin

The runtime that is bound is pinned twice, and the two must agree:

| Where | What it states |
| --- | --- |
| `compatibility-manifest.json` → `components[]` | The `jev-context-fabric` revision, license, interpreter requirement, and the interfaces it provides. |
| `profiles/isolated-offline.json` → `fabric` | The same component and revision, plus the harness (`codex`) and the MCP server name (`jev-context`) this profile installs. |

`jev_manifest.validate_manifest(..., profile=...)` reports
`E_PROFILE_FABRIC_PIN` when the profile pin names an unknown component, a
component that does not provide `memory_tools`, a different revision, or a
different interpreter requirement. `fabric_env.install` re-checks the same
pin against the manifest and refuses to install on any drift, so a profile can
never quietly bind a fabric revision the manifest did not approve.

That is a check on the configuration, not on the code that runs, so the driver
also reads the revision of `--fabric` itself and refuses a standalone checkout
that is not at the pinned commit. A checkout nested inside another work tree
(the in-repo test double) is not a component checkout: `git -C <path> rev-parse
HEAD` would answer for the enclosing repository, so such a path reports no
revision rather than a misleading one, and a path that cannot state its revision
is refused rather than installed anyway: `--allow-unpinned` is the only way to
run one, and the record then says `unpinned` instead of the pin. Without this, a
record could name the pinned revision while an unapproved one was installed,
because the revision in the record came from the manifest rather than from the
checkout.

## Containment

The installer is run with a redirected environment:

| Variable | Redirected to |
| --- | --- |
| `HOME` | `<env-dir>/home` |
| `CODEX_HOME` | `<env-dir>/home` |
| `XDG_CONFIG_HOME` | `<env-dir>/home/xdg` |
| `JEV_CONTEXT_HOME` | `<env-dir>/fabric` |
| `JEV_BUS_HOME` | `<env-dir>/home/.jev/bus` |

Every path the installer reports as changed, together with every reported
harness configuration root, must resolve inside `<env-dir>`. A single escaping
path raises `FabricError` and no record is written, so a partially-applied or
mis-targeted install cannot be mistaken for a successful one.

Inside the isolated home the install surfaces are:

| Path | Surface |
| --- | --- |
| `home/config.toml` → `[mcp_servers.jev-context]` | MCP server entry, merged into the existing config. |
| `home/hooks.json` | Native lifecycle capture hooks. |
| `home/.agents/skills/jev-context/SKILL.md` | Prompt-retrieval skill. |
| `<env-dir>/fabric/` | Fabric runtime, database, and configuration templates. |

The ambient home (`~/.jev-context-fabric`, `~/.codex`) is never created or
modified; `status` reports an ambient-home fingerprint and `verify` asserts the
ambient memory home still does not exist.

## Commands

```
python3 jev/scripts/fabric_env.py --fabric <fabric-checkout> install
python3 jev/scripts/fabric_env.py status
python3 jev/scripts/fabric_env.py verify
python3 jev/scripts/fabric_env.py --fabric <fabric-checkout> uninstall
```

`install` writes `.jev/isolated/fabric-env.json`, which records the pinned
component and revision, the profile, the resolved runtime (harness, MCP server
name, interpreter, interpreter requirement and whether it is satisfied), the
workspace, every changed file, and the checkout that ran - its `revision`,
whether it is `pinned` at the manifest revision, and whether its work tree is
`dirty`. `--dry-run` reports what would change without writing a record or a
file. Re-running `install` is idempotent: a second run reports no changed files.

`status` reports the bound surfaces and whether the MCP entry, hooks, and skill
are still present against the recorded file list. It fails closed when no
binding exists.

`verify` drives the **installed** copy of the fabric runtime (not the checkout)
and asserts six properties:

| Check | Meaning |
| --- | --- |
| `capture_visible_in_own_workspace` | A capture made in workspace A is retrievable in workspace A. |
| `capture_hidden_from_other_workspace` | The same capture is not visible from a separate worktree. |
| `child_session_capture_visible_in_parent_workspace` | A child session's capture lands in the parent workspace. |
| `memory_home_inside_environment` | The SQLite database lives under `<env-dir>/fabric`. |
| `ambient_memory_home_untouched` | `~/.jev-context-fabric` was never created. |
| `status_reports_workspace` | The runtime reports the workspace it was bound to. |

`uninstall` runs the installer's `--uninstall`, which restores the exact
pre-install bytes of every file it changed (including `config.toml`) and
removes the files it created. It applies the same revision rule against the
recorded `component_revision`, so removal logic cannot be run from a checkout
other than the one that installed. The result records the checkout, any
conflicts, and the unrelated settings in `config.toml` survive.

## Disable

The binding is reversible and additive:

1. `fabric_env.py uninstall` restores `config.toml` byte-for-byte, removes
   `hooks.json` and the skill, and reports conflicts.
2. Rolling back the whole environment (`isolated_env.py rollback`) moves
   `.jev/isolated` aside, taking the binding with it.

Neither step touches the ambient Codex home, the ambient session state, or any
WSL distribution.

## Evidence tiers and limits

| Tier | Source |
| --- | --- |
| `real-fabric-installer` | A standalone checkout at the manifest-pinned revision, run against the isolated environment. |
| `unpinned-fabric-checkout` | An installer run under the `--allow-unpinned` opt-in from a path that is not a standalone checkout at the pin, including the in-repo test double. Never evidence that a pinned install ran. |

Required CI (`python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'`)
drives the in-repo double, because the fabric lives in a separate repository
that CI does not check out. Those runs are tier `unpinned-fabric-checkout`, and
they are never evidence that a real install ran. The real-installer results are
recorded on issue #12.

Known limits:

- `install` needs a fabric checkout (`--fabric`); the pinned component is not
  vendored into this repository.
- `uninstall` needs the same checkout, because the removal logic belongs to the
  component rather than the host.
- A checkout nested inside another work tree, or one whose revision cannot be
  read at all (no git metadata, or git unavailable), cannot state which
  revision it is, so it is refused by default: an approval gate that waves
  through a checkout just because it hides its revision is not a gate. Running
  one requires the explicit `--allow-unpinned` opt-in, and the record then
  states `unpinned: true` and `unpinned_allowed: true` under `checkout` instead
  of claiming the pin. Such a run is never pinned-checkout evidence. Required CI
  uses that opt-in for the in-repo double, which by construction has no
  revision of its own.
- An uncommitted work tree is recorded (`dirty: true`) rather than refused, so
  the installer's own `__pycache__` cannot make a second install fail. A dirty
  checkout at the pinned revision is evidence that the pin was read, not that
  the tree was pristine.
- `verify` exercises capture, retrieval, and workspace identity. It does not
  assert that the host's MCP client loads the server, which is phase #5 (#13)
  onwards.
