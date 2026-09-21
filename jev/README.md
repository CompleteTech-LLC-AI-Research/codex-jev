# JEV integration layer

This directory is the fork-specific home of the JEV integration for this Codex
checkout. It is owned by the integration host; the supporting components stay in
their own repositories and are consumed here at the exact revisions recorded in
[`compatibility-manifest.json`](compatibility-manifest.json).

Tracking epic: [CompleteTech-LLC-AI-Research/codex-jev#2](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/2).

## What is in here

| Path | Purpose |
| --- | --- |
| `compatibility-manifest.json` | Machine-readable pins, patch order, interface versions, feature switches, events, credentials, and ownership. |
| `profiles/` | Integration profiles. Each profile declares the feature switches for one supported configuration. |
| `fixtures/` | Deterministic, labelled offline Responses-API fixtures. Nothing here is live evidence. |
| `scripts/isolated_env.py` | Create, inspect, and roll back the isolated environment under `.jev/isolated`. |
| `scripts/offline_fixtures.py` | Loopback-only fixture service that stands in for a provider. |
| `scripts/launch_isolated.py` | Run the pinned binary against the isolated home and the fixtures. |
| `scripts/build_provenance.py` | Build the pinned CLI with the repository recipe and record provenance. |
| `scripts/verify-manifest.py` | Fail-closed validator for the manifest, a profile, the patch state, and the checkout pin. |
| `scripts/jev_manifest.py` | The validation rules, importable from tests. |
| `tests/` | Focused tests for every validation rule. |
| `patches/` | Ordered, digest-pinned patches applied to the pinned host source. |

## Scope

- Integration host: this repository.
- Supporting components: `codex-plaintext-collab`, `jev-context-fabric`,
  `jev-prune-kit`, `jev-sentinel`, `jev-codex-approval`.
- **OmniRoute is excluded.** `omniroute-codex-docker` is listed in
  `integration.excluded_repositories`, and the validator rejects any manifest
  that tries to integrate it.

## Validate this checkout

```sh
python3 jev/scripts/verify-manifest.py --patch-state applied --json
python3 jev/scripts/verify-manifest.py --profile jev/profiles/integrated-offline.json
python3 -m unittest discover -s jev/tests -t jev/tests
python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'
```

Exit codes: `0` valid, `1` validation failed, `2` usage or unreadable input.
Every failure carries a stable code such as `E_PATCH_BASE` or
`E_REMOTE_INFERENCE_UNAUTHORIZED` so an unsupported combination fails
explicitly instead of silently building.

## Read next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — where each component sits in the request lifecycle.
- [`CONTRACTS.md`](CONTRACTS.md) — the cross-component contracts and the invariants they enforce.
- [`ISOLATED_ENV.md`](ISOLATED_ENV.md) — build the pinned host and run the isolated profile offline.
- [`ROLLBACK.md`](ROLLBACK.md) — disable the integration and remove the isolated environment.
