# Build environment record

Recorded from the reference integration environment. This file describes what was
observed, not what is guaranteed.

## Reference environment

| Item | Observed value |
|:--|:--|
| Platform | Linux (container), `x86_64` |
| Rust | `rustc 1.97.1`, `cargo 1.97.1` |
| `just` | 1.58.0 |
| Python | 3.11.2 |
| Node | v24.16.0 |
| CPUs / RAM | 12 / 125 GB |
| Workspace | isolated checkouts under `/home/agent/jev/work`, isolated `CODEX_HOME` |

## Build recipe (as executed)

```sh
git worktree add <worktree> -b <branch> <integration-pin>
git apply <codex-plaintext-collab>/patches/0001-disable-collab-message-encryption.patch
cd <worktree>/codex-rs
CARGO_TARGET_DIR=<shared target> cargo build --release --bin codex
```

The patch applies to the integration pin with context offsets only (no fuzz, no
rejection); `verify_manifest.py resolve` re-checks that applicability on every run.

## Environment adaptations (not component pins)

**Vendored OpenSSL.** The reference environment has no system OpenSSL development files
(`pkg-config --exists openssl` fails, `/usr/include/openssl` is absent) and no package
manager or privilege to install them. Several dependencies link `native-tls`/OpenSSL, so a
plain `cargo build` fails in `openssl-sys`. The documented fallback in
`codex-plaintext-collab` is to force the vendored build:

```toml
[dependencies.openssl]
version = "0.10"
features = ["vendored"]
```

This is a build-environment adaptation for platforms without OpenSSL development files.
It is applied to the build worktree only and is **not** part of any component pin; the
manifest records it under `runtime_requirements.platform_note`. A platform that already
has OpenSSL development files does not need it.

## Evidence labels

Every result in this integration is labeled:

- `offline-fixture` — deterministic local tests and fixtures, no host and no model.
- `real-host` — the built binary or a host process actually ran.
- `live-provider` — a paid model/provider call. Requires explicit consent and a budget,
  and is not performed by default.

A fixture result is never reported as live model accuracy, safety, or performance.

## Known limitations

- Commit signing is not configured in this environment: no `gpg` binary, no
  `gpg.format = ssh`, and no signing key registered on the account. Commits are therefore
  unsigned here. No key was created and no signing configuration was weakened.
- `codex-rs` release builds are heavy: a shared `CARGO_TARGET_DIR` is used across
  worktrees to stay inside the available disk.
- WSL shutdown or termination is never used, and other sessions are never interrupted.
