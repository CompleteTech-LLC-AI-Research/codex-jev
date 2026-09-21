# JEV integration layer (codex-jev)

This directory is the fork-specific integration layer for the JEV Codex stack
(epic [#2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2)).
It pins the independently maintained components, records the cross-component
contracts, and drives the reproducible build and isolated profile.

**OmniRoute is excluded.** Nothing in this layer depends on, installs, or
references `omniroute-codex-docker`.

## Contents

| Path | Purpose |
| --- | --- |
| `manifest.json` | Machine-readable compatibility manifest: exact codex pin, immutable component revisions, patch order, runtime requirements, platforms, feature switches, shared event ids, ownership, credentials/consent. |
| `contracts/INTEGRATION_CONTRACTS.md` | The interface contracts and architectural invariants every component must honor. |
| `contracts/EVENTS.md` | Shared event identifiers and the correlation envelope used across components. |
| `scripts/verify_manifest.py` | Resolves the pinned inputs against real checkouts and fails explicitly on unsupported combinations. |
| `tests/test_verify_manifest.py` | Focused behavioral tests for the verifier's explicit-failure contract. |

The build/profile helper and rollback guide are delivered by the later phase-1
sub-issues ([#10](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/10),
[#11](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/11)).

## Status vocabulary

Every entry that is not yet proven by a real build/run is marked honestly:

- `validated` — reproduced on this machine with recorded evidence.
- `declared` — specified and wired, but not yet reproduced end-to-end.
- `unsupported` — refused explicitly at verification time.

`manifest.json` carries the per-item status so a clean checkout can tell a
declared integration from a validated one instead of assuming.

## Quick start

```sh
# Resolve pins against a set of checkouts and print a verdict.
python3 integration/scripts/verify_manifest.py \
  --manifest integration/manifest.json \
  --component-root /path/to/checkouts
```
