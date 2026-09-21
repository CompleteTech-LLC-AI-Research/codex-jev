# Native approval preflight and Guardian fallback

This document records where the JEV approval preflight enters the pinned host,
what it is allowed to replace, and what stays with the existing reviewer. It
backs issue [#21](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/21),
contract `C6` in [`CONTRACTS.md`](CONTRACTS.md), and the manifest feature
`approval.preflight`.

Preflight is **off by default**. Nothing here grants, widens, records, or caches
a permission, and the host's own execution decision remains authoritative.

## Where the boundary is

The host reviews an approval request in one synchronous attempt. Preflight is a
candidate answer for that one attempt, and it is consulted before the existing
inference starts:

| Path | Location | Role |
| --- | --- | --- |
| Call site | `codex-rs/core/src/guardian/review_request.rs:154` | `super::super::jev::review(..)` inside `attempt()`; `None` falls through to the unchanged Guardian call at `:168`. |
| Review identity | `codex-rs/core/src/guardian/review_request.rs:10` | `PreparedApproval.jev_review_id`, taken from the host's own `review_id` at `:138`, so the answer is bound to one request. |
| Module declaration | `codex-rs/core/src/guardian/mod.rs:10` | `mod jev;`. |
| Ported adapter | `codex-rs/core/src/guardian/jev.rs` | The carrier: eligibility, evidence preparation, one bounded subprocess, and the response gate. |

`guardian/review_request.rs` is included as `guardian::review::request`
(`guardian/review.rs:5` declares `#[path = "review_request.rs"] mod request;`),
so the adapter reaches `guardian::jev` as `super::super::jev` and reuses
`guardian::review`, `guardian::prompt`, and `guardian::approval_request` rather
than copied policy.

## What was ported, and what the guard proves

`jev-codex-approval` ships the adapter as `adapters/codex-native/jev.rs` at the
pinned revision `0b931ee24a907c9dc47dc1828a94a6afcd7775b6` (sha256
`37d3a1dddf32d26f18a5149b6ed39ab8d27be10f6af9132f6a5889ccafb23f43`, 269
lines). Its own `STATUS.md` records `native_build_verified: false`: the authors
never compiled it. This repository ports it into the pinned host base, compiles
it, and tests it.

The installer refuses to install onto source it does not recognize. The two
guarded host files are recorded as blob ids in
`compatibility-manifest.json`:

| Guarded file | Blob at the pinned host base |
| --- | --- |
| `codex-rs/core/src/guardian/mod.rs` | `c8765943a57d65eb1186133d584cbf70bc0989b0` |
| `codex-rs/core/src/guardian/review_request.rs` | `3ac51383012724857723385974c5a8c15ba87c5a` |

Both blobs are byte-identical at this repository's pinned host base
`8198a91a4f46b01647bc6c0d8d63afafbf4c9180` and at the adapter's upstream
`c45ea25ffb72d5f7324489d824d0c677283aa0b4`, so the port needed no source
reconciliation: the guarded files differ from the pinned base only by the four
declarations above. `verify-manifest.py --native-adapter applied` checks the
blobs, the module declaration, and the call site, and fails closed when any of
them is missing or moved.

## The configuration contract

Three environment variables must each be an absolute path to an existing file.
If any one is missing or relative, preflight is inert and the original route
runs unchanged:

| Variable | Meaning |
| --- | --- |
| `CODEX_JEV_PYTHON` | The interpreter, invoked as `-I <launcher> native --config <config>`. |
| `CODEX_JEV_LAUNCHER` | The reviewed launcher script; the subprocess runs with the launcher's directory as its working directory. |
| `CODEX_JEV_CONFIG` | The engine configuration file. |

The switch is the only feature gate, and `CONTRACTS.md` C10 makes
`JEV_SWITCH_<FEATURE>` the only mechanism phases 2-6 may read:

| Switch | Effect |
| --- | --- |
| `JEV_SWITCH_APPROVAL_PREFLIGHT=1` | Enables preflight for eligible attempts. |
| unset, empty, `0`, `true`, `yes` | Disabled. `1` is the only value that turns a declared switch on. |

`isolated-offline.json` keeps both approval switches `false`, so the isolated
profile starts inert. `integrated-offline.json` enables `approval.preflight`;
without the three paths above it still routes to Guardian.

## Eligibility and deferral

Every row is a deliberate hand-back to the host's existing route, not an error.
None of them changes what the host does.

| Condition | Result |
| --- | --- |
| The declared switch is not `1` | Guardian runs; preflight is never started. |
| `CODEX_JEV_PYTHON`, `CODEX_JEV_LAUNCHER`, or `CODEX_JEV_CONFIG` does not resolve | Guardian runs. |
| The attempt was cancelled before it started, or is cancelled at any later point | Guardian runs, and a late cancellation discards a prepared answer. |
| The turn requires Guardian (`require_guardian`) | Guardian runs. |
| The caller asked for the synchronous reviewer | Guardian runs. |
| A retry of an earlier review | Guardian runs, so the reviewer that produced the first judgement answers again. |
| The action class stays Guardian-owned: `apply_patch` and `exec_command` without an escalation request are the only eligible classes; `write_stdin`, `mcp_tool_call`, `network_access`, `request_permissions`, `execve` (unix), and any escalated command are not | Guardian runs. |
| The retained context is incomplete, or Guardian's prompt was truncated | Guardian runs. |
| Managed requirements pin the reviewer or require automatic review for the model | Guardian runs. |
| The prepared answer changes the policy text, the history revision, or the root authorization version between preparation and use | The answer is discarded and Guardian runs. |
| The wire payload exceeds the bound, the subprocess fails or is killed, the answer is malformed, or the deadline is exhausted | Guardian runs within the original deadline. |

## What a preflight may not decide

An accepted answer must carry `schema_version: 1`, the exact `request_id`, a
`jev-`-prefixed model identifier, and one of the declared decisions. On top of
that the host re-checks the boundary itself instead of trusting the engine's
policy: an `allow` is accepted only at `low` risk with `explicit` or
`within_task` authorization. `deny` and every ambiguity keep the existing
reviewer. Enforcement stays disabled; a shadow record is a candidate answer,
not a granted permission.

The budget is `min(guardian_deadline, now + 2s)`, the wire bound is `256 KiB`
in each direction, the child has stdout piped and stderr discarded, and
`kill_on_drop(true)` means an abandoned attempt cannot outlive the review. The
per-request cost is therefore one interpreter start plus one bounded local
invocation, and it is paid only on eligible attempts (#25 measures it).

## Evidence

The recorded tier is **offline-fixture plus static host compile tests**. The
Python approval engine was never executed, no live model was used, and no real
provider answered.

| Check | Command | Result |
| --- | --- | --- |
| Host compiles with the port | `cargo check -p codex-core --lib --offline` | ok. |
| Adapter unit tests | `cargo test -p codex-core --lib jev:: --offline` | 10 passed; 0 failed. |
| Guardian behaviour is unchanged | `cargo test -p codex-core --lib guardian:: --offline` | 89 passed; 0 failed. |
| Formatting | `cargo fmt --all -- --config imports_granularity=Item --check` | ok. |
| Ported-tree checks | `python3 jev/scripts/verify-manifest.py --patch-state applied --native-adapter applied` | ok, exit 0. |
| Rollback direction fails closed | `python3 jev/scripts/verify-manifest.py --native-adapter absent` | `E_NATIVE_ADAPTER_STATE`, exit 1. |
| Manifest and validator tests | `python3 -m unittest discover -s jev/tests -t jev/tests` | 95 passed. |
| Required-CI Python suites | `python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'` | 163 passed. |

Not performed, and not claimed: a live model review, a real Python engine
invocation, cancellation while a subprocess is running, authorization changes
during a response, managed required-review policies, background or
multi-environment sessions, and per-OS runs. The adapter records its own five
parser and binding tests; the rest of the list above is exactly what issue #21
leaves to the real-host phase (#22 and #25).

## Disable and rollback

1. Unset `CODEX_JEV_PYTHON`, `CODEX_JEV_LAUNCHER`, and `CODEX_JEV_CONFIG`, or set
   `JEV_SWITCH_APPROVAL_PREFLIGHT=0`, and restart the host. The original review
   route runs; no source change is needed.
2. To remove the port from the tree, delete the `mod jev;` declaration and the
   `jev.rs` file, and restore the single
   `run_guardian_review_session_before_deadline` call site that the installer
   replaced (`codex-rs/core/src/guardian/review_request.rs:154`). Then
   `python3 jev/scripts/verify-manifest.py --native-adapter absent` must pass.
3. [`ROLLBACK.md`](ROLLBACK.md) covers the wider integration. Removing the
   plaintext patch does not remove this module, and removing this module does
   not remove the patch: the declarations in the manifest are what keep the two
   states distinguishable.
