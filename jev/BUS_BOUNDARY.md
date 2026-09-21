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
| `JEV_BUS_VIEW` | The approved view file the carrier filters with. With none configured an item removal has no authority at the host, so the projection stays byte-only. |
| `JEV_BUS_TIMEOUT_MS` | The whole-invocation deadline; default 15000, clamped to 120000. |
| `JEV_BUS_WORKSPACE` | The workspace label the adapter records on each receipt. |

The adapter is invoked over its documented CLI (`apply --request - --json
--session … --turn … --workspace … --stage …`) with the request copy on stdin.
The host re-checks the properties it depends on — that the array is unchanged,
or that it lost exactly the items the report says an approved view removed — and
rejects the rest as unproven.

**Failure policy: every failure returns the caller's array unchanged.** A
disabled switch, no registered stage, an unconfigured adapter, a spawn error, a
deadline, a non-zero exit, malformed output, an addition, a removal with no
configured view, a removal the report does not account for exactly, and a
removal of anything but standalone assistant prose all leave the outgoing
request exactly as the host built it. That is the stage-input fallback the bus
contract requires, applied at the host boundary; the host never substitutes an
approximation, and a wedged stage can never hold a turn open.

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
  the original item. A stage that edits one is refused: the refusal is recorded as
  a `refused` **note** tagged `E_UNSUPPORTED_MUTATED` (not a raised exception), and
  the original payload is kept.
- **No unapproved reorder or removal.** A stage that changes the item order is
  refused (`E_CHAIN_SHAPE`); the original request is returned unchanged. An item
  dropped without an approved view is restored, and the `unapproved_removal` note
  names the stage the array shows dropped it, never the view stage by construction.
  An approved view is the one thing that may remove whole items, and only for the
  prose the carrier's eligibility policy admits.
- **Switches gate invocation, not just output.** A disabled switch means the
  stage is never registered, so it is never invoked. `projection.fabric_views`
  requires `projection.dedup_receipts` (`E_SWITCH_ORDER`).
- **The host invokes the adapter exactly once, or not at all.** `jev_bus.rs`
  runs the adapter once per outgoing payload when the switches resolve to an
  adapter and at least one stage command; `codex-rs/core/src/jev_bus_tests.rs`
  counts the invocations a stand-in adapter records and asserts they are zero
  when disabled and one otherwise.
- **The host re-checks what it depends on.** A projection the host cannot
  re-derive is refused by the host as well as by the adapter's `_rebuild`, so an
  unverified array is never sent even if the adapter is replaced. The array may
  stay the same length, or — only when `JEV_BUS_VIEW` names an approved view —
  lose exactly the items the adapter's own report marks removed, and only when
  every one of those items is standalone assistant prose the host itself
  recognizes as removable and the surviving array is the incoming array minus
  those positions in order. An addition, a reorder, a removal the report does not
  account for, a duplicate or out-of-range position, a removal with no configured
  view, and a removal of anything carrying a tool call, reasoning, an image, or
  audio are all refused (`item-count`), and the caller's array is returned
  unchanged. The host does not decide *which* prose is approvable: it bounds what
  may leave, while the carrier's `fabric_views.py` owns the eligibility policy.

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
- **Each projected turn pays a process cost nothing has measured yet.** With the
  switches on, the host runs the adapter as one `python3` process per outgoing
  payload, and the adapter runs one more process per registered stage
  (`JEV_BUS_STAGE_*` is a command line executed by `_subprocess_invoke`), so the
  two-stage chain is three processes and a re-serialized round trip per turn.
  The chain is serial, the payload handed over is bounded at 8 MiB
  (`MAX_INPUT_BYTES`), and the whole call is cut off at `JEV_BUS_TIMEOUT_MS`
  (default 15000, clamped to 120000) with the child killed. No run here measures
  that latency — [#25](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/25)
  owns the performance validation. The
  [#70](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/70) row
  below does launch a real host turn, but it records the bytes that turn sent,
  not how long the chain took.
  With the switches off — the declared default and every profile — the cost is
  exactly zero, because no process is spawned.
- **The reduction is now shown by a launched host, but only on a seeded
  transcript.** The `real-host-binary` row below starts a real `codex` process,
  resumes a session whose transcript already carries a duplicate read pair, and
  reads the bytes it put on the wire. That closes the gap
  [#70](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/70)
  was filed for, but it does not show a host *discovering* a pair on its own:
  this revision exposes no `read`, `read_file`, or `file_read` tool, so a pair
  can only enter the transcript from a resumed rollout. There is still no
  live-provider run and no token measurement.

## Evidence

All results below are the `offline-fixture` tier unless noted. No live provider
call was made.

| Check | Result |
| --- | --- |
| `jev/tests/test_bus_boundary.py` wire-request tests | 24 tests, ok (23 on `main` `aef58a0c2` before this change): ordering, single invocation, no mutation, opaque retention, refusal, fallback, deadline, switches, receipts, bounds, removal attribution. |
| `codex-rs/core/src/jev_bus_tests.rs` host-boundary tests | 9 tests, ok: exact single invocation, replacement of the outgoing view only, byte-identical opaque retention, and passthrough on a disabled switch, a missing adapter, a failure, a deadline, malformed output, and a changed item count. |
| `.github/scripts/test_jev_bus.py` — the required-CI home (`repo-checks` discovers `.github/scripts/test_jev_*.py`; it does not run `jev/tests`) | 28 tests, ok: the same invariants, the receipt rule (including a declined response earning nothing), and three `bus-stage-stub` subprocess-transport runs. |
| `.github/scripts/test_jev_bus_host.py` — the host-invocation home | 10 tests, ok: the switch names the launcher exports are byte-identical to the names the native boundary reads, the adapter's documented CLI reduces a real request over the real subprocess transport, one `projection_receipt` is written per applied stage, and the disabled/declining/failing paths return the request unchanged. |
| Required CI battery (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) | 341 tests, ok (336 on `main` `aef58a0c2` before this change). |
| `jev/tests` | 160 tests, ok (159 on `main` `aef58a0c2` before this change). |
| `verify-manifest.py --patch-state applied` | ok. |
| `real-component` transport run, proven pair (`bus_boundary.py apply` against the pinned `jev-prune-kit` checkout at `2ecc8ff4`, `runner.py --bus-stage`) | Stage 100 invoked; the substitution is proven, so `C3` accepts it (`accepted: [1]`, `reverted: []`); marker `[Jev prune: repeated read-result body omitted; retained witness: call_2]` in the outgoing payload; `call_2` body retained; one `projection_receipt` at stage 100; item count preserved; on-disk request unchanged. |
| `real-component` transport run, protected pair (same pinned stage, pair inside the current turn) | The component still substitutes; `C3` reverts it (`reverted: [{index: 2, reason: protected_turn}]`), the outgoing request equals the input byte-for-byte, and stage 100 holds no receipt. |
| `bus-stage-stub` transport runs (`subprocess`, checked-in doubles) | Both stages invoked in order `[jev-prune.dedup, jev-context-fabric.view]`; one `projection_receipt` per applied stage; the opaque `reasoning` item byte-identical; an unproven substitution over the wire is reverted. |
| Host-invocation run (`test_jev_bus_host.py` through the adapter the host resolves) | The adapter is reached with the host's argv, stdin, and environment; the duplicate read is replaced by a retained-witness marker the host-side receipt enforcement accepts; assistant prose is replaced by the view marker; the outgoing view is strictly smaller than the incoming one; the source request is untouched. |
| Disabled-switch run | `invoked: []`, note `disabled`, outgoing request byte-identical to the input. |
| `real-host-binary` run — `jev/smoke/run-projection-reset-smoke.sh` against a `codex` built from `8ca45b6a7a` (binary sha256 `8da29416…`), three turns resuming one seeded session | Switch off: the recorded `input` is byte-identical to the unswitched control (9753 bytes, 35 items) and no `projection_receipt` appears. Switch on with a receipt in the pinned component's own proof format: the recorded `input` is strictly smaller (9178 bytes, 575 saved), the item count is unchanged at 35, the source body carries the omission marker while the witness body is retained, and the chain emits one `projection_receipt` at stage 100 with `accepted: [3]` and no reverts. All three rollouts still hold the original read body, so nothing was persisted. Recorded in [`evidence/projection-real-host.md`](evidence/projection-real-host.md). |

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
compile or run a Codex binary; only the `real-host-binary` row above does. No
row is a live-provider measurement: the reduction is measured in serialized
bytes on a fixture request, never in tokens and never against a real model.
