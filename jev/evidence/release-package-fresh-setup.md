# Evidence: a fresh isolated setup reproduces the configuration and rolls back

Tracks [#26](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/26),
the release-package sub-issue of
[#8](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/8). It is
gate **G5** of [`RELEASE.md`](../RELEASE.md) and it is what
[`ROLLBACK.md`](../ROLLBACK.md) points at for the "Verified rollback" claim.

Nothing here is a statement about model accuracy, safety, or performance. It
records what was executed, on which revision, and what was not executed.

## Revisions

| Item | Value |
| --- | --- |
| Verified revision of this repository | `fea08a179faf18ef31de792bbe996b5d527b428c` (branch `jev/6.3-release-package`, on top of `origin/main` `3ddf7345a9`) |
| Binary exercised | `/home/agent/jev/work/lead/target/debug/codex`, sha256 `6d51fdc9278d1a2fbc1bb01e024ae12d5d9a3b233d7229a7f6e792eaf35c0c3b` |
| Toolchain | `rustc 1.95.0`, `cargo 1.95.0` (`codex-rs/rust-toolchain.toml`); `python3 3.11.2`; `ruff 0.15.13` from the `scripts` uv project |
| Pinned host base | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| Candidate artifact | `codex-jev-candidate.tar.gz`, sha256 `f2343da19bfc006d45fecc9924ea5b60c2029294519c4eb052194cdc0544dd3b` |
| Tier | `offline-fixture + real-host-binary`, plus `derived-configuration` for the artifact and diagnostics |

The revision above is the tree these runs were performed on. This document is
itself a packaged member, so the commits after it that add the document and its
pins change the artifact digest and grow the member and test totals: those
commits are `git diff <revision>..HEAD --stat`, and the artifact named here is
the one built at the revision above. Recording a digest for an archive that
contained this line would be circular, so the record carried *inside* the
archive is the authoritative one, and `--expect-revision` is what ties a
rebuild to its tree.

`cargo build -p codex-cli --bin codex` on this revision is a no-op
(`Finished dev profile ... in 4.77s`), so the binary above is the one this
revision builds. It is a **debug** build, not a release artifact, and no
release binary was rebuilt for this evidence.

## What was run

Every command below ran with the worktree clean at the revision above; the
health check is `git status --porcelain` printing nothing, and the artifact
record confirms it with `source.worktree_dirty: false`.

### 1. The candidate artifact builds, verifies, and is reproducible

```sh
python3 jev/scripts/release_artifact.py build \
  --out "$D/candidate.tar.gz" --record-out "$D/release-record.json" --json
python3 jev/scripts/release_artifact.py verify "$D/candidate.tar.gz" \
  --expect-revision fea08a179faf18ef31de792bbe996b5d527b428c
```

The record pins 130 members and the archive carries those plus the record
itself. Seven secret-shaped findings are allowed, each one a labelled fixture
whose matched text carries a synthetic marker, and each named in the record:

| Member | Pattern | Marker | Occurrences |
| --- | --- | --- | --- |
| `.github/scripts/test_jev_capture.py` | `openai_key` | `fixture` | 1 |
| `.github/scripts/test_jev_release.py` | `openai_key` | `fixture` | 1 |
| `.github/scripts/test_jev_screening.py` | `openai_key` | `aaaa` | 1 |
| `jev/tests/capture_fixtures/04-secret-bearing.jsonl` | `github_token` | `fixture` | 2 |
| `jev/tests/capture_fixtures/04-secret-bearing.jsonl` | `openai_key` | `fixture` | 1 |
| `jev/tests/capture_fixtures/README.md` | `github_token` | `fixture` | 1 |
| `jev/tests/capture_fixtures/README.md` | `openai_key` | `fixture` | 1 |

Building the same revision twice into different paths produces the same sha256,
so the archive is byte-reproducible.

### 2. A fresh isolated setup reproduces the validated configuration

```sh
python3 jev/scripts/isolated_env.py init --env-dir "$D/isolated" --binary "$BIN"
python3 jev/scripts/isolated_env.py status --env-dir "$D/isolated"
python3 jev/scripts/launch_isolated.py --env-dir "$D/isolated" --dry-run
python3 jev/scripts/launch_isolated.py --env-dir "$D/isolated" --timeout 120 \
  exec "report status"
```

The resolved plan reproduces the profile the manifest names: profile
`isolated-offline` with digest
`3e138ea57e4f7e936155c58ae0369880710c1bf61725f5fc8e04b02dfd9e2462`, host commit
`8198a91a4f46b01647bc6c0d8d63afafbf4c9180`, `optional_features_enabled: []`, the
loopback fixture service on `http://127.0.0.1:8756/v1`, and
`remote_inference: {consent: false, enabled: false}`. The twelve `JEV_SWITCH_*`
variables come from the profile's declared defaults rather than from the
ambient environment: exactly three are `1`
(`CAPTURE_CANONICAL_EVIDENCE`, `COLLAB_PLAINTEXT_MESSAGES`,
`RETRIEVAL_BUDGETED_HYDRATION`) and no enforcement gate is among them.

`status` reports `binary_matches_plan: true`, so the recorded plan and the
binary on disk are the same file.

### 3. Two hosted runs against the fixtures

Both invocations exit 0 and answer `jev offline fixture reply`, with
`tokens used 0` and one recorded fixture request (`assistant_message` on
`/v1/responses`). The invocation record names the tier
`offline-fixture + real-host-binary`, the plan's binary sha256, and the pinned
host commit.

### 4. Diagnostics are clean on the same layout

```sh
python3 jev/scripts/jev_diagnostics.py report --binary "$BIN" \
  --env-dir "$D/isolated" --out "$D/diagnostics.json" --check
```

Exit 0. The report carries the verified revision, the binary sha256, and
`redaction.findings: []`; `check.failures` is empty. It is assembled from
allow-listed fields, so the ambient home appears as a path and two booleans,
never as content.

### 5. Rollback moves the environment aside, and is idempotent

```sh
python3 jev/scripts/isolated_env.py rollback --env-dir "$D/isolated" \
  --target-root "$D/superseded"
python3 jev/scripts/isolated_env.py rollback --env-dir "$D/isolated" \
  --target-root "$D/superseded"
python3 jev/scripts/isolated_env.py status --env-dir "$D/isolated"
```

| Call | Exit | Result |
| --- | --- | --- |
| first `rollback` | 0 | `state: moved` to `superseded/isolated-20260921T213158Z` |
| second `rollback` | 0 | `state: absent`, `moved_to: null` |
| `status` afterwards | 1 | `error: no isolated environment at ...` |

The moved directory is complete: the second call finds nothing to move rather
than deleting anything, and `status` fails closed instead of reporting a plan
for an environment that is gone.

The default destination was exercised separately, without `--target-root`: on
this revision `rollback` moved the environment to
`.jev/superseded/isolated-20260921T214027Z` in the repository, which
`.gitignore` excludes (`.gitignore:99:.jev/`), so the worktree stayed clean.
`--target-root` exists for the case where the environment lives outside the
repository and its record should be kept next to it.

### 6. The ambient home is not touched

The machine's own Codex home is `/home/agent/.codex-account-1`, which the
session producing this evidence also writes to (its SQLite state, WAL, and log
files change while it runs). It therefore cannot serve as its own control, so
the claim is supported two ways.

**A negative control.** The same sequence (init, launch, rollback) ran with
`CODEX_HOME` pointed at a scratch home holding a `config.toml`, a session
rollout, a history file, and a log. Its file list and digest are identical
before and after; the launch still answered from the fixtures, because the
launcher sets the child's `CODEX_HOME` to the environment's own home.

**An attribution window.** Snapshotting the real home through a 35-second idle
window and then through the command window shows the same set of changed
paths in both, and none that only the commands changed:

| Window | Paths changed | Changed by the commands alone |
| --- | --- | --- |
| idle, no integration command | 9 (`*_N.sqlite`, `*-wal`, `*-shm`, `logs_2.sqlite`) | — |
| init + launch + rollback | 6, all within the idle set | **0** |

## Results

| Check | Command | Exit | Observed |
| --- | --- | --- | --- |
| Artifact build | `release_artifact.py build` | 0 | 130 members + record = 131 archived |
| Artifact verify | `release_artifact.py verify --expect-revision` | 0 | ok against its own record |
| Reproducibility | build twice | 0 | identical sha256 `f2343da19b…` |
| Fresh setup | `isolated_env.py init` | 0 | plan matches the profile |
| Plan consistency | `isolated_env.py status` | 0 | `binary_matches_plan: true` |
| Hosted launch ×2 | `launch_isolated.py exec` | 0, 0 | `jev offline fixture reply` |
| Diagnostics | `jev_diagnostics.py report --check` | 0 | no redaction findings, no check failures |
| Rollback | `isolated_env.py rollback` ×2 | 0, 0 | `moved`, then `absent` |
| Status after rollback | `isolated_env.py status` | 1 | fails closed |
| Ambient home control | probe home digest | — | identical before and after |
| Manifest gates | `verify-manifest.py` | 0 | `--patch-state applied`, all four `--profile` paths, `--native-adapter applied`, `--approval-enforcement disabled` |
| Manifest refusal | `verify-manifest.py --patch-state absent` | 1 | fails closed; also `--native-adapter absent` |
| Required-CI lane | `python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'` | 0 | 404 tests, OK |
| Focused lane | `python3 -m unittest discover -s jev/tests -t jev/tests` | 0 | 183 tests, OK |
| Formatting | `ruff format --check .` (0.15.13), `cargo fmt -- --config imports_granularity=Item --check` | 0 | 185 files formatted; no Rust file changed |

The 25 tests in `.github/scripts/test_jev_release.py` carry the refusal cases
this document does not re-run by hand: a tampered member, a missing required
member, a credential-shaped name, a real key in a product file, an undeclared
synthetic allowance, a credential planted in the record, an untracked leak,
and an artifact built from a revision the caller did not expect.

## What this does not show

- **No live provider.** Every run above is offline: the only reachable endpoint
  is the loopback fixture service and no credential is read. Gate G8 is open.
- **No signed artifact and no published binary.** The archive carries no
  compiler output; a consumer rebuilds from the pinned revision and compares.
  The record's `source.worktree_dirty` is `false` here, which is what makes that
  comparison meaningful.
- **One platform.** `linux-x86_64` only, matching the platform matrix in
  [`RELEASE.md`](../RELEASE.md). `macos-aarch64` and `windows-x86_64` are
  declared and untested, and the generated launcher is a POSIX `sh` script.
- **Rollback is not an uninstall.** It moves the isolated environment aside. It
  does not remove the fabric binding, the native adapter, or the host patches;
  those steps are in [`ROLLBACK.md`](../ROLLBACK.md).
- **The ambient-home claim is about this integration, not about Codex.** The
  control shows no *integration* script writes to the home `CODEX_HOME` names.
  The host binary it launches writes to the home the launcher gives it.

## Reproduce

```sh
cd /home/agent/jev/work/lead/codex-jev
D=/home/agent/jev/work/lead/verify/release-fresh
BIN=/home/agent/jev/work/lead/target/debug/codex   # built per ISOLATED_ENV.md

python3 jev/scripts/release_artifact.py build --out "$D/candidate.tar.gz" \
  --record-out "$D/release-record.json" --json
python3 jev/scripts/release_artifact.py verify "$D/candidate.tar.gz" \
  --expect-revision "$(git rev-parse HEAD)"

python3 jev/scripts/isolated_env.py init --env-dir "$D/isolated" --binary "$BIN"
python3 jev/scripts/launch_isolated.py --env-dir "$D/isolated" --timeout 120 \
  exec "report status"
python3 jev/scripts/jev_diagnostics.py report --binary "$BIN" \
  --env-dir "$D/isolated" --check
python3 jev/scripts/isolated_env.py rollback --env-dir "$D/isolated" \
  --target-root "$D/superseded"
```

The artifact pins `source.revision`, so `--expect-revision` is what ties a
rebuild to the tree it came from. `verify` exits 0 for a candidate that matches
its own record and 1 for every refusal above.
