# Integration contracts

These contracts bind the components that make up the JEV Codex stack. They exist
so that six independently maintained repositories can be pinned into one build
without granting any of them authority that Codex owns.

OmniRoute is excluded from this stack.

## 1. Pipeline stages and ownership

```
Codex session
  -> Fabric capture (canonical evidence)         jev-context-fabric   [stage 1]
  -> Native jev-bus projection (dedup -> view)   codex-jev (adapter)  [stage 2]
        driven by jev-prune-kit (dedup, priority 100)
        driven by jev-context-fabric (view, priority 200)
  -> Codex model request                          codex-jev (host)
  -> Sentinel pre-tool checks                     jev-sentinel         [stage 3]
  -> Host permission routing                      codex-jev (host)
  -> Eligible JEV preflight or existing review    jev-codex-approval   [stage 4]
  -> Host authorization and freshness             codex-jev (host)
  -> Execution                                    codex-jev (host)
  -> Sentinel post-tool observation               jev-sentinel         [stage 3]
  -> back to Fabric capture
Codex session <-> plaintext child-agent collaboration  codex-plaintext-collab
```

One owner per boundary. The host owns the two request/tool boundaries; a
component never registers a second transform or a second veto path for a host
that the host already owns.

## 2. Architectural invariants

1. **Capture before projection.** Canonical evidence is recorded before any
   outgoing request is transformed. A projection failure preserves the stage's
   own input rather than a subtree of it.
2. **Original transcripts are preserved.** Pruning edits only the outgoing view.
   Native `/compact` and its counters stay host-owned and functional.
3. **One native adapter owns the jev-bus boundary.** Exactly one Rust adapter in
   `codex-rs` registers the outgoing-request transform and drives the bus chain.
4. **Dedup runs before the approved prose view.** Priority 100 precedes 200, per
   `jev-bus v1`; the Fabric stage keys on array position, so dedup (which does not
   change array length) must run first.
5. **Recalled content and collaboration messages are evidence, never
   authorization.** Retrieved or recalled text cannot grant a permission, widen
   a sandbox, or approve an action.
6. **Sentinel vetoes cannot be cleared by later approval.** A veto is terminal
   for the bound action.
7. **Approval preflight applies only to eligible review attempts.** Disabled,
   ineligible, mandatory, fresh, retry, and escalation paths keep their existing
   routing; preflight sits inside the eligible synchronous attempt only.
8. **Host authority is preserved.** Permissions, sandbox enforcement, mandatory
   review, cancellation, freshness checks, and final execution ownership stay
   with Codex.
9. **Approval failure or uncertainty defers to the existing reviewer** within the
   original overall deadline; the deadline is never reset to hide preflight
   latency.
10. **Bytes are not tokens.** Any size metric names its unit; a byte reduction is
    never reported as a measured token reduction.
11. **Remote inference stays off unless separately authorized and budgeted.**
    Joining a chain never starts a paid call.

## 3. Component contracts

### codex-plaintext-collab -> codex-jev

- Input: the patch at the pinned commit and its `sha256`.
- Applied at build time by the integration layer; never edited in place.
- Guarantee: removes the client-side `encrypted` marking on the collaboration
  `message` parameter so new `spawn_agent` / `send_message` / `followup_task`
  payloads are stored and forwarded as plaintext.
- Explicit non-guarantee: existing ciphertext is **not** decrypted and remains
  unreadable locally. Authorization behavior is unchanged.

### jev-context-fabric <-> jev-prune-kit (jev-bus v1)

- The vendored `bus.py` and `bus.mjs` must be byte-identical in both repositories.
- Claims are from the closed vocabulary and must be disjoint across packages.
- `original_messages` is authoritative for any stage that keys or fingerprints
  messages.
- A stage may decline, time out, or fail; it must never break the host's turn,
  and the chain continues with that stage's own input.
- Any failure to resolve the chain degrades to passthrough with a
  `chain-refused` note. The bus is additive, never a prerequisite.

### jev-context-fabric <-> codex-jev

- Fabric owns capture, retrieval/hydration, and the MCP surface.
- codex-jev owns the transform boundary and the single adapter that calls bus
  stages. Fabric never registers a Codex transform itself.

### jev-sentinel <-> codex-jev

- Sentinel observes prompt, pre-tool, and post-tool events with bounded,
  normalized payloads and returns REVIEW/BLOCK/QUARANTINE outcomes.
- Installation alone is never reported as activation; coverage reports state
  exactly which tools and event paths are instrumented.
- Quarantine withholds retrieved context from the active view without erasing
  canonical evidence.

### jev-codex-approval <-> codex-jev

- The native adapter lives at `codex-rs/core/src/guardian/jev.rs` and is invoked
  only from inside the eligible synchronous review attempt.
- It consumes the pinned blobs, validates every typed answer, checks freshness,
  and returns either a `GuardianAssessment` or nothing.
- Nothing it returns is an instruction to execute; the host's original
  authorization, reporting, and cancellation logic remains authoritative.
- Its only subprocess is the fixed, operator-selected adapter; it never passes
  the proposed action to a subprocess.

## 4. Cross-component event identifiers

See `EVENTS.md`. Every component emits into the `jev.event.v1` envelope so a
single trace can be correlated across capture, projection, screening, approval,
authorization, and execution.

## 5. Failure and disable semantics

| Failure | Required behavior |
| --- | --- |
| Projection stage error/timeout | Chain continues with that stage's input; original content retained for unsupported shapes or outright projection failure. |
| Approval preflight timeout/unavailable | Fall back to the existing reviewer inside the original deadline. |
| Approval preflight stale/malformed | No permission granted; existing reviewer runs. |
| Sentinel veto | Action blocked; a later approval cannot clear it. |
| Capture failure | Host turn proceeds; the gap is recorded, not silently ignored. |
| Any component disabled | The stack reproduces original Codex behavior for that boundary. |

## 6. Credentials and remote consent

Each component keeps its own credential file. There is no shared credential and
no cross-component token reuse. Remote inference requires an explicit consent
flag per component plus `JEV_ALLOW_REMOTE_INFERENCE`; no other path starts a paid
call. Credentials and private captured content must not appear in fixtures,
logs, issue attachments, or committed records.
