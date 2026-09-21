# Native request adapter and jev-bus boundary

This document records where the Codex host builds its outgoing request, how the
integration takes ownership of that one boundary, and what the adapter refuses.
It backs issue [#15](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/15)
and contract `C2` in [`CONTRACTS.md`](CONTRACTS.md).

The boundary has two halves, and `ARCHITECTURE.md` names both kinds of change:
the **native adapter** is the host module written in this repository
(`codex-rs/core/src/jev_bus.rs`, behind switches whose `false` defaults the
manifest declares), and the **ordered patch** is the recorded source change that
puts its call site in `client.rs`
(`jev/patches/0002-jev-bus-boundary.patch`, `order` 20, targeting the pinned base
`8198a91a4f46b01647bc6c0d8d63afafbf4c9180`). The Python adapter in `jev/scripts`
is the process the host module runs.

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
| Host call site | `codex-rs/core/src/jev_bus.rs` | The host-owned `jev_bus_call_site`: the one outgoing-request boundary the host projects. |

The full and incremental paths converge on the same array, so the adapter treats
the wire ``input`` array as the one boundary. It does not care whether the array
is the whole transcript or a websocket delta: both are the same ordered list of
typed items.

## Who invokes the adapter

The adapter is only reachable because the host calls it. `codex-rs/core/src/jev_bus.rs`
owns the host half of the boundary and is invoked from `client.rs` in
`stream_responses_api` and `stream_responses_websocket`, immediately after
`filter_tool_result_metadata` and before `prepare_response_items_for_request`, in
both cases on the `request` the session has already built. One projection happens
per outgoing payload. On the websocket path the continuation delta is derived
from the same array afterwards, so the incremental payload carries the projected
view as well.

The host reads the wiring from the environment, so the isolated profile owns it:

| Variable | Meaning |
| --- | --- |
| `JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS`, `JEV_SWITCH_PROJECTION_FABRIC_VIEWS` | Enable the stages. Only the exact value `1` is on. |
| `JEV_BUS_ADAPTER` | The adapter to run (`jev/scripts/bus_boundary.py`). |
| `JEV_BUS_PYTHON` | Its interpreter; defaults to `python3`. |
| `JEV_BUS_STAGE_DEDUP`, `JEV_BUS_STAGE_FABRIC_VIEW` | One stage command each. A stage whose switch is on but whose command is unset is not registered, so a stage is never invented. |
| `JEV_BUS_TIMEOUT_MS` | The whole-invocation deadline; default 15000, clamped to 120000. |
| `JEV_BUS_WORKSPACE` | The workspace label the adapter records on each receipt. |

The adapter is invoked over its documented CLI (`apply --request - --json
--session … --turn … --workspace … --stage …`) with the request copy on stdin.
The host re-checks the one property it depends on — that the item count is
unchanged — and rejects the rest as unproven.

**Failure policy: every failure returns the caller's array unchanged.** A
disabled switch, no registered stage, an unconfigured adapter, a spawn error, a
deadline, a non-zero exit, malformed output, and a changed item count all leave
the outgoing request exactly as the host built it. That is the stage-input
fallback the bus contract requires, applied at the host boundary; the host never
substitutes an approximation, and a wedged stage can never hold a turn open.

The host does **not** decide policy. It does not know which reads are duplicates,
which prose is approvable, or what a receipt must prove: those stay with
`jev-prune-kit`, `jev-context-fabric`, and the host-side receipt enforcement in
`jev/scripts/dedup_receipts.py`.

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
5. **Records one `projection_receipt` per applied stage.** A stage is applied
   unless it both reported a bare `passthrough` and handed back the array it was
   given. The `action` strings inside a stage's notes are another package's
   wording and the `jev-bus.v1` contract does not fix them, so the array the
   stage returned is the evidence that decides: a stage that substitutes bodies
   and also reports an unrelated passthrough keeps its receipt.
   Only an array the bus itself accepted counts: `run_chain` declines any
   response without `ok: true` (and, for a `transform`, without a message list),
   keeps that stage's own input and records the decline as a passthrough, so a
   mutation carried in a rejected array is never reported as an applied stage and
   never earns a receipt.
   A stage whose every change the `C3` receipt enforcement reverts also loses
   its receipt, because a receipt records a projection that reached the wire;
   the refusal is recorded as a `reverted` note and in `report.dedup.reverted`.

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
- **The host invokes the adapter exactly once, or not at all.** `jev_bus.rs`
  runs the adapter once per outgoing payload when the switches resolve to an
  adapter and at least one stage command; `codex-rs/core/src/jev_bus_tests.rs`
  counts the invocations a stand-in adapter records and asserts they are zero
  when disabled and one otherwise.
- **The host re-checks what it depends on.** A projection that changes the item
  count is refused by the host as well as by the adapter's `_rebuild`, so an
  unverified array is never sent even if the adapter is replaced. Because the
  host cannot re-derive an approval the carrier holds, this same rule also
  refuses an *approved* view removal, so the host path reduces bytes (dedup
  replacements) but never items until an authority the host can verify is
  wired; that reconciliation is tracked separately (#58).

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

## Known limits

- **The receipt carries no capture identity yet.** A receipt's `capture_id` is
  taken from the `id` a stage puts on its note, and the pinned stage 100 note
  (`{"action":"duplicate read bodies substituted","count":N,"bytes":M}`) has no
  `id`, so the field is `""` and the substituted count and byte total are not
  carried onto the receipt. Binding a receipt to the pinned witness identity
  is available but unclaimed: the `C3` proof that accepted the substitution does
  name the retained witness (`report.dedup.receipts[].witness.id`), so the
  identity could be bound onto the receipt rather than guessed from the note's
  wording, which the boundary deliberately does not do. `test_jev_bus` pins the
  current behavior so the gap stays visible.
- **The checked-in stage doubles are a wire contract, not component evidence.**
  `jev/tests/bus_stage_stub/` speaks `jev-bus.stage.v1` so that required CI can
  exercise the subprocess transport without the component repositories checked
  out. Every such run is tier `bus-stage-stub`. The real stage semantics remain
  owned by `jev-prune-kit` and `jev-context-fabric`.

## Evidence

All results below are the `offline-fixture` tier unless noted. No live provider
call was made.

| Check | Result |
| --- | --- |
| `jev/tests/test_bus_boundary.py` wire-request tests | 23 tests, ok: ordering, single invocation, no mutation, opaque retention, refusal, fallback, deadline, switches, receipts, bounds. |
| `codex-rs/core/src/jev_bus_tests.rs` host-boundary tests | 9 tests, ok: exact single invocation, replacement of the outgoing view only, byte-identical opaque retention, and passthrough on a disabled switch, a missing adapter, a failure, a deadline, malformed output, and a changed item count. |
| `.github/scripts/test_jev_bus.py` — the required-CI home (`repo-checks` discovers `.github/scripts/test_jev_*.py`; it does not run `jev/tests`) | 28 tests, ok: the same invariants, the receipt rule (including a declined response earning nothing), and three `bus-stage-stub` subprocess-transport runs. |
| `.github/scripts/test_jev_bus_host.py` — the host-invocation home | 10 tests, ok: the switch names the launcher exports are byte-identical to the names the native boundary reads, the adapter's documented CLI reduces a real request over the real subprocess transport, one `projection_receipt` is written per applied stage, and the disabled/declining/failing paths return the request unchanged. |
| Required CI battery (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) | 198 tests, ok (188 before this change). |
| `jev/tests` | 93 tests, ok. |
| `verify-manifest.py --patch-state applied` | ok. |
| `real-component` transport run, proven pair (`bus_boundary.py apply` against the pinned `jev-prune-kit` checkout at `2ecc8ff4`, `runner.py --bus-stage`) | Stage 100 invoked; the substitution is proven, so `C3` accepts it (`accepted: [1]`, `reverted: []`); marker `[Jev prune: repeated read-result body omitted; retained witness: call_2]` in the outgoing payload; `call_2` body retained; one `projection_receipt` at stage 100; item count preserved; on-disk request unchanged. |
| `real-component` transport run, protected pair (same pinned stage, pair inside the current turn) | The component still substitutes; `C3` reverts it (`reverted: [{index: 2, reason: protected_turn}]`), the outgoing request equals the input byte-for-byte, and stage 100 holds no receipt. |
| `bus-stage-stub` transport runs (`subprocess`, checked-in doubles) | Both stages invoked in order `[jev-prune.dedup, jev-context-fabric.view]`; one `projection_receipt` per applied stage; the opaque `reasoning` item byte-identical; an unproven substitution over the wire is reverted. |
| Host-invocation run (`test_jev_bus_host.py` through the adapter the host resolves) | The adapter is reached with the host's argv, stdin, and environment; the duplicate read is replaced by a retained-witness marker the host-side receipt enforcement accepts; assistant prose is replaced by the view marker; the outgoing view is strictly smaller than the incoming one; the source request is untouched. |
| Disabled-switch run | `invoked: []`, note `disabled`, outgoing request byte-identical to the input. |

The transport runs exercise the real `subprocess` stage path. The
`real-component` row drives the pinned `jev-prune-kit` stage over its own
checked-out `runner.py --bus-stage` with a receipt that the component's own
`assess` path would produce; the `bus-stage-stub` row drives the checked-in
doubles, which is the only transport evidence required CI can reproduce without
the component repositories. The live `jev-context-fabric` prose stage, a live
provider call, and the component stages' own semantics are not exercised here
and remain their own suites' subject.

The `codex-rs` tests build the real host module and drive it against a hermetic
stand-in adapter, so they prove the host boundary and its fallbacks. They do not
compile or run a Codex binary, and no test above is a live-provider
measurement: the reduction is measured in serialized bytes on a fixture request,
never in tokens and never against a real model.
