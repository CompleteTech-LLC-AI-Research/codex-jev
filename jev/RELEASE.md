# Release, upgrade, and rollback workflow

Phase 6.3 (#26) packages the integration so a fresh, isolated setup can
reproduce the validated configuration, roll it back, and decide release
readiness from **actual evidence**: `jev/scripts/release_readiness.py` re-derives
every gate at run time, and a gate that has no run behind it stays `not-run`
instead of passing because a tracking issue was closed.

Nothing in this document is a claim about model accuracy, safety, or speed, and
no live-provider run is part of this workflow. The tiers it reports are the ones
the phase-6.1/6.2 harnesses established: `offline-fixture`, `bus-stage-stub`,
`component-stub`, `real-component`, `real-host-binary`, and `live-provider`
(**not run**; paid inference is outside this authorization).

## 1. Manifest verification

The manifest is the single source of pins, feature switches, credentials,
ownership, and the excluded repositories. Verify it before and after every
upgrade:

```sh
python3 jev/scripts/verify-manifest.py --patch-state applied \
  --native-adapter applied --approval-enforcement disabled
python3 jev/scripts/verify-manifest.py --profile jev/profiles/integrated-offline.json
python3 jev/scripts/verify-manifest.py --profile jev/profiles/isolated-offline.json
python3 jev/scripts/verify-manifest.py --components-root <component-checkouts>
python3 jev/scripts/release_readiness.py gates --json <readiness.json>
```

Exit codes are the same everywhere: `0` valid, `1` a check failed, `2` usage or
unreadable input. Every failure carries a stable code such as `E_PATCH_BASE` or
`E_REMOTE_INFERENCE_UNAUTHORIZED`, so an unsupported combination fails
explicitly rather than building quietly. `--components-root` is the check that
proves each component checkout still sits at its pinned revision.

## 2. Build and profile instructions

```sh
export OPENSSL_DIR=<static-openssl-prefix> OPENSSL_STATIC=1   # this container
export RUST_MIN_STACK=8388608                                 # codex-rs/justfile
python3 jev/scripts/build_provenance.py build                 # cargo build -p codex-cli
```

`build_provenance.py build` runs the repository recipe (`cargo build -p
codex-cli` from `codex-rs/`), tees the log to `.jev/build-cli.log`, and records
the binary path, its SHA-256, the revision, the toolchain, and the manifest
digest. The pinned host toolchain is Rust `1.95.0` (`codex-rs/rust-toolchain.toml`);
the integration scripts need Python `>=3.11` and the standard library only - no
`pip` or `npm` install at integration time.

`codex-rs/target/debug/codex` is the development binary every recorded real-host
run used. It is **not** a release artifact: a platform record names the exact
binary digest it exercised, so a release build has to be re-recorded rather than
inherited.

| Profile              | Purpose                                                      | Optional switches it turns on                                                              |
| -------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `baseline`           | Manifest defaults only.                                      | none                                                                                       |
| `isolated-offline`   | The isolated launch profile: every optional path disabled.   | none                                                                                       |
| `integrated-offline` | Full local integration with enforcement still observational. | projection (dedup, Fabric views), Sentinel shadow, retrieval screening, approval preflight |
| `enforcement-eval`   | Controlled-enforcement evaluation on eligible paths only.    | the above plus Sentinel enforcement, screening enforcement, approval enforcement           |

`isolated_env.py` refuses any profile that would enable an optional switch, so an
isolated environment cannot start in enforcement mode by accident. Remote
inference stays `false` in every profile here; a profile that enables it without
explicit consent and a positive budget is refused
(`E_REMOTE_INFERENCE_UNAUTHORIZED`).

## 3. Feature controls

Every switch resolves from the manifest, then from the profile, and reaches the
runtime as `JEV_SWITCH_<NAME>` (`1`/`0`).

| Switch                         | Default | Requires                                          | Owning component       |
| ------------------------------ | ------- | ------------------------------------------------- | ---------------------- |
| `capture.canonical_evidence`   | `true`  | -                                                 | jev-context-fabric     |
| `retrieval.budgeted_hydration` | `true`  | `capture.canonical_evidence`                      | jev-context-fabric     |
| `collab.plaintext_messages`    | `true`  | -                                                 | codex-plaintext-collab |
| `projection.dedup_receipts`    | `false` | `capture.canonical_evidence`                      | jev-prune-kit          |
| `projection.fabric_views`      | `false` | `projection.dedup_receipts`                       | jev-context-fabric     |
| `sentinel.shadow`              | `false` | -                                                 | jev-sentinel           |
| `sentinel.enforcement`         | `false` | `sentinel.shadow`                                 | jev-sentinel           |
| `screening.retrieval`          | `false` | `retrieval.budgeted_hydration`, `sentinel.shadow` | jev-sentinel           |
| `screening.enforcement`        | `false` | `screening.retrieval`, `sentinel.enforcement`     | jev-sentinel           |
| `approval.preflight`           | `false` | -                                                 | jev-codex-approval     |
| `approval.enforcement`         | `false` | `approval.preflight`                              | jev-codex-approval     |
| `remote_inference.enabled`     | `false` | explicit consent + budget                         | (host)                 |

The three `true` defaults are local, read-only, and part of the pinned build.
Every enforcement switch defaults to `false` and the release gate
`approval.enforcement.disabled` fails if that changes.

## 4. Diagnostics

| Question                                      | Command or record                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------- |
| Is this combination supported by the pin?     | `verify-manifest.py` (above)                                                          |
| Is release evidence current?                  | `release_readiness.py gates --json <file>`                                            |
| What did the isolated environment resolve to? | `isolated_env.py status`; `.jev/isolated/isolated-env.json`                           |
| Which switches will the launcher export?      | `.jev/isolated/jev-profile.json`                                                      |
| What did the fixture service serve?           | `.jev/isolated/logs/fixture-requests.json`                                            |
| What did each launch do?                      | `.jev/isolated/logs/invocations.jsonl`                                                |
| Which binary did a run exercise?              | `build_provenance.py record --out <artifact>`; `binary_sha256` in the platform record |
| Was the operator's own Codex home touched?    | `isolated_env.ambient_home_fingerprint()` (path, existence, config presence only)     |
| Which gates are blocking?                     | the `blocking` array of the readiness document                                        |

The readiness document also carries `revision`, `host_pin`, `rust_toolchain`,
`python_requirement`, `pinned_inputs_digest`, per-platform rows, and the
`isolated_roundtrip` plan so a reviewer can diff two evaluations.

## 5. Install and upgrade order

Install (or reproduce) in this order; each step is independently verifiable.

1. **Pin and verify** the manifest, patch digests, and component revisions
   (`verify-manifest.py --components-root <checkouts>`).
2. **Build** the pinned host (`build_provenance.py build`) and record its digest.
3. **Install the native boundary**: apply the ordered patches, then assert
   `--patch-state applied --native-adapter applied`. Removing them is the
   rollback direction of the same check.
4. **Create the isolated environment** (`isolated_env.py init`) and confirm
   `status` reports the profile and binary digest you intended.
5. **Bind the optional components** (for example `fabric_env.py install`) - only
   against the isolated home.
6. **Select a profile** and confirm the switch set (`jev-profile.json`).
7. **Verify** with `verify-manifest.py --profile <profile>`.
8. **Record real-host evidence** for this platform
   (`release_readiness.py record-platform --codex <binary>`); the gate treats a
   record as stale the moment the pinned inputs change.
9. **Build the candidate** (`release_readiness.py candidate --out <file>`).

Upgrade is the same sequence with one extra rule: a component change lands in
**its own repository first**, then the integration pin moves, then the host is
rebuilt and re-verified, then platform evidence is re-recorded. Never move a pin
to an unmerged or unverified revision, and never edit a component's files from
this checkout.

## 6. State ownership, backups, and rollback

| State                                                       | Owner                  | Where                                  | Backup / recovery                                                                             |
| ----------------------------------------------------------- | ---------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------- |
| Integration source and patches                              | this repository (git)  | working tree                           | git history; `verify-manifest.py` detects drift                                               |
| Isolated environment and its logs                           | the operator           | `.jev/isolated` (or `--env-dir`)       | `isolated_env.py rollback` **moves** it to `.jev/superseded/isolated-<UTC>` and never deletes |
| Fixture definitions                                         | this repository        | `jev/fixtures`, `jev/tests/*_fixtures` | git; labelled synthetic, never captures                                                       |
| Component checkouts                                         | their own repositories | operator's checkout root               | each component's own git history and release process                                          |
| Component state (receipt stores, incidents, shadow reports) | the owning component   | under the isolated env only            | rolled back with the environment                                                              |
| Operator's ambient Codex home and sessions                  | the operator           | `~/.codex`                             | **never read or written by these scripts**; the fingerprint check only reports path/existence |

Rollback:

```sh
python3 jev/scripts/isolated_env.py rollback          # reversible, never deletes
python3 jev/scripts/verify-manifest.py --patch-state absent   # after unpatching
python3 jev/scripts/verify-manifest.py --native-adapter absent # after removing the port
```

The full disable/remove order, including what is deliberately not reversible, is
in [`ROLLBACK.md`](ROLLBACK.md). Two properties keep it honest, and both are
re-checked by the release gates:

- the rollback checks **discriminate** - on the integrated tree
  `--patch-state absent` and `--native-adapter absent` report errors instead of
  passing vacuously, so a half-removed port cannot look clean;
- a fresh environment can be created twice from the same pins and rolled back to
  a preserved record, and the ambient home fingerprint is unchanged across the
  whole round trip (`isolated.roundtrip`).

## 7. Separate component maintenance

| Component                | Repository                                          | Pin                          | Owns                                                                  | Upgrade path                                                                         |
| ------------------------ | --------------------------------------------------- | ---------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `codex-plaintext-collab` | CompleteTech-LLC-AI-Research/codex-plaintext-collab | `7bf92025`                   | the plaintext collaboration patch `0001`                              | change, test, merge in the component repo, then update the patch digest here         |
| `jev-context-fabric`     | CompleteTech-LLC-AI-Research/jev-context-fabric     | `50790992`                   | capture, retrieval, memory tools, the approved prose view (stage 200) | component PR, then move `components[].revision` and the profile fabric pin           |
| `jev-prune-kit`          | CompleteTech-LLC-AI-Research/jev-prune-kit          | `2ecc8ff4`                   | the jev-bus contract and the dedup stage (stage 100)                  | component PR, then move the pin and re-run the bus-boundary tests                    |
| `jev-sentinel`           | CompleteTech-LLC-AI-Research/jev-sentinel           | `4ecd748d`                   | the boundary evaluator and veto evaluation                            | component PR, then move the pin and re-run the veto and screening tests              |
| `jev-codex-approval`     | CompleteTech-LLC-AI-Research/jev-codex-approval     | `0b931ee2`                   | the approval preflight adapter and its evaluation record              | component PR, then move the pin; the adapter is installed by `native_source_adapter` |
| `codex-jev` (this repo)  | CompleteTech-LLC-AI-Research/codex-jev              | `8198a91a` (host patch base) | the host call site, envelopes, ownership, and the integration scripts | here, with the required checks                                                       |

OmniRoute is excluded (`integration.excluded_repositories`) and the validator
refuses a manifest that tries to integrate it.

## 8. Platform matrix

Support is declared in `host.platforms.supported`. Only a recorded
`real-host-binary` run makes a platform verified, and a record is bound to the
binary digest, the revision, and a digest of the pinned inputs it depended on.

| Platform                   | Status       | Evidence                                                                                                                            |
| -------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `linux-x86_64` (reference) | **verified** | `jev/evidence/platform-matrix.json`: `plaintext-collaboration` and `projection-reset` host smokes against the recorded debug binary |
| `macos-aarch64`            | **not run**  | no runner or host available in this environment                                                                                     |
| `windows-x86_64`           | **not run**  | no runner or host available in this environment                                                                                     |

Re-record with:

```sh
python3 jev/scripts/release_readiness.py record-platform \
  --codex <binary> --kit <jev-prune-kit-checkout>
```

The gate credits a platform only when its record is `verified`; every other
outcome refuses it and is named in the gate detail:

- `stale` - the pinned-input digest no longer matches the tree, or the recorded
  revision resolves but is not an ancestor of `HEAD`;
- `unverifiable` - the recorded revision does not resolve in this clone, so the
  binding cannot be checked; an unresolvable revision is **not** a pass;
- `fail` - the record claims a `live-provider` tier, records no harness result,
  or omits a revision or a successful harness verdict.

`release_ready` therefore stays `false` while any supported platform has no
current, verifiable run - that is the intended outcome, not a bug.

## 9. Release gates

| Gate                             | Proves                                                              | Evidence class |
| -------------------------------- | ------------------------------------------------------------------- | -------------- |
| `manifest.pins`                  | manifest, pins, and every declared profile validate                 | verified-here  |
| `host.patch.applied`             | the release tree carries the pinned patches                         | verified-here  |
| `host.patch.rollback-detected`   | the rollback check reports a patched tree as patched                | verified-here  |
| `host.adapter.applied`           | the native approval adapter is installed and wired                  | verified-here  |
| `host.adapter.rollback-detected` | the adapter-removal check detects the installed port                | verified-here  |
| `approval.enforcement.disabled`  | enforcement is declared but still off by default                    | verified-here  |
| `remote_inference.disabled`      | no consent, no budget, `false` default, off in the isolated profile | verified-here  |
| `isolated.roundtrip`             | a fresh setup reproduces the configuration and rolls back           | verified-here  |
| `candidate.exclusions`           | the artifact carries no credential-shaped bytes or denied path      | verified-here  |
| `platform.matrix`                | every supported platform has a current real-host run                | recorded       |

`release_ready` is `true` only when every gate is `pass`. A gate whose evidence
is only an assertion (`claimed`) is rewritten to `fail`, and a gate with no run
behind it is `not-run` and listed in `blocking`; neither can be mistaken for a
pass.

## 10. The candidate artifact

```sh
python3 jev/scripts/release_readiness.py candidate --out <candidate.tar.gz> \
  --json <candidate.json>
```

The inclusion set is declared, not globbed ad hoc: the `jev/` tree (docs,
scripts, profiles, patches, fixtures, smokes, tests, evidence),
`.github/scripts/test_jev_*.py` (the required-CI lane), and the host boundary
module `codex-rs/core/src/jev_bus.rs`.

Excluded, and reported by rule in the candidate document:

- **private and generated state**: `.jev/` (the isolated environment and its
  logs), `superseded/`, `__pycache__/`, virtualenvs, caches, `target/`,
  `node_modules/`;
- **credential paths**: `.env*`, `*.pem`, `*.p12`, `*.key`, `id_rsa*`/`id_ed25519*`,
  `*credential*`, `auth.json`, `hosts.yml`, `.netrc`, `*.log`, `*.sqlite`;
- **credential-shaped content**: private-key blocks, provider keys (`sk-`,
  `sk-ant-`), GitHub tokens (`ghp_`, `github_pat_`), AWS access keys, Slack
  tokens, `Authorization: Bearer` headers, and secret-shaped assignments.

A content hit refuses the whole candidate (exit 1, no archive written) unless the
file is one of the four declared **synthetic fixtures** whose subject is a
credential shape. Those are listed with a reason in the candidate document and
are withheld from the archive, so the artifact contains no credential bytes at
all, placeholders included. Adding a fifth exemption fails the required-CI test
`test_credential_shaped_fixtures_are_withheld_and_declared`, which pins the set.

The document records every included file with its SHA-256 and size, the
`bundle_digest` over that list, and the archive's own SHA-256. Both are
byte-identical across builds: the tar members are sorted with zeroed
metadata and the gzip header carries no name or timestamp, so two runs from the
same revision produce the same bytes whatever the output path.

## 11. Failure and disable behavior

- **Unsupported combination** - the validators fail closed with a stable code
  rather than building; a profile that enables an optional switch in an isolated
  environment is refused.
- **Projection** - a failed, stale, or unapproved view leaves the stage input
  byte-identical (`stale_view`, `unapproved_removal`, `view_not_approved`), and
  the canonical transcript is never rewritten.
- **Sentinel** - veto paths fail closed and a latched veto cannot be cleared by a
  later approval.
- **Approval** - an eligibility failure, uncertainty, or timeout defers to the
  existing reviewer within the original deadline; enforcement stays off until
  the declared evaluation criteria are met.
- **Remote inference** - disabled by default and refused without explicit
  consent and a bounded budget.
- **Disable** - set the `JEV_SWITCH_*` values to `0`, unset
  `CODEX_JEV_PYTHON`/`CODEX_JEV_LAUNCHER`/`CODEX_JEV_CONFIG`, roll the isolated
  environment aside, and/or uninstall the patch and the native port. The
  rollback checks above report a half-finished removal.

## 12. Known limitations

- `macos-aarch64` and `windows-x86_64` are supported by the pin and **not
  validated**: no run exists, so the platform gate stays `not-run` and the
  release is not ready for them.
- The recorded real-host runs used a **debug** binary
  (`codex-rs/target/debug/codex`), not a release build; a release artifact needs
  its own record.
- No `live-provider` tier was run. Paid inference is outside this authorization
  and requires explicit consent and a positive budget; the token figure
  elsewhere is a byte-derived estimate, never a measurement.
- The platform record's binaries are not rebuilt by the gate, so the gate checks
  that its pinned inputs are current rather than re-executing the smokes.
- Stage 200 (the Fabric prose view) has no component bus-stage entry point, so
  the composed and performance harnesses use the host-side `fabric_views` code.
- Repository-wide CI is red for pre-existing, unrelated reasons (codespell in
  `codex-rs`, Bazel macOS/Windows lanes, `cargo shear`, SDK lanes); the lane this
  workflow relies on is `repo-checks / build-test`.
- `main` carries no branch protection, so there is no enforceable required-check
  rule; merges rely on the checks recorded in each PR.
- Two follow-ups that weakened the gate bindings are now **fixed**, not merely
  recorded: #97 — the platform gate forgot to refuse a record whose `revision`
  is unresolvable in the local clone, so an invented or never-fetched SHA was
  credited `verified` (fixed by crediting only on `revision_reachable(...) is
  True`); and #98 — `gates --skip-roundtrip` *removed* the round-trip gate
  instead of emitting it as `not-run`, so `release_ready: true` was possible
  with the round trip never proven (fixed by emitting the gate as `not-run`,
  which blocks). Both are reproduced in
  [`evidence/release-phase-claim.md`](evidence/release-phase-claim.md).

## 13. Recorded evidence

| Artifact                                                                   | Subject                                                             |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| [`evidence/platform-matrix.json`](evidence/platform-matrix.json)           | the recorded real-host platform run and the gates' freshness inputs |
| [`VALIDATION.md`](VALIDATION.md)                                           | offline, real-host, and live-provider tiers                         |
| [`END_TO_END.md`](END_TO_END.md)                                           | the composed regression harness and its tiers                       |
| [`evidence/release-phase-claim.md`](evidence/release-phase-claim.md)       | the composed phase-6 release verdict and why it is `false` here      |
| [`evidence/plaintext-pinned-build.md`](evidence/plaintext-pinned-build.md) | the plaintext collaboration host run                                |
| [`evidence/projection-real-host.md`](evidence/projection-real-host.md)     | the projection and exact-reset host run                             |
| [`ISOLATED_ENV.md`](ISOLATED_ENV.md)                                       | build and isolated-environment instructions                         |
| [`ROLLBACK.md`](ROLLBACK.md)                                               | disable, remove, and not-reversible behavior                        |
