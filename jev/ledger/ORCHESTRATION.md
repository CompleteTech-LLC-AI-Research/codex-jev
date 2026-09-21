# JEV integration orchestration ledger

Durable record for epic #2. Updated as work progresses. No credentials, private
captures, or raw sensitive logs belong here; paths are recorded so state can be
re-derived, not so it can be trusted blindly.

Last updated: 2026-09-21 (phase 1 start).

## Integration pin

| Field | Value |
|:--|:--|
| Integration host | `CompleteTech-LLC-AI-Research/codex-jev` |
| Pin | `8198a91a4f46b01647bc6c0d8d63afafbf4c9180` |
| Upstream base merged | `9fdff739ea0eca7881f180858dfd71b7bd1fa483` (openai/codex, 2026-09-20) |
| Version string | `0.0.0-dev` |
| Manifest | `jev/manifest.integration.json` |

## Component pins

| Component | Revision | Kind | Codex surface |
|:--|:--|:--|:--|
| `codex-plaintext-collab` | `073b3a99e4fa0eebe5417fd096f2e37abd7a7527` | source patch | `multi_agents_spec.rs`, `router.rs` |
| `jev-context-fabric` | `5079099211c0d39a6ead347633a99d675a210c64` | installed package | `config.toml` MCP key, `hooks.json`, skill |
| `jev-prune-kit` | `2ecc8ff4e0976991c7abc09287d8f2f736d3164c` | installed package | skill only on Codex; supplies the dedup stage |
| `jev-sentinel` | `4ecd748d38fbe9ed5c770e7e69a1e03a3db4bf7a` | installed package | `hooks.json` |
| `jev-codex-approval` | `0b931ee24a907c9dc47dc1828a94a6afcd7775b6` | source transform | `core/src/guardian/jev.rs` |

Approval adapter: targets upstream `c45ea25ffb72d5f7324489d824d0c677283aa0b4`; both
guardian blobs are byte-identical at the integration pin, so no port is required
(verified by `verify_manifest.py resolve`).

## Issue and dependency state

| Issue | Depends on | State | Notes |
|:--|:--|:--|:--|
| #2 epic | — | open | acceptance tracked by phases |
| #3 phase 1 | — | open | implementation started |
| #4 phase 2 | #3 | open | blocked |
| #5 phase 3 | #4 | open | blocked |
| #6 phase 4 | #5 | open | blocked |
| #7 phase 5 | #6 | open | blocked |
| #8 phase 6 | #7 | open | blocked |
| #9 manifest and contracts | — | in progress | this PR |
| #10 plaintext collaboration | #9 | in progress | patched release build running |
| #11 isolated profile | #10 | pending | profile tooling can be prepared, not validated |
| #12–#26 | chain | pending | blocked by their predecessors |

Phase order is the delivery sequence. Concurrency slots are not used to bypass a blocked
dependency.

## Repository protection state

`main` has no branch protection and no rulesets; required checks are empty. That means
required approvals must come from the repository's own conventions and independent review,
not from a protection gate. No `--admin` or other bypass is used anywhere.

## Decisions

| ID | Decision | Contract |
|:--|:--|:--|
| D-001 | `codex` becomes a carrier host owned by `codex-jev`; `TRANSFORM_HOSTS` unchanged | C2 |
| D-002 | The adapter never reorders, merges or re-keys messages between stages | C3 |
| D-003 | Recalled content is injected untrusted, marked stale; unprovable source hashes are dropped | C5 |
| D-004 | Absent/stale/failed/uncertain approval defers to the existing reviewer inside the original deadline | C7 |
| D-005 | OmniRoute is excluded everywhere, enforced by the verifier | C13 |

Open: how `jev-prune-kit` should register its dedup stage for host `codex` (installer
change only, `bus.py` untouched). Owner: phase 3 work tracked from #16.

## Workstreams

| Workstream | Repository | Worktree | Branch | Base commit | Owner |
|:--|:--|:--|:--|:--|:--|
| Manifest and contracts (#9) | codex-jev | `wt/compat-manifest` | `jev/1.1-compat-manifest` | `8198a91a4` | lead |
| Plaintext integration (#10) | codex-jev | `wt/plaintext-collab` | `jev/1.2-plaintext-collab` | `8198a91a4` | lead |
| Component checkouts | five component repos | `work/<repo>` | `main` | pinned revisions | lead (read-only) |

Shared build target: `work/target` (a release build of the patched binary is large; one
shared target dir keeps the disk footprint bounded).

## PR and check state

| Issue | Branch | PR | Tested commit | Checks | Reviewer findings | Merge commit |
|:--|:--|:--|:--|:--|:--|:--|
| #9 | `jev/1.1-compat-manifest` | pending | pending | `verify_manifest.py check` + 25 unit tests pass locally | pending | — |
| #10 | `jev/1.2-plaintext-collab` | pending | pending | release build in progress | pending | — |

## Validation evidence so far

| Evidence | Label | Result |
|:--|:--|:--|
| `verify_manifest.py resolve` against all six checkouts | offline-fixture | pass: revisions bound, patch applies to the pin, adapter blobs match, vendored jev-bus copies agree |
| `jev/tests` unit suite (25 tests) | offline-fixture | pass, including 18 explicit refusals |
| Patched release build of `codex` | real-host | in progress |
| End-to-end lifecycle | — | not started |
| Live-provider measurement | live-provider | not performed and not authorized |

## Environment limitations

- No system OpenSSL development files; the vendored fallback is documented in
  `jev/docs/BUILD.md` and is not a component pin.
- Commit signing is unavailable: no `gpg`, no SSH signing configuration, no signing key
  registered on the account. Commits are unsigned; no key was created and signing was not
  weakened.
- Only the reference platform is validated. macOS and Windows remain `untested`.

## Remaining blockers

- Codex hook trust approval is an operator action inside a live session; until it is
  performed, hook-based components can only be reported as staged, not activated.
