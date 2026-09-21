# Isolated build and integration environment

Phase #3 requires one pinned build that runs without touching an operator's
existing Codex home or sessions. `jev/scripts/isolated_env.py` creates that
environment, `jev/scripts/offline_fixtures.py` provides the deterministic offline
service, and `jev/scripts/launch_isolated.py` runs the pinned binary against
both.

## Build the pinned host

```
cargo build -p codex-cli          # from codex-rs/, the repository's dev recipe
```

`jev/scripts/build_provenance.py build` runs the same recipe, tees the log to
`.jev/build-cli.log`, and records provenance for the produced binary. In this
container the glibc build also needs a static OpenSSL prefix:

```
export OPENSSL_DIR=<prefix> OPENSSL_STATIC=1
```

and Rust test runs need the repository's own stack size, `justfile`
`rust_min_stack`:

```
export RUST_MIN_STACK=8388608
```

## Create the environment

```
python3 jev/scripts/isolated_env.py init            # .jev/isolated
python3 jev/scripts/isolated_env.py status
python3 jev/scripts/isolated_env.py rollback        # moves it to .jev/superseded
```

The environment contains:

| Path | Contents |
| --- | --- |
| `home/config.toml` | Codex home whose provider is the loopback fixture service. No Codex-native feature flag is enabled. |
| `jev-profile.json` | The JEV switches for the chosen profile, plus the `JEV_SWITCH_*` values the launcher exports. |
| `isolated-env.json` | The resolved plan: profile digest, host pin, features, binary, fixture port and catalog. |
| `bin/codex-isolated` | Launcher wrapper. |
| `logs/` | Fixture request record and one JSON line per invocation. |

## Launch

```
.jev/isolated/bin/codex-isolated exec "offline smoke"
python3 jev/scripts/launch_isolated.py --dry-run -- exec "offline smoke"
```

The launcher starts the fixture service on loopback, exports `CODEX_HOME`,
`JEV_ISOLATED_ENV=1`, `JEV_FIXTURE_BASE_URL`, and `JEV_SWITCH_*`, runs the binary,
then records the invocation. `--dry-run` resolves the same plan without a binary,
which is what the required-CI test uses.

## Offline fixtures

`jev/fixtures` holds the deterministic responses the loopback service returns.
Selection is priority-ordered: the lowest `priority` whose `match.body_contains`
appears in the request body wins, and the empty-match `assistant_message`
fixture is the fallback.

| Fixture | Purpose |
| --- | --- |
| `01-tool-call-shell.json` | A shell `function_call`, to exercise the tool router. |
| `02-collab-plaintext.json` | A `spawn_agent` call whose message is plaintext. |
| `03-collab-encrypted.json` | A `spawn_agent` call whose message stays encrypted. |
| `99-assistant-message.json` | Fallback: one deterministic assistant message. |

A targeted fixture answers a bounded number of times (`max_matches`, default 1)
before the request falls through to the fallback, so a fixture conversation
always terminates instead of re-issuing the same tool call forever. The fallback
carries no bound.

The two collaboration fixtures describe tool calls that only exist when the
upstream `collab` feature is enabled: the isolated home deliberately enables no
Codex-native feature flag, so the real binary reports those calls as
`unsupported call` at the router. They are validated by the fixture tests here
and are exercised end to end by the profiles added in later phases, which do
turn the native collaboration tooling on.

## Verified run

A debug build of the pinned host (`codex-rs/target/debug/codex`,
sha256 `6c075b080548a04e6773335a598f7faefa59c4e17ae4df8508837c75bd98fb8d`,
rustc 1.95.0) run through `.jev/isolated/bin/codex-isolated exec` produced:

| Prompt | Fixture sequence | Exit |
| --- | --- | --- |
| `hello offline world` | `assistant_message` | 0 |
| `JEV_FIXTURE_TOOL_CALL ...` | `tool_call_shell` -> `assistant_message` | 0 |
| `JEV_FIXTURE_COLLAB_PLAINTEXT ...` | `collab_plaintext` -> `assistant_message` | 0 |

Stdout for each run is the fixture's `jev offline fixture reply`; the request
sequence is written to `.jev/isolated/logs/fixture-requests.json` and the run
record to `.jev/isolated/logs/invocations.jsonl`.

## Feature switches

Every JEV switch comes from the manifest's `features` map. The `isolated-offline`
profile starts with **every optional switch disabled** - projection, Sentinel,
approval preflight and remote inference are all `false`; only the manifest
defaults (`capture.canonical_evidence`, `retrieval.budgeted_hydration`,
`collab.plaintext_messages`) stay on, because they are local, read-only, and
part of the pinned build. `resolve_plan()` refuses any profile that would enable
an optional switch, so an isolated environment cannot silently start in
enforcement mode.

Switches reach the runtime as `JEV_SWITCH_<NAME>` (`1`/`0`), for example
`JEV_SWITCH_SENTINEL_SHADOW=0`. Later phases read that contract instead of
guessing at upstream Codex flags.

## Fabric runtime and MCP binding

Phase #4 binds the pinned context fabric to this environment with
`jev/scripts/fabric_env.py`. The profile's `fabric` pin (component, revision,
interpreter requirement, harness, MCP server) is cross-checked against the
manifest, and the fabric installer runs only with `HOME`, `CODEX_HOME`,
`XDG_CONFIG_HOME`, `JEV_CONTEXT_HOME` and `JEV_BUS_HOME` redirected into the
environment directory; a reported path outside it fails closed.

```
python3 jev/scripts/fabric_env.py --fabric <fabric-checkout> install
python3 jev/scripts/fabric_env.py status
python3 jev/scripts/fabric_env.py verify
```

The install merges `[mcp_servers.jev-context]` into `home/config.toml`, writes
`home/hooks.json`, adds `home/.agents/skills/jev-context/SKILL.md`, and keeps
the runtime and its SQLite database under `.jev/isolated/fabric`. Unrelated
settings in `config.toml` are preserved, and `uninstall` restores them. See
[FABRIC_BINDING.md](FABRIC_BINDING.md).

## Evidence tiers

| Tier | What it shows |
| --- | --- |
| `offline-fixture` | The fixture service answers a request deterministically, with no clock, randomness, or network. |
| `real-host-binary` | The pinned binary built from this checkout actually runs against that service. |
| live provider | Never used here; remote inference stays disabled and unbudgeted. |

Fixtures are labelled in `jev/fixtures` with `"tier": "offline-fixture"` so no
artifact can be mistaken for live-provider evidence.

## Rollback

See [ROLLBACK.md](ROLLBACK.md). Rollback moves the environment directory into
`.jev/superseded/`; it never deletes anything, and the ambient Codex home is
never read or written by any of these scripts.
