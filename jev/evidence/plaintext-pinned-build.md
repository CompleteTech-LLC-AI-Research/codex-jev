# Evidence: host-driven plaintext collaboration in the pinned build

Tracks the plaintext requirement of [#10](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/10)
and the real-host evidence gap recorded on [#11](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/11).
Companion to the pins in [`compatibility-manifest.json`](../compatibility-manifest.json).

Nothing here is a statement about model accuracy, safety, or performance. It
records what was actually executed, on which revision, and what was not executed.

The earlier revision of this document (`10788ddcec` / patch `be12a6e0…`) is
superseded: it predates the patch landing on `main`, before
`0001` was widened to carry the host's own test and snapshot alignment.

## Revisions

| Item | Value |
| --- | --- |
| Verified revision of this repository | `6b24d07e016e708b0e53c4de76d3983f2b60342f` (`main`) |
| `host.base_commit` pin | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| Patch | `jev/patches/0001-disable-collab-message-encryption.patch`, sha256 `13bc90c385bce43110c2aaeda9333fe7b35f448c5e7ab78736091d68b8de21e7`, 14 targets |
| Manifest digest | `compatibility-manifest.json` sha256 `e9c24cede9f0403a9c78fde7b4ba6be2eeed00c94e62b58f0c352c342af8860f` |
| Toolchain | `rustc 1.95.0 (59807616e 2026-04-14)`, `cargo 1.95.0 (f2d3ce0bd 2026-03-21)`, from `codex-rs/rust-toolchain.toml` |

The manifest digest and the patch digest were re-derived on the verified
revision (`sha256sum` over the checked-in files); the manifest records the same
patch digest, so the executable check and the reviewable artifact agree.

The manifest digest advanced from `ccc41a4b…` (on `017b472be5`) to `e9c24ced…`
because [#13](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/13)
inserted the `capture` stage (`{"order": 0, "id": "capture"}`) into the
manifest's pipeline. That is the only change to the file: the `patches` entry,
its sha256, and every plaintext-relevant pin are identical. The smoke and the
evidence sweep were re-run on `6b24d07e01` with the same three binaries (all
three sha256 values below are unchanged), so the pins still hold on the merged
revision.

## Artifacts exercised

| Role | Binary | sha256 | Patch state |
| --- | --- | --- | --- |
| Patched release build (primary) | `/home/agent/jev/build/target/release/codex` | `d29ed9e0eb3bff45e5035a7f0a6590f59ed6548586273ba813fa7aa2c791a3b3` | `applied` (`0001`) |
| Patched debug build (pooled) | `/home/agent/jev/lead/codex-jev/codex-rs/target/debug/codex` | `6c075b080548a04e6773335a598f7faefa59c4e17ae4df8508837c75bd98fb8d` | `applied` |
| Unpatched pinned base (negative control) | `/home/agent/jev/work/lead/target/debug/codex` | `8b8cc7f78b2ee56336a8976b551d840512f68f5af0dbcd4bfcbee8ab01031f9f` | `absent` |

Release build: the source worktree is at the pinned base `8198a91a4f…` with
`0001` applied to the working tree (`with_encrypted` occurrences in
`multi_agents_spec.rs`: 0; `router.rs` classifies with
`map_or(true, |args| args.is_empty())`, the pin's form). A durable copy of the
same bytes is archived at
`/home/agent/jev/build/artifacts/codex-1.2.0-pre0003rewrite`.

Debug build: recorded in [`ISOLATED_ENV.md`](../ISOLATED_ENV.md) with the same
digest; its behaviour under this smoke (no `encrypted` marker anywhere) is
patched, and that behaviour is the evidence, not the path.

Negative control: a throwaway worktree at the pinned base with the patch list
empty, carrying a `provenance.json` that records `patch_state: "absent"`,
`patches: []`, and every feature `false`.

## What the smoke executes

`jev/smoke/run-plaintext-smoke.sh` runs a **real Codex host process** against a
**loopback Responses-API mock** (`jev/smoke/mock_responses_server.py`) in an
isolated `CODEX_HOME`. No provider is contacted and no credential is read: the
only reachable endpoint is the mock, and the config declares a local
`model_provider` with `request_max_retries = 0`.

The mock scripts a real parent/child collaboration:

1. the parent's first turn asks for a `spawn_agent` call whose `message` is the
   sentinel task text `JEV-SMOKE-PLAINTEXT-TASK-7f3c`;
2. the spawned child agent sends its own request, which carries that plaintext
   task;
3. the parent's post-spawn turn answers once the child has been served.

`jev/smoke/check_plaintext.py` then asserts, from the recorded bodies and the
persisted session rollout:

| # | Assertion |
| --- | --- |
| 1 | All three turns were served (`parent_initial`, `child`, `parent_after_spawn`). |
| 2 | The `message` parameter of `spawn_agent`, `send_message`, and `followup_task` carries no `encrypted` marker, and none of them became a strict schema. |
| 3 | The child's request contains the plaintext task text. |
| 4 | No request carries `encrypted_function_args` or `ciphertext`. |
| 5 | The replayed `spawn_agent` call keeps the `collaboration` namespace. |
| 6 | The persisted rollout stores the readable call and no encrypted marker. |

### Why assertion 5 exists

Collaboration tools are advertised to the model inside a Responses API
namespace (`collaboration`). The tool router dispatches on the namespaced tool
name, so a function call that omits the namespace is refused as
`unsupported call: spawn_agent` and the parent never reaches a child turn. An
earlier revision of this fixture emitted a bare call name and produced a
false green on the outbound bodies alone; the namespace is now asserted so that
failure mode cannot pass silently.

### Why the rendezvous exists

A spawned agent runs concurrently with its parent and the host exits as soon as
the root turn completes, so answering the parent's post-spawn turn immediately
raced the child's request against process exit. The mock now holds that turn
until the child turn has been served (bounded, default 20s), which makes the
child turn observable instead of a coin flip. Before this change the patched
release binary produced no child turn on one of two runs; after it, three
consecutive release runs observed `parent_initial`, `child`, `parent_after_spawn`
every time.

## Results

| Artifact | Turns observed | Checker |
| --- | --- | --- |
| Patched release | `parent_initial`, `child`, `parent_after_spawn` | `ok: true`, exit 0 |
| Patched debug | `parent_initial`, `child`, `parent_after_spawn` | `ok: true`, exit 0 |
| Unpatched pinned base (control) | `parent_initial`, `child`, `parent_after_spawn` | `ok: false`, exit 1 |

The negative control fails for exactly one reason:

```
tool `spawn_agent` still advertises an `encrypted` marker: True
tool `send_message` still advertises an `encrypted` marker: True
tool `followup_task` still advertises an `encrypted` marker: True
```

That is the discriminator: the unpatched host still asks the backend to encrypt
the collaboration `message`, while the patched host advertises it as plaintext.
Both binaries can be driven to a child turn by a mock that sends plaintext, so
the schema marker — not the turn count — is what separates them.

## Fixture self-test (the checker can fail)

`jev/smoke/self_test.py` runs the mock and the checker against synthetic request
bodies and asserts that the checker passes the plaintext shape **and rejects**
the encrypted shape, a missing child turn, and a call that drops the namespace:

```
case plaintext: exit=0 ok=True
case encrypted-schema: exit=1 ok=False failures=[…`encrypted` marker…]
case no-child: exit=1 ok=False failures=[…never served a `child` turn…]
case bare-namespace: exit=1 ok=False failures=[…omitted the `collaboration` namespace…]
case rendezvous: exit=0 turns=['parent_initial', 'child', 'parent_after_spawn']
self-test: passed
```

A checker that cannot fail is not evidence, so the negative controls are part of
the fixture's own test suite. `.github/scripts/test_jev_smoke.py` runs this
self-test inside the existing repository-level Python gate.

## Isolated-profile checks re-run on this revision

The isolated environment added by [#11](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/11)
was re-verified on the same revision against the patched release binary:

| Check | Command | Result |
| --- | --- | --- |
| Environment resolves | `isolated_env.py init --env-dir … --port 18757` | plan written; `remote_inference_enabled: false` |
| Plan matches the binary | `isolated_env.py status` | `binary_matches_plan: true`, `optional_features_enabled: []` |
| Offline launch | `launch_isolated.py -- exec "hello offline world"` | exit 0, reply `jev offline fixture reply`, tier `offline-fixture + real-host-binary` |
| Manifest | `verify-manifest.py` | `ok`, exit 0 |
| Patch state fails closed | `verify-manifest.py --patch-state absent` on the applied tree | `E_PATCH_STATE`, exit 1 |
| Unsupported combination | `unsupported_combination_check.py` | rejected `E_REMOTE_INFERENCE_UNAUTHORIZED` |

The isolated profile deliberately enables **no** Codex-native feature flag, so
the collaboration fixtures there end at `unsupported call`. This smoke is the
complement: it turns `multi_agent_v2` on in its own fixture config so a real
parent/child turn can be observed.

## What was not executed

| Not run | Why |
| --- | --- |
| Live provider | Needs explicit consent and a positive budget; none was configured. The tier never rises above `real-host-binary`. |
| Windows / macOS | The reference platform is Linux x86_64; the harness and the self-test are standard-library Python and are not claimed elsewhere. |
| Full `cargo test` on this revision | Owned by the host-test alignment recorded on [#36](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/36); not re-run here. |
| `codex-code-mode-host` selectors | The `rusty_v8` prebuilt archive 404s in this container (recorded on [#37](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/pull/37)). |

## Environment limits

- `codex-rs/rust-toolchain.toml` pins 1.95.0. Toolchain-sensitive commands must
  run from `codex-rs/`; from the repository root `cargo` picks up the machine
  default (1.97.1), and an empty `RUSTUP_TOOLCHAIN` defeats the override.
- This container's `/tmp` is a full 512 MB tmpfs, so the harness and the
  evidence driver keep scratch under `TMPDIR` instead.
- The pinned builds used an `openssl-sys` `vendored` adaptation because the
  container has no system OpenSSL development files. It is a build-environment
  adaptation in a throwaway worktree, not part of any pin.
- Commits are pushed unsigned (no signing key on this machine) and the only
  collaborator is the automation account, so reviews are recorded self-reviews
  backed by reproducible checks.
