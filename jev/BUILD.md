# Isolated build and integration profile

This document is the operator guide for building the pinned `codex-jev` fork in
isolation and for disabling the integration. It pairs with
`jev/scripts/build-pinned-codex.sh` and the profiles under `jev/profiles/`.

## What "isolated" means here

Every build runs in a throwaway git worktree checked out at the host base commit
recorded in `compatibility-manifest.json` — never in your working checkout. The
compile runs with a private `CODEX_HOME` so no user configuration, credential, or
session is read. The build directory (default `.jev-build/`, git-ignored) holds
the worktree, the isolated home, the target directory, and the provenance file.

The script never runs `wsl --shutdown`, `wsl --terminate`, or `wsl -t`. It does
not modify an active agent profile, and it only writes inside its build
directory and the worktree it creates.

## Build

```sh
# Integrated build: pinned base + manifest patches, isolated-offline profile.
jev/scripts/build-pinned-codex.sh --verify-fixtures

# Validate the pin, patch state, profile, and fixtures without compiling.
jev/scripts/build-pinned-codex.sh --check --verify-fixtures

# Release binaries.
jev/scripts/build-pinned-codex.sh --release
```

Useful options: `--build-dir DIR`, `--codex-home DIR`, `--target-dir DIR`,
`--reuse`, `--no-smoke`.

After a successful build the script prints the binary and the `CODEX_HOME` to
use, and writes `provenance.json` describing the base commit, the applied
patches and their digests, the profile, the toolchain, and the binary digest.

### Deterministic offline fixtures

The build and its checks never contact a live provider. Fixtures live under
`jev/fixtures/` and are pinned by digest in `jev/fixtures/manifest.json`. Validate
them at any time with:

```sh
python3 jev/scripts/run-offline-fixtures.py
```

The validator is a file check only: it confirms each fixture matches its digest,
that each SSE fixture is well formed and structurally deterministic, and that no
fixture carries a credential marker. Fixtures are labelled at the **offline
fixture** tier and are never presented as live-model evidence.

## Disable the integration (reproduce the original behavior)

The disable path builds the pinned base **without** the manifest patches and runs
the `isolated-build` profile, which explicitly disables every optional feature:

```sh
jev/scripts/build-pinned-codex.sh --no-patches --verify-fixtures
```

`--no-patches` selects `jev/profiles/isolated-build.json` automatically and the
script asserts the patch state is `absent` before compiling. This reproduces the
upstream behavior exactly rather than approximating it: nothing in the tree is
edited to "look disabled".

## Rollback

There is nothing to roll back in your working checkout — the build never touches
it. To remove the isolated artifacts:

```sh
jev/scripts/build-pinned-codex.sh --clean
```

That removes the worktree registered under the build directory and deletes the
build directory. To roll back an individual change, revert the commit on the
integration branch and rebuild; feature switches are reverted through the
profile, and build-time patches through `--no-patches`.

## Container note (Linux without system OpenSSL)

Some CI/containers have no system OpenSSL development files. In that case pass
`--openssl-vendored`, which vendors OpenSSL **inside the throwaway worktree
only**. The edit is confined to `.jev-build/` and is never committed; a reused
worktree carrying that marker is refused so it can never leak into a release.

## Environment limits

- A full compile is heavy; the automated fork CI validates the pin, the patch
  state, the profile, and the fixtures on every change, and performs the full
  isolated compile on demand. See `.github/workflows/jev-integration.yml`.
- Live-provider tests are out of scope for this build. They require explicit
  consent and a positive budget and are tracked as an open blocker in
  `ORCHESTRATION.md`.
