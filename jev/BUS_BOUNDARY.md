# Native request adapter and jev-bus boundary

This document records where the Codex host builds its outgoing request, how the
integration takes ownership of that one boundary, and what the adapter refuses.
It backs issue [#15](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/15)
and contract `C2` in [`CONTRACTS.md`](CONTRACTS.md).

## Where the boundary is

Codex builds the outgoing request in exactly one place per turn, and only one
field of it is the model-visible context: the serialized ``ResponseItem`` array
in ``input``.

| Path | Location | Role |
| --- | --- | --- |
| Request type | `codex-rs/codex-api/src/common.rs:260` | `ResponsesApiRequest`; `input: Vec<ResponseItem>` at `:264`. |
| Full request | `codex-rs/core/src/client.rs:966` | The single `ResponsesApiRequest` construction site. |
| Provider lineage | `codex-rs/core/src/client.rs:1948` | `previous_response_id` plus `incremental_items` for the websocket continuation. |
| Incremental wire | `codex-rs/core/src/client.rs:1966` | `ResponseCreateWsRequest { input: incremental_items.as_deref().unwrap_or(&request.input), .. }` at `:1968`. |
| Shape vocabulary | `codex-rs/protocol/src/models.rs:1010` | `#[serde(tag = "type", rename_all = "snake_case")]` on `enum ResponseItem` at `:1011`. |

The full and incremental paths converge on the same array, so the adapter treats
the wire ``input`` array as the one boundary. It does not care whether the array
is the whole transcript or a websocket delta: both are the same ordered list of
typed items.

## What the adapter does

`jev/scripts/bus_boundary.py` is the host's carrier for the `codex` host. It:

1. **Normalizes only supported shapes.** A `ResponseItem` is mapped onto a bus
   message only when another component can own it:
   - `message` -> a role/content message (the approved prose view, stage 200);
   - `function_call` -> an assistant tool call (the pair partner for stage 100);
   - `function_call_output` -> a tool result (duplicate-read dedup, stage 100).

   Every other shape — `reasoning`, `web_search_call`, `image_generation_call`,
   `agent_message`, `local_shell_call`, and the rest — is **opaque**. It is
   carried through the chain tagged with its original index and restored from
   the original item verbatim.
2. **Invokes one bus owner.** It calls the vendored `jev-bus.v1` contract once.
   The bus orders the registered stages by priority, so stage 100
   (`jev-prune-kit`) always runs before stage 200 (`jev-context-fabric`). The
   priority, owner, and package are asserted against
   `compatibility-manifest.json` `events.stages` at load, so the manifest and the
   adapter cannot drift apart silently.
3. **Enforces bounds, deadlines, and ownership.** The request is refused above
   the bus wire bound (`8 MiB`); each stage has a per-stage timeout and the chain
   has a whole-chain deadline; a stage is skipped once the deadline is exhausted.
4. **Falls back to each stage's input.** A stage that errors, times out, or
   returns an unrecognized shape contributes nothing and the chain continues from
   that stage's own input — the behavior the bus already guarantees — and the
   adapter records a `passthrough` note.

## Invariants the adapter proves

- **Ordered single invocation.** The chain is the manifest's projection stages in
  priority order; each enabled stage is invoked exactly once.
- **No canonical transcript mutation.** The caller's request object is never
  modified. Only a freshly built outgoing payload replaces `input`.
- **Unsupported shapes retain original content.** An opaque item is restored from
  the original item. A stage that edits one is refused (`E_UNSUPPORTED_MUTATED`).
- **No reorder or removal.** A stage that changes the item count or order is
  refused (`E_CHAIN_SHAPE`); the original request is returned unchanged.
- **Switches gate invocation, not just output.** A disabled switch means the
  stage is never registered, so it is never invoked. `projection.fabric_views`
  requires `projection.dedup_receipts` (`E_SWITCH_ORDER`).

## Disable behavior

With projection disabled (the `baseline` profile and every isolated profile,
where `projection.*` defaults are `false`), no stage is registered, the chain is
empty, the request is returned byte-for-byte, and no receipt is written. This is
the same disable path `C8` describes: the pinned base without the integration
reproduces the original behavior.

## The vendored bus is byte-identical

The `jev-bus.v1` contract is owned by `jev-prune-kit` and vendored
byte-identically into every participating package. The host vendors the same file
as `jev/scripts/jev_bus.py`; `test_bus_boundary.VendoredBusTests` asserts it
matches the pinned component copy (`sha256
514e59a2776ae1adbed5bbcf8e669d4fa15972877440a15cf8b00516bbfb87b7`) whenever
that checkout is present.

## Evidence

All results below are the `offline-fixture` tier unless noted. No live provider
call was made.

| Check | Result |
| --- | --- |
| `jev/tests/test_bus_boundary.py` wire-request tests | 20 tests, ok: ordering, single invocation, no mutation, opaque retention, refusal, fallback, deadline, switches, receipts, bounds. |
| Required CI battery (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) | ok (includes the new tests). |
| `verify-manifest.py --patch-state applied` | ok. |
| `real-host` transport run (`bus_boundary.py apply` over the subprocess transport) | Both stages invoked in order `[jev-prune.dedup, jev-context-fabric.view]`; `projection_receipt` per stage; the opaque `reasoning` item byte-identical; the duplicate read body replaced in the outgoing payload while the on-disk request stayed unchanged. |
| Disabled-switch run | `invoked: []`, note `disabled`, outgoing request byte-identical to the input. |

The transport run exercises the real `subprocess` stage path with a loopback
fixture stage; it proves host wiring, not the safety of a live model or of the
component semantics. Skipped checks: nothing stage-specific; the component
stages are exercised through the bus contract, and their own suites remain the
owner of their semantics.
