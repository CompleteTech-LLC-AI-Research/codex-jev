# JEV integration architecture

## Lifecycle

```mermaid
flowchart TD
    C[Codex session] --> I[Sentinel prompt check]
    I --> F[Fabric capture and retrieval]
    F --> P[Native jev-bus projection: dedup then approved prose view]
    P --> M[Codex model request]
    M --> S[Sentinel pre-tool checks]
    S --> H[Host permission routing]
    H --> J[Eligible JEV preflight or existing review]
    J --> V[Host authorization and freshness]
    V --> E[Execution]
    E --> O[Sentinel post-tool observation]
    O --> F
    C <--> A[Plaintext child-agent collaboration]
```

## Boundaries

| Boundary | Owner | Notes |
| --- | --- | --- |
| `jev_bus` stage ordering and single invocation | `jev-prune-kit` contract, hosted by the native adapter in this repository | Stage 100 is duplicate-read dedup; stage 200 is the approved Fabric view. |
| Event envelope and correlation identifiers | this repository | Other components emit envelopes; only the host defines the schema. |
| Memory tools and retrieval | `jev-context-fabric` | Capture happens before projection; retrieval is budgeted and source-backed. |
| Boundary observation and vetoes | `jev-sentinel` | Prompt, pre-tool, and post-tool; shadow first; enforcement only where a veto is representable. Host carrier in [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md). |
| Approval preflight | `jev-codex-approval` | Eligible review attempts only; failure or uncertainty defers to the existing reviewer. |
| Authorization, sandbox, cancellation, compaction, final execution | this repository (host) | Never delegated to a component. |

## Exclusions

OmniRoute is excluded from the integrated stack. It is not a manifest component,
it is listed under `integration.excluded_repositories`, and the validator
fails closed if it appears as a component. No routing, gateway, or deployment
asset from that repository is part of this architecture.

## Components that change host behavior

Only two kinds of integration change this repository's source:

1. **Ordered patches** recorded in `compatibility-manifest.json` under `patches`,
   each pinned by digest and by the host base commit it applies to.
2. **Native adapters** written in this repository behind a feature switch whose
   default is declared in the manifest.

Everything else stays in the owning component repository and is consumed through
an interface with a declared version.

## Evidence tiers

Every validation claim is labelled with the tier that produced it:

| Tier | Meaning |
| --- | --- |
| offline fixture | Deterministic local tests with injected inputs and no model calls. |
| real host, mocked service | The real Codex host code path with the Responses API mocked; this proves host wiring, not model safety. |
| live provider | A real model call. This requires explicit configuration, a positive budget, and operator consent; it is never implicit. |

Fixture results are never presented as evidence of live model accuracy, safety,
or performance.
