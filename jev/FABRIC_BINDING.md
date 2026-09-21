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
workspace, and every changed file. `--dry-run` reports what would change
without writing a record or a file. Re-running `install` is idempotent: a
second run reports no changed files, and the capture handlers are not added
twice.

## Capture ordering

Canonical capture has to happen before any component projects context into an
outgoing request, so `install` also binds `jev/scripts/capture_hook.py` as the
**first** handler for every captured lifecycle event. The adapter's groups are
prepended to `home/hooks.json`, ahead of the groups the fabric installer wrote,
and every existing group is preserved exactly as it was (order included).
`uninstall` removes only the adapter's groups.

`hooks.json` is treated as owned by the fabric installer: a file that is not
JSON, or not a JSON object, is never rewritten. Bind raises `FabricError`,
install writes no record, and the file keeps its exact bytes; `status` reports
the problem under `capture.hooks_error` instead of failing. See
[CANONICAL_CAPTURE.md](CANONICAL_CAPTURE.md) for the envelope schema, the
correlation rules, and the host hook-trust gate that must be satisfied before a
real host will invoke the adapter.

`status` reports the bound surfaces and whether the MCP entry, hooks, and skill
are still present against the recorded file list. It fails closed when no
binding exists.

`verify` drives the **installed** copy of the fabric runtime (not the checkout)
and asserts six fabric properties and five host-capture properties:

| Check | Meaning |
| --- | --- |
| `capture_visible_in_own_workspace` | A capture made in workspace A is retrievable in workspace A. |
| `capture_hidden_from_other_workspace` | The same capture is not visible from a separate worktree. |
| `child_session_capture_visible_in_parent_workspace` | A child session's capture lands in the parent workspace. |
| `memory_home_inside_environment` | The SQLite database lives under `<env-dir>/fabric`. |
| `ambient_memory_home_untouched` | `~/.jev-context-fabric` was never created. |
| `status_reports_workspace` | The runtime reports the workspace it was bound to. |
| `host_capture_records_every_kind` | Replaying the bound adapter records one envelope per captured kind, in order. |
| `host_capture_replay_is_deduplicated` | A replayed delivery adds no record. |
| `host_capture_resolves_correlation` | A result resolves to its call and a message to its prompt. |
| `host_capture_stream_conforms` | The capture stream validates with no errors. |
| `host_capture_has_no_gaps` | No missing-call or missing-prompt gap is reported. |

`uninstall` runs the installer's `--uninstall`, which restores the exact
pre-install bytes of every file it changed (including `config.toml`) and
removes the files it created. The result records any conflicts, and the
unrelated settings in `config.toml` survive.

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
| `real-fabric-installer` | The pinned `jev-context-fabric` checkout, run against the isolated environment. |
| `fabric-stub` | `jev/tests/fabric_stub/install.py`, a checked-in test double used by required CI, which does not have the component checkout. |

Required CI (`python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'`)
drives the stub, because the fabric lives in a separate repository that CI does
not check out. Stub results are never evidence that a real install ran. The
real-installer results are recorded on issue #12.

Known limits:

- `install` needs a fabric checkout (`--fabric`); the pinned component is not
  vendored into this repository.
- `uninstall` needs the same checkout, because the removal logic belongs to the
  component rather than the host.
- `verify` exercises capture, retrieval, workspace identity, and the bound
  capture adapter. It does not assert that the host's MCP client loads the
  server, or that a live session invokes the adapter; both need a real host
  run (phase #8, #25).
- The host runs a `hooks.json` handler only when the handler is trusted
  (`hook_trust_status`); `hooks.json` starts `Untrusted`, so a real host will
  not invoke the capture adapter until that trust is provisioned or the
  launcher passes `--dangerously-bypass-hook-trust`. This is documented in
  [CANONICAL_CAPTURE.md](CANONICAL_CAPTURE.md) and tracked as live-host
  validation, not worked around here.
