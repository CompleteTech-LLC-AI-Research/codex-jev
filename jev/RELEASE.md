# Release package, upgrade, and rollback

Phase 6.3 ([#26](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/26))
packages the integration: what ships, in what order it installs, who owns each
piece of state, how to upgrade it, how to get back, which platforms were
actually exercised, and which gates a release claim has to pass.

The rule this document exists to enforce is the phase's own: **release readiness
follows actual evidence, not issue completion.** So the gate table below has
rows that are *open*, and the artifact this phase produces is a **candidate**,
not a release.

## What ships

`jev/scripts/release_artifact.py` selects the integration package from the
**tracked** files of a checkout:

| Included | Why |
| --- | --- |
| `jev/**` (manifest, profiles, patches, fixtures, scripts, tests, smoke, docs, evidence) | The integration host owns this tree. |
| `codex-rs/core/src/jev_bus.rs`, `codex-rs/core/src/jev_bus_tests.rs`, `codex-rs/core/src/guardian/jev.rs` | The native modules the host itself compiles: the request boundary and the approval adapter. |
| `.github/scripts/test_jev_*.py` | The required-CI harnesses that validate the package. |

Two rules keep credentials and private data out, and both fail **closed**:

1. **Structural.** Only git-tracked paths are eligible, so the isolated
   environment (`.jev/`), build output (`codex-rs/target/`), component
   checkouts, `__pycache__`, and any untracked rollout, log, or credential
   cannot enter the archive by accident.
2. **Content.** Every member is scanned for provider-key, private-key, header,
   and JWT shapes. A match with no synthetic marker is a refusal
   (`E_ARTIFACT_CREDENTIAL`), and a deny-list of credential-shaped names
   (`.env*`, `auth.json`, `*.pem`, `*.key`, `id_rsa*`, `.netrc`, ...) is a
   refusal (`E_ARTIFACT_FORBIDDEN`).

The scan has exactly one escape hatch, and it is per-finding rather than
per-path: a match is allowed only when the **matched text itself** carries a
synthetic marker (`FIXTURE`, `NOT-A-REAL`, `EXAMPLE`, `AAAA`, ...). The fixtures
that exist to prove the redaction path (`jev/tests/capture_fixtures/`) and the
two required-CI harnesses that plant a fake key are the only things that trip
it. Every allowance is named in the record with its path, digest, pattern, and
occurrence count, and `verify` recomputes the allowance set - so no directory is
blanket-exempt and a waiver cannot be widened silently.

Length thresholds are deliberately conservative: only a realistically long
value is treated as a credential, because a short illustrative token in a
document is not one. Provider-prefixed keys are the primary signal.

## The candidate artifact

```sh
# build (defaults to .jev/dist/codex-jev-candidate.tar.gz)
python3 jev/scripts/release_artifact.py build --record-out .jev/dist/release-record.json

# verify it, optionally against the revision it must have come from
python3 jev/scripts/release_artifact.py verify .jev/dist/codex-jev-candidate.tar.gz \
  --expect-revision <sha>
```

The archive is reproducible: members are sorted, tar metadata is zeroed, the
gzip header carries `mtime=0`, and the record holds no timestamp, so the same
revision produces the same bytes.

`jev/release-record.json` - inside the archive - pins the source revision, the
host base commit, the toolchain and Python requirements, every interface
version, the ordered patch digests, the component revisions with their Python
requirements, the profile files, the feature defaults, the fixture catalog
digest, the manifest's own digest, and the sha256 of every member except the
record itself.

`verify` asserts, and fails closed on each:

| Code | Meaning |
| --- | --- |
| `E_ARTIFACT_MISSING` | A required member is absent. |
| `E_ARTIFACT_FORBIDDEN` | A member matches the credential/state deny-list. |
| `E_ARTIFACT_CREDENTIAL` | A member's bytes contain secret-shaped content with no synthetic marker. |
| `E_ARTIFACT_EXEMPTION` | A synthetic allowance is undeclared, unearned, or outside the declared markers. |
| `E_ARTIFACT_DIGEST` | A member does not match the digest the record pins, or the pinned set differs from the archive. |
| `E_ARTIFACT_MANIFEST` | The record's pins disagree with the manifest the archive itself carries. |
| `E_ARTIFACT_REVISION` | The artifact was built from a different revision than the caller expected. |
| `E_ARTIFACT_UNREADABLE` / `E_ARTIFACT_MEMBER` | The archive cannot be read, or a member is not a regular file. |

What it deliberately does **not** do: it is not a signature, not an SBOM, and
not a live-tier claim. It proves the package is internally consistent and free
of credentials, nothing more.

## Diagnostics

```sh
python3 jev/scripts/jev_diagnostics.py report --check --json   # exit 0 = clean
```

The report describes this checkout: revision and dirty flag, the manifest
digest and its validation result, every profile's validation result, the state
of each ordered patch and of the native approval adapter, the feature-switch
defaults with their requirements, each component pin and whether a local
checkout (`--components-root`) matches it, the fixture catalog, the recorded
binary hash, the isolated environment, and the ambient Codex home by **path and
existence only**.

`redaction` is part of the document rather than a promise about it: the finished
report is re-scanned and any finding is reported by pattern and JSON pointer,
never by value. `--check` fails on a finding, a manifest or profile error, a
checkout whose patch or adapter state is partly applied, a component drift, or a
mismatch against `--expect-patch-state` / `--expect-native-adapter`.

## Install order

Order matters because later steps consume what earlier ones wrote and each path
has exactly one writer.

| # | Step | Command | Writes | Owner |
| --- | --- | --- | --- | --- |
| 1 | Verify the pins | `python3 jev/scripts/verify-manifest.py --patch-state applied --json` | nothing | `jev/scripts/jev_manifest.py` |
| 2 | Choose a profile | `python3 jev/scripts/verify-manifest.py --profile jev/profiles/<p>.json` | nothing | same |
| 3 | Build the host | `python3 jev/scripts/build_provenance.py build` | `codex-rs/target/`, `.jev/build-cli.log` | the pinned cargo recipe |
| 4 | Create the isolated home | `python3 jev/scripts/isolated_env.py init` | `.jev/isolated/` | `isolated_env.py` |
| 5 | *(optional)* install the native approval adapter | re-apply patch `0002`-style install; verify with `--native-adapter applied` | `codex-rs/core/src/guardian/jev.rs`, `guardian/mod.rs`, `guardian/review_request.rs` | the adapter installer |
| 6 | *(optional)* bind the context fabric | `python3 jev/scripts/fabric_env.py --fabric <checkout> install` | `.jev/isolated/fabric/`, `home/config.toml`, `home/hooks.json`, `home/.agents/skills/jev-context/SKILL.md` | `fabric_env.py` |
| 7 | *(optional)* wire the Sentinel carrier | `python3 jev/scripts/sentinel_boundary.py install-hooks ...` | `home/hooks.json` | `sentinel_boundary.py` |
| 8 | Diagnose | `python3 jev/scripts/jev_diagnostics.py report --check` | nothing (or `--out`) | `jev_diagnostics.py` |

Never bind a runtime before its environment exists, and never let two owners
write one file: step 6 and step 7 both touch `home/hooks.json`, so `fabric_env`
merges rather than replaces and its `uninstall` restores the exact pre-install
bytes.

## State ownership

| Path | Writer | Lifetime | Removed by |
| --- | --- | --- | --- |
| `.jev/isolated/` | `isolated_env.py` | One integration environment | `isolated_env.py rollback` (moves to `.jev/superseded/`) |
| `.jev/isolated/fabric/` | `fabric_env.py` | The bound fabric runtime and its SQLite database | `fabric_env.py uninstall` |
| `.jev/isolated/home/config.toml` | generated, then merged by `fabric_env.py` | The isolated Codex home | rollback |
| `.jev/dist/`, the release record | `release_artifact.py` | Published artifacts | delete explicitly |
| `codex-rs/target/` | cargo | Build output | `cargo clean`, or delete |
| `codex-rs/core/src/guardian/*` | the adapter installer | The optional native port | the installer's `absent` path |
| the ambient Codex home and any running session | **nobody here** | - | - |

## Upgrade order

1. **Re-pin the host base.** Update `host.base_commit` and each patch's
   `sha256` / `applies_to_host_base` in `jev/compatibility-manifest.json`.
2. **Re-verify.** `verify-manifest.py --patch-state applied --json` must pass
   after the re-pin, before anything is built.
3. **Re-install the native adapter if its guarded blobs moved.** The manifest
   records the guarded host blobs; a base-commit bump changes them, so step 5
   above has to be re-run and `--native-adapter applied` re-checked.
4. **Re-create the environment.** The profile digest is part of the plan, so an
   environment created by an older plan reports `binary_matches_plan: false`
   rather than silently running.
5. **Re-record provenance** (`build_provenance.py record`) before publishing
   results, because a rebuild changes the binary hash.
6. **Re-run the gates.** `jev_diagnostics.py report --check`, the composed
   harness (`jev/END_TO_END.md`), and the required-CI lanes.

**Separate component maintenance.** `codex-plaintext-collab`,
`jev-context-fabric`, `jev-prune-kit`, `jev-sentinel`, and `jev-codex-approval`
are released in their own repositories. This integration consumes them at the
exact revisions in the manifest and never edits a component checkout: a
component bump is a manifest-only change (step 1, component table), and it does
**not** require a host rebuild unless the component's wire contract moved -
which is what the `interfaces` versions are for. OmniRoute is excluded
(`integration.excluded_repositories`).

## Backups

Before an upgrade or a rollback:

- **Manifest, profiles, patches, scripts** - tracked, so git is the backup.
- **`.jev/isolated/fabric/`** - copy it if the fabric database holds something
  you want to keep; it is state, not source.
- **`.jev/dist/`** and the release records - copy them if they were published.

Never copy a credential or captured private content into the repository. The
ambient Codex home needs no backup for this integration, because no script here
reads or writes it.

## Rollback

[`ROLLBACK.md`](ROLLBACK.md) is the full procedure: stop using the isolated
environment, uninstall the fabric binding, remove the native adapter, and return
the patch state to `absent`. `isolated_env.py rollback` **moves** the
environment to `.jev/superseded/isolated-<UTC>`; it never deletes, so the record
can be inspected or restored. A fresh setup that reproduces the validated
configuration and then rolls back is recorded in
[`evidence/release-package-fresh-setup.md`](evidence/release-package-fresh-setup.md).

## Supported platform matrix

| Platform | Declared | Validated here | What was actually run | Limits |
| --- | --- | --- | --- | --- |
| `linux-x86_64` (reference) | yes | **yes** | Fresh isolated environment, the real host binary against the loopback fixtures, the required-CI Python lanes, the composed harness, the candidate artifact build and verify. | Debug binary, not the release artifact. |
| `macos-aarch64` | yes | no | nothing | No macOS host in this environment. The Python package is stdlib-only and the patches are text, so portability is plausible, but the Rust build and the host runs are **untested** for this integration. |
| `windows-x86_64` | yes | no | nothing | Same as above, plus one concrete gap: the generated launcher `.jev/isolated/bin/codex-isolated` is a POSIX `sh` script. On Windows, call `python3 jev/scripts/launch_isolated.py` directly. |

Python: `>=3.11` for the integration scripts; `jev-prune-kit` alone also
supports 3.10. Rust: `1.95.0` from `codex-rs/rust-toolchain.toml`.

## Known limitations

- **No live-provider tier.** Paid inference needs explicit consent and a
  positive budget, which this environment does not have, so no live test runs
  implicitly and none runs in an existing session. Gate G8 below is open.
- **Approval enforcement stays disabled.** The gate is a *state* check
  (`verify-manifest.py --approval-enforcement disabled`); enabling it requires a
  report measured on the frozen holdout, which is
  [#85](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/85).
- **The artifact is not signed.** Commits are unsigned and the project has a
  single GitHub identity, so integrity rests on the record's digests and on
  reproducible builds, not on a signature.
- **The artifact does not embed a binary.** It pins the revision and the build
  recipe; a consumer rebuilds and compares. `build_provenance.py` records
  `binary_sha256: null` when the binary was not built by that invocation.
- **Host trust for hooks is partly unverifiable.** See the known limits in
  [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md).

## Release gates

| # | Gate | Evidence required | Status |
| --- | --- | --- | --- |
| G1 | Manifest, profile, patch, and adapter integrity | `verify-manifest.py` passes for `--patch-state applied`, `--profile`, `--native-adapter applied`, and fails closed for `--patch-state absent` | pass |
| G2 | Required-CI lanes green on the revision | `repo-checks / build-test` (the `.github/scripts/test_jev_*.py` lane) and `jev/tests`, both on the pinned revision | pass |
| G3 | Composed end-to-end regression with tiers | [`END_TO_END.md`](END_TO_END.md) and the harness trace | pass |
| G4 | Real-host smokes on the pinned binary | [`evidence/`](evidence/) host runs and `smoke/README.md` | pass |
| G5 | Fresh isolated setup reproduces the configuration and rolls back | [`evidence/release-package-fresh-setup.md`](evidence/release-package-fresh-setup.md) | pass |
| G6 | Candidate artifact integrity | `release_artifact.py verify` against `--expect-revision` | pass |
| G7 | Diagnostics clean | `jev_diagnostics.py report --check` exits 0 | pass |
| G8 | Live-provider coverage | #25's recorded baseline-vs-integrated run with consent and a bounded budget | **open** |
| G9 | Approval enforcement enablement | An evaluation report measured on the frozen holdout, plus the declared criteria | **open** |

A release claim is the conjunction of G1-G7 (G6 and G7 on the exact revision)
plus G8 for any live-tier claim. G8 is open, so what exists today is a
**candidate**. G9 being open is not a blocker for the candidate: enforcement is
disabled by default and the validator refuses a manifest that turns it on
without the evaluation record.

## What this package does not claim

Nothing here is a statement about model accuracy, safety, or speed. There is no
live-provider evidence in this document, no performance number, and no claim
about upstream Codex behaviour beyond the pinned base commit.
