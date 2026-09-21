# JEV integration host (`codex-jev`)

This directory is the integration surface for the JEV stack tracked by
[epic #2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2). It holds the
compatibility manifest, the cross-component contracts, the orchestration ledger, and the
tooling that resolves and verifies a pinned stack.

It is deliberately not `docs/`: upstream keeps `docs/` for its own user-facing
documentation, and these files describe a fork-specific integration.

## Scope

| Role | Repository |
|:--|:--|
| Integration host | `codex-jev` (this fork of `openai/codex`) |
| Collaboration transport | `codex-plaintext-collab` |
| Memory and retrieval | `jev-context-fabric` |
| Context projection | `jev-prune-kit` |
| Boundary checks | `jev-sentinel` |
| Approval preflight | `jev-codex-approval` |
| **Excluded** | `omniroute-codex-docker` |

Each component keeps its own repository, release process and tests. This host pins exact
revisions, records the patch order, and owns the one native boundary that components
cannot own themselves.

## Layout

| Path | Purpose |
|:--|:--|
| `manifest.integration.json` | Machine-readable pins, patch order, stages, feature switches, ownership, platforms |
| `verify_manifest.py` | Validates the manifest and binds it to checkouts on this machine |
| `tests/` | Focused tests for the verifier, including the refusals it must make |
| `contracts/integration-contracts.md` | Cross-component contracts and integration decisions |
| `ledger/ORCHESTRATION.md` | Issue, dependency, agent, PR and merge ledger |
| `docs/BUILD.md` | Build environment record and build/verification recipe |

## Architecture

```
Codex session
  └─ Fabric capture (canonical evidence, hashed, before projection)
       └─ native jev-bus carrier in codex-rs
            ├─ stage 100  jev-prune.dedup     tool-result:read
            └─ stage 200  jev-context.view    assistant-prose, message-remove, system-append
                 └─ Codex model request
                      └─ Sentinel pre-tool checks (hooks.json)
                           └─ host permission routing
                                └─ eligible approval preflight, else existing reviewer
                                     └─ host authorization, freshness, execution
                                          └─ Sentinel post-tool observation → Fabric
Codex session <-> plaintext child-agent collaboration
```

OmniRoute appears nowhere in this diagram, and `verify_manifest.py` fails if the
excluded identifier appears anywhere outside the exclusion records.

## Verify a checkout

```sh
# manifest only
python3 jev/verify_manifest.py check

# manifest bound to real checkouts: revisions, artifact hashes,
# patch applicability and vendored jev-bus copies
python3 jev/verify_manifest.py resolve \
  --host-repo /path/to/codex-jev \
  --component codex-plaintext-collab=/path/to/codex-plaintext-collab \
  --component jev-context-fabric=/path/to/jev-context-fabric \
  --component jev-prune-kit=/path/to/jev-prune-kit \
  --component jev-sentinel=/path/to/jev-sentinel \
  --component jev-codex-approval=/path/to/jev-codex-approval
```

Exit codes: `0` verified, `2` unsupported or invalid input, `3` on-disk mismatch. A
revision that does not match the pin is a mismatch, not a warning: re-pin deliberately by
editing the manifest rather than by ignoring the difference.

## What this manifest does not claim

- It does not claim live-model accuracy, safety, or performance. Fixture results are not
  live evidence.
- It does not claim that installing a component activates it. Codex hooks require
  operator trust approval; staging is not activation.
- It does not claim platform coverage beyond the reference platform. Unlisted platforms
  are `untested` until evidence exists.
- It does not authorize paid inference, production deployment, or destructive cleanup.
