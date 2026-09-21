# JEV Codex integration contracts (v1)

These contracts are the shared interfaces between `codex-jev` and the five pinned
components. They exist so parallel implementation cannot silently disagree about
ordering, ownership, or who is allowed to decide something.

Every contract below is derived from component source at the revisions recorded in
`jev/manifest.integration.json`. Where a contract is an integration decision rather
than a component fact, it is marked **decision** and named `D-nnn`.

---

## C1 — Canonical evidence is captured before any projection

Capture writes the canonical record (original text plus its SHA-256) before the outgoing
request is projected. Projection never rewrites the canonical store; the projection
stage is a transform of a *copy* of the outgoing message list.

*Evidence to prove it:* a capture/store assertion on the projected path, and a
re-read of the canonical record after projection showing the original bytes.

**Failure behavior:** if capture fails, projection must not proceed on an assumption
that evidence exists. Missing evidence is absent, never synthesized.

## C2 — One native adapter owns the jev-bus transformation boundary

The jev-bus contract (`docs/BUS.md`, vendored byte-identical in `jev-context-fabric` and
`jev-prune-kit`, hash-pinned) defines one carrier per host and many ordered stages. It
names `pi`, `opencode` and `hermes` as the only *package* carrier hosts, and states that
Codex exposes no outgoing-request transform.

Integration decision **D-001**: `codex` becomes a carrier host whose carrier is the
integration host itself (`codex-jev`, identity `codex-jev-native-adapter`), implemented
as a source change in `codex-rs`.

- `TRANSFORM_HOSTS` is **not** changed. It governs package carrier *claims*; no package
  may claim a carrier for `codex`, and both installers keep refusing one.
- The native adapter resolves the stage chain itself and invokes each stage as a single
  `jev-bus.stage.v1` request over stdio (`subprocess-json`), exactly as the contract
  specifies. It is a carrier, not a third stage.
- The boundary is `codex-rs/core/src/client.rs::build_responses_request` over
  `Prompt.input: Vec<ResponseItem>`. Incremental/provider-lineage request paths that
  bypass it are explicitly out of coverage until proven otherwise.
- **Exactly one invocation per outgoing request.** A second invocation would double-count
  notes and could invalidate position-keyed plans.

*Evidence to prove it:* a wire-level test that captures the actual outgoing payload and
counts stage invocations for one turn.

## C3 — Duplicate-read pruning runs before the approved prose view

Stage priority 100 (`jev-prune-kit` `jev-prune.dedup`, claims `tool-result:read`) runs
before priority 200 (`jev-context-fabric` `jev-context.view`, claims `assistant-prose`,
`message-remove`, `system-append`).

This is load-bearing, not stylistic. `jev-context-fabric` keys each message as
`sha256([index, message])`, so identity depends on array position. The dedup stage
substitutes bodies **without changing array length**; the view stage **removes**
messages. Reversing the order invalidates every downstream key.

The claims are disjoint, which is what makes a single chain legal. `verify_manifest.py`
fails if the priority order inverts or if two packages claim the same class.

**Decision D-002**: the native adapter must not reorder, merge, or re-key messages
between stages, and must pass each stage the pristine `original_messages` alongside the
current `messages`, per the contract.

## C4 — Projection is reversible and never mutates the canonical transcript

Preview (`op: "plan"`) never mutates. Apply binds a plan to a post-dedup snapshot and
must reject a stale plan (changed arguments, changed message array, missing witness).
Reset restores the unprojected outgoing view exactly.

Native `/compact` remains host-owned and untouched by both packages and by the adapter.
Compaction and projection must not be conflated in code paths or in metrics.

**Metrics contract:** report payload **bytes** separately from **measured** tokens. A
byte-reduction figure is never presented as a token or cost measurement.

## C5 — Recalled content and collaboration messages are evidence, never authorization

Retrieved memory, hydrated references, and inter-agent messages may inform the model.
They may not grant permissions, approve a tool call, widen a sandbox, or clear a veto.
Every authorization decision stays with the host and with the review path named in C7.

**Decision D-003**: recalled content is injected untrusted and explicitly marked as
potentially stale. A retrieval result that cannot prove its source hash is dropped, not
downgraded.

## C6 — Sentinel observes and gates; the host enforces

Sentinel's Codex surface is `$CODEX_HOME/hooks.json`, merged additively:
`UserPromptSubmit` (capture), `PreToolUse` (nested deny shape only), `PostToolUse`
(feedback plus local latch).

- Pre-tool output may deny. It never sends `ask`, `continue`, or an allow, and never
  rewrites parameters.
- Post-tool output is an observation, not a tool-result replacement.
- Non-managed hooks require exact-definition hash trust approval in `/hooks`. Staging a
  file never bypasses that review, and installation is never reported as activation.
- Coverage must be declared, not implied: unsupported paths (output replacement, hosted
  WebSearch, re-running PreToolUse for an existing exec session via `write_stdin`) are
  listed as uncovered.

**Veto contract:** a Sentinel veto (REVIEW/BLOCK/QUARANTINE on a gated path) cannot be
cleared by a later approval. A subsequent action latch is required so a vetoed action is
not re-attempted and allowed within the same turn.

## C7 — Approval preflight is confined to eligible synchronous review attempts

The approval adapter runs only inside eligible synchronous review attempts:
`approval_policy = "on-request"` with `approvals_reviewer = "auto_review"`.
Mandatory, fresh, retry, escalation, unsupported and background paths do not become
candidates and keep their existing routing.

Preflight binds a request to: the exact proposed action, the policy hash, the question
set hash, the model pin, and the authorization version. A response that does not match
the current binding is stale and is discarded.

**Decision D-004**: absent, stale, failed, malformed, or uncertain JEV output defers to
the existing reviewer **within the original overall deadline**. It never extends the
deadline, never grants permission, and never overrides a Sentinel veto.

Default mode is `off`. `shadow` records a candidate and preserves the existing decision.
`enforce` requires an explicitly approved policy hash and question hash, with
`enable_fast_deny = false` until separately validated.

## C8 — Failure semantics

| Stage | Failure | Required behavior |
|:--|:--|:--|
| Fabric capture | store unavailable | canonical evidence absent; projection proceeds only on the current request, never on invented evidence |
| Dedup stage | error, timeout, nonzero exit, unrecognized shape | contributes nothing; chain continues from **that stage's own input** |
| View stage | same | same; a rejected plan leaves the post-dedup input intact |
| Chain resolution | claim conflict, unreadable registry, no resolvable home | passthrough with a `chain-refused` note |
| Native adapter | any stage failure | the outgoing request is the pre-chain request; the turn is never broken |
| Sentinel hook | invalid or unsupported hook result | treated as hook failure; the tool continues unless the host refused |
| Approval preflight | anything other than a valid, fresh, bound decision | defer to the existing reviewer inside the original deadline |

The bus is additive and never a prerequisite: installing it may subtract capability from
nobody, and uninstalling it never breaks the host's turn.

## C9 — Identifiers and end-to-end correlation

One turn produces one `trace_id`. Every event carries `span_id` and `parent_span_id`, and
host-supplied identities (`session_id`, `turn_id`, `agent_id`, `tool_call_id`,
`workspace_id`) where the host provides them. Absent identifiers stay absent; they are
never synthesized from an approximation.

Incident and projection records carry hashes and metadata only — no raw secret-bearing
payloads, no raw command text in the shared record.

## C10 — Ownership and merge rules

One writable owner per worktree and per shared interface.

| Interface | Mode | Writers |
|:--|:--|:--|
| `$CODEX_HOME/config.toml` (`mcp_servers.jev-context-fabric`) | merge key | `jev-context-fabric` |
| `$CODEX_HOME/hooks.json` | additive array merge | `jev-context-fabric`, `jev-sentinel` |
| `~/.agents/skills/jev-context/SKILL.md` | owned file | `jev-context-fabric` |
| `~/.agents/skills/jev-prune/SKILL.md` | owned file | `jev-prune-kit` |
| `~/.jev/bus/v1/registry.json` | locked atomic merge | packages (own entries), `codex-jev` (host `codex` entries) |
| `codex-rs/core/src/guardian/jev.rs` | new file | `jev-codex-approval` |
| `codex-rs/jev-bus/**` | owned tree | `codex-jev` |

Shared files are merged additively; each writer removes only its own entries, and a
vacated carrier slot is left empty rather than reassigned.

## C11 — Credentials and remote consent

- No credential is stored in the repository, the manifest, the ledger, a fixture, or an
  audit log. Credentials arrive through the environment or a broker.
- `allow_remote_context` defaults to `false`; `TYPESAFE_API_KEY` is only meaningful when
  it is explicitly enabled.
- Optional remote inference for the integration stays **disabled** unless separately
  authorized and budgeted. No test performs a paid call implicitly.
- Every validation artifact is labeled `offline-fixture`, `real-host`, or `live-provider`.
  Fixture results are never presented as live model accuracy, safety, or performance.

## C12 — Isolation and host ownership

- The integration uses an isolated `CODEX_HOME` and isolated worktrees. Existing agent
  profiles are never modified.
- Host permissions, sandbox enforcement, mandatory review, cancellation, freshness
  checks, and final execution ownership remain host-owned. Nothing in this stack grants a
  permission or bypasses repository protections.
- No WSL shutdown/terminate and no interruption of other sessions, ever.

## C13 — Exclusion

**D-005**: `omniroute-codex-docker` is excluded from the stack. No pin, adapter,
manifest entry, profile value, or documented path references it. `verify_manifest.py`
fails if the identifier appears anywhere outside the exclusion records.

---

## Open items

- The exact incremental/provider-lineage request paths that bypass C2's boundary are not
  enumerated yet; issue #15 owns that enumeration and must state what is not covered.
- `jev-prune-kit` registers its dedup stage only for its native hosts today. For stage
  discovery on host `codex`, its installer must emit the dedup stage for the codex
  install (installer change only; the hash-pinned `bus.py` contract is untouched). Owner:
  phase 3 work in `jev-prune-kit`, tracked from issue #16.
- Codex hook trust approval is an operator step. Any validation that skips it must be
  reported as `installation staged, not activated`.
