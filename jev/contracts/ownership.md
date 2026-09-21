# Ownership boundaries

One writable owner per path. A component owns its repository, its files, and its
install receipt; the host owns permissions and execution. Any change that crosses a
boundary below needs the owning repository's own PR and review.

| Path or interface | Owner | Not allowed |
| --- | --- | --- |
| Outgoing-request transform boundary | this fork (`codex-jev`) | A stage package registering its own transform; a second carrier on this host. |
| Duplicate-read substitution (priority 100) | `jev-prune-kit` | Touching messages the current user turn covers, or changing the array length. |
| Approved prose view (priority 200) | `jev-context-fabric` | Removing prose without a locally approved plan, or re-keying the post-dedup snapshot. |
| Canonical capture and retrieval | `jev-context-fabric` | Writing to the host transcript, or treating recalled text as authorization. |
| Boundary observation, incidents, vetoes | `jev-sentinel` | Granting or widening permission; replacing host output. |
| Eligible review preflight | `jev-codex-approval` | Running outside eligible synchronous review attempts; overriding a Sentinel veto; submitting remote context without consent. |
| Collaboration message plaintext | `codex-plaintext-collab` | Decrypting existing ciphertext, or changing who may read a message. |
| Permissions, sandbox, cancellation, review requirement, compaction, execution | this fork (`codex-jev`) | Delegating any of these to a component. |

## Shared interfaces

`jev-bus.stage.v1` is vendored byte-identically in `jev-context-fabric` and
`jev-prune-kit`, and their test suites fail if the two copies drift. The native
carrier consumes that same protocol, so the frozen contract has three independent
consumers.

## Excluded

`omniroute-codex-docker` owns nothing in this integration and is not a dependency of
any component listed above.
