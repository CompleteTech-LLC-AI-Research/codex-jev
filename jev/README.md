# JEV integration assets

This directory holds the fork-specific integration assets for the JEV stack
(`codex-plaintext-collab`, `jev-context-fabric`, `jev-prune-kit`, `jev-sentinel`,
`jev-codex-approval`) on top of the pinned Codex fork.

It is intentionally outside `docs/`: the upstream `AGENTS.md` reserves `docs/` for
upstream product documentation and the app-server API, and these files describe a
fork-specific integration rather than upstream behaviour.

| Path | Purpose |
| --- | --- |
| `manifest.json` | Machine-readable compatibility manifest: pinned revisions, patch order, runtimes, platforms, feature switches, ownership, and the combinations that must fail explicitly. |
| `contracts/architecture.md` | The integrated lifecycle, component boundaries, and the excluded components. |
| `contracts/jev-bus-codex.md` | The single native jev-bus transformation boundary that owns outgoing-request projection. |
| `contracts/event-identifiers.md` | Event stages, identity derivation, and correlation rules. |
| `contracts/events.v1.json` | Machine-readable event stage list and required fields. |
| `contracts/ownership.md` | Which component owns which path, and what no component may do. |
| `contracts/compatibility.md` | Compatibility, upgrade, disable, and rollback rules. |
| `tools/verify_manifest.py` | Resolves a clean checkout against the manifest and fails explicitly for unsupported combinations. |
| `tests/test_manifest.py` | Offline tests for manifest resolution and the explicit-failure rules. |

## Quick check

```sh
python3 jev/tools/verify_manifest.py --checkout .
python3 -m unittest discover -s jev/tests -t .
```

`verify_manifest.py` exits `0` only when the checkout resolves to the pinned inputs.
Unsupported combinations exit `2` with a single explicit reason; internal errors exit `3`.
