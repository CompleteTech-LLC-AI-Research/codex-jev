#!/usr/bin/env python3
"""The native request adapter and jev-bus boundary for the Codex host.

Codex builds exactly one outgoing request for a turn. The full request is a
``ResponsesApiRequest`` whose ``input`` is the serialized ``ResponseItem``
array, and the websocket continuation path reuses the same field with a
``previous_response_id`` and only the incremental items. Both paths converge on
``input`` in ``codex-rs/core/src/client.rs`` (see ``BUS_BOUNDARY.md``), so this
adapter treats the wire ``input`` array as the single transformation boundary.

At that boundary the host invokes one bus owner, the vendored ``jev-bus.v1``
contract, exactly once. The bus orders the registered stages by priority: stage
100 is ``jev-prune-kit`` duplicate-read dedup and stage 200 is
``jev-context-fabric`` approved prose view. This module never edits the
canonical transcript it is handed; it builds a new outgoing payload, and only
supported message shapes can change. A stage that errors, times out, reorders,
removes, or returns an unrecognized shape contributes nothing and the chain
continues from that stage's own input.

Subcommands:

``apply``   resolve the boundary, run the chain, and print the outgoing request;
``plan``    run each stage in plan mode without returning transformed messages;
``switch``  report the resolved ``JEV_SWITCH_*`` state and the stage chain.

Exit codes: 0 ok, 1 refused, 2 usage error.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_bus  # vendored jev-bus.v1, byte-identical to the pinned components
import jev_manifest
import dedup_receipts
import fabric_views

HOST = "codex"
ADAPTER = "codex-jev"
RECEIPT_KIND = "projection_receipt"
WIRE_LIMIT = jev_bus.MAX_WIRE
DEDUP_STAGE = "jev-prune.dedup"
VIEW_STAGE = "jev-context-fabric.view"

# Stable refusal codes. A boundary that cannot prove its invariants refuses
# rather than approximating a transform.
E_INPUT_SHAPE = "E_INPUT_SHAPE"
E_WIRE_BOUND = "E_WIRE_BOUND"
E_SWITCH_ORDER = "E_SWITCH_ORDER"
E_STAGE_MISMATCH = "E_STAGE_MISMATCH"
E_CHAIN_SHAPE = "E_CHAIN_SHAPE"
E_UNSUPPORTED_MUTATED = "E_UNSUPPORTED_MUTATED"

# Codex ``ResponseItem`` variants serialize with ``type`` in snake_case. Only
# these three shapes are normalized onto the bus; everything else is opaque and
# is restored verbatim, so no stage can ever see or alter it.
SUPPORTED_SHAPES = ("message", "function_call", "function_call_output")

# The projection stages the manifest declares, keyed by the manifest's stage id.
# Ownership and order are asserted against the manifest at load, so the two can
# never drift apart silently.
STAGE_SPECS = {
    "dedup": {
        "package": "jev-prune-kit",
        "name": "jev-prune.dedup",
        "claims": ["tool-result:read"],
        "switch": "projection.dedup_receipts",
    },
    "fabric_view": {
        "package": "jev-context-fabric",
        "name": "jev-context-fabric.view",
        "claims": ["assistant-prose"],
        "switch": "projection.fabric_views",
    },
}


class BoundaryError(ValueError):
    """A bounded, non-sensitive refusal. Its message never contains context text."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def load_manifest():
    return jev_manifest.load_json(jev_manifest.default_manifest_path(), "manifest")


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fingerprint(messages) -> str:
    """A total, order-sensitive snapshot of a stage's message array."""
    try:
        return _canonical(messages)
    except (TypeError, ValueError):
        return repr(messages)


def _tagged(messages) -> dict[int, str]:
    """Map each boundary tag to a fingerprint of the item carrying it.

    Tags are what make an exact comparison possible: a removal is the tag that
    disappeared, not a length change that shifts every later position, so a
    stage's removals can be attributed to that stage instead of guessed from the
    array's new length.
    """
    if not isinstance(messages, list):
        return {}
    tagged = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        index = message.get("_jev_index")
        if isinstance(index, int):
            tagged[index] = _fingerprint(message)
    return tagged


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------- shapes


def _text_from_parts(parts) -> str | None:
    """Join a Codex content array into text; ``None`` when it is not text-only."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return None
    chunks = []
    for part in parts:
        if not isinstance(part, dict):
            return None
        text = part.get("text")
        if not isinstance(text, str):
            return None
        chunks.append(text)
    return "\n".join(chunks)


def _opaque(index: int, item) -> dict:
    """Tag a shape no stage may own, carrying a deep copy of the original item.

    The copy is what makes ``E_UNSUPPORTED_MUTATED`` meaningful: an in-process
    stage receives this tag by reference, so an alias of ``request["input"][i]``
    would let a stage edit both the canonical transcript and its own comparison
    baseline at once, hiding the edit from the guard in ``_rebuild``.
    """
    return {
        "_jev_index": index,
        "_jev_shape": "opaque",
        "_jev_raw": copy.deepcopy(item),
    }


def normalize_item(item, index: int):
    """Map one wire ``ResponseItem`` onto a bus message.

    Every bus message carries ``_jev_index`` and ``_jev_shape`` so the outgoing
    array can be rebuilt positionally and opaque shapes can be restored verbatim.
    """
    if not isinstance(item, dict):
        return _opaque(index, item)
    shape = item.get("type")
    if shape not in SUPPORTED_SHAPES:
        return _opaque(index, item)
    if shape == "message":
        text = _text_from_parts(item.get("content"))
        if text is None:
            # A non-text message shape is not one the prose view can own.
            return _opaque(index, item)
        return {
            "_jev_index": index,
            "_jev_shape": "message",
            "role": item.get("role") or "assistant",
            "content": text,
        }
    if shape == "function_call":
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            return _opaque(index, item)
        return {
            "_jev_index": index,
            "_jev_shape": "function_call",
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "type": "function",
                    "id": call_id,
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            ],
        }
    call_id = item.get("call_id")
    if not isinstance(call_id, str):
        return _opaque(index, item)
    output = item.get("output")
    if not isinstance(output, str):
        return _opaque(index, item)
    return {
        "_jev_index": index,
        "_jev_shape": "function_call_output",
        "role": "tool",
        "tool_call_id": call_id,
        "content": output,
    }


def normalize_input(items) -> list:
    if not isinstance(items, list):
        raise BoundaryError(E_INPUT_SHAPE, "request input must be an array")
    return [normalize_item(item, index) for index, item in enumerate(items)]


def _content_parts(original, text):
    """Rebuild a Codex ``content`` array from a (possibly rewritten) string."""
    parts = original.get("content")
    kind = "input_text"
    if (
        isinstance(parts, list)
        and parts
        and isinstance(parts[0], dict)
        and isinstance(parts[0].get("type"), str)
    ):
        kind = parts[0]["type"]
    elif (original.get("role") or "") == "assistant":
        kind = "output_text"
    return [{"type": kind, "text": text}]


def _rebuild(messages, original_items):
    """Turn the post-chain bus array back into a wire ``input`` array.

    The array may only shrink, by whole items, and never grow or reorder: a
    removed index is restored from the original item here, and whether it may
    actually leave the request is decided later by the approved-view filter. Any
    mutation of an opaque or unsupported shape is refused; those shapes are
    always restored from the original item verbatim.
    """
    if not isinstance(messages, list):
        raise BoundaryError(E_CHAIN_SHAPE, "stage returned no message list")
    if len(messages) > len(original_items):
        raise BoundaryError(E_CHAIN_SHAPE, "stage added input items")
    present = {}
    last = -1
    for message in messages:
        if not isinstance(message, dict):
            raise BoundaryError(E_CHAIN_SHAPE, "stage returned a non-object message")
        index = message.get("_jev_index")
        shape = message.get("_jev_shape")
        if not isinstance(index, int) or not isinstance(shape, str):
            raise BoundaryError(
                E_CHAIN_SHAPE, "stage returned a message without a boundary tag"
            )
        if index <= last or index >= len(original_items):
            raise BoundaryError(
                E_CHAIN_SHAPE, "stage reordered, duplicated, or removed an input item"
            )
        last = index
        present[index] = message
    out = []
    for index in range(len(original_items)):
        message = present.get(index)
        original = original_items[index]
        if message is None:
            # A stage dropped this item. Restore it here; the approved-view
            # filter decides whether it may leave the outgoing request.
            out.append(original)
            continue
        shape = message.get("_jev_shape")
        if shape == "opaque":
            # Always the original bytes; a stage's edit here is refused below.
            if message.get("_jev_raw") != original:
                raise BoundaryError(
                    E_UNSUPPORTED_MUTATED, "unsupported shape was modified"
                )
            out.append(original)
            continue
        if not isinstance(original, dict) or original.get("type") != shape:
            raise BoundaryError(
                E_CHAIN_SHAPE, "boundary tag does not match the original item"
            )
        if shape == "message":
            text = message.get("content")
            if not isinstance(text, str):
                raise BoundaryError(E_CHAIN_SHAPE, "message content is not text")
            if text == _text_from_parts(original.get("content")):
                out.append(original)
                continue
            rebuilt = copy.deepcopy(original)
            rebuilt["content"] = _content_parts(original, text)
            out.append(rebuilt)
        elif shape == "function_call":
            out.append(original)
        else:  # function_call_output
            text = message.get("content")
            if not isinstance(text, str):
                raise BoundaryError(E_CHAIN_SHAPE, "tool output content is not text")
            if text == original.get("output"):
                out.append(original)
                continue
            rebuilt = copy.deepcopy(original)
            rebuilt["output"] = text
            out.append(rebuilt)
    return out


# ---------------------------------------------------------------- switches


def switch_state(env=None) -> dict:
    """Resolve one JEV switch per manifest feature from the ``JEV_SWITCH_*`` env."""
    env = os.environ if env is None else env
    manifest = load_manifest()
    state = {}
    for feature, spec in manifest["features"].items():
        key = "JEV_SWITCH_" + feature.upper().replace(".", "_")
        raw = env.get(key)
        state[feature] = spec["default"] if raw is None else raw == "1"
    return state


# ---------------------------------------------------------------- registry


def build_registry(state: dict, transports: dict, manifest=None) -> dict:
    """Build the single-owner registry from the manifest's declared stages.

    Only enabled projection stages are registered, so a disabled switch means
    the stage is never invoked rather than invoked and ignored.
    """
    manifest = manifest or load_manifest()
    if state.get("projection.fabric_views") and not state.get(
        "projection.dedup_receipts"
    ):
        raise BoundaryError(E_SWITCH_ORDER, "fabric_views requires dedup_receipts")
    declared = {stage["id"]: stage for stage in manifest["events"]["stages"]}
    stages = []
    for stage_id, spec in STAGE_SPECS.items():
        stage = declared.get(stage_id)
        if stage is None:
            raise BoundaryError(
                E_STAGE_MISMATCH, f"manifest does not declare stage {stage_id}"
            )
        if stage.get("owner") != spec["package"]:
            raise BoundaryError(
                E_STAGE_MISMATCH,
                f"stage {stage_id} owner {stage.get('owner')!r} != {spec['package']!r}",
            )
        if not state.get(spec["switch"]):
            continue
        argv = transports.get(stage_id)
        if not argv:
            raise BoundaryError(
                E_STAGE_MISMATCH, f"no transport configured for {stage_id}"
            )
        stages.append(
            {
                "name": spec["name"],
                "package": spec["package"],
                "priority": int(stage["order"]),
                "claims": list(spec["claims"]),
                "hosts": [HOST],
                "transport": {
                    "argv": list(argv),
                    "timeout_ms": int(transports.get("timeout_ms", 6000)),
                },
            }
        )
    return {"schema": jev_bus.SCHEMA, "hosts": {}, "stages": stages}


# ---------------------------------------------------------------- boundary


def _receipt(
    stage_order: int,
    package: str,
    session: str,
    turn: str,
    workspace: str,
    source_id: str,
) -> dict:
    body = {
        "stage": stage_order,
        "package": package,
        "session": session,
        "turn": turn,
        "source": source_id,
    }
    return {
        "event_id": _digest(["projection_receipt", body]),
        "parent_event_id": None,
        "session_id": session,
        "turn_id": turn,
        "tool_call_id": None,
        "component": package,
        "stage": stage_order,
        "kind": RECEIPT_KIND,
        "origin_workspace": workspace,
        "capture_id": source_id,
        "occurred_at_ms": _now_ms(),
        "redaction": None,
    }


def project(
    request,
    *,
    registry: dict,
    session: str = "",
    turn: str = "",
    workspace: str = "",
    op: str = "transform",
    chain_timeout_ms: int = jev_bus.DEFAULT_CHAIN_TIMEOUT_MS,
    invoke=None,
    view=None,
    cancelled: bool = False,
) -> tuple[dict, dict]:
    """Apply the single bus owner at the boundary and return the outgoing request.

    The canonical ``request`` is never mutated. The returned report records the
    chain notes, the stage invocation order, and one receipt per applied stage.
    A stage is applied unless it both reported a bare ``passthrough`` and handed
    back the array it was given, so a stage that projects earns its receipt even
    when it also reports unrelated notes, while a stage that cannot project still
    ran: it stays in ``invoked`` and only loses its receipt. A stage whose every
    change contract ``C3`` reverts also loses its receipt, because a receipt
    records a projection that reached the wire.

    When an approved prose ``view`` is supplied it is applied to the post-dedup
    array, so eligible assistant prose leaves the outgoing request; the view is
    bound to that exact snapshot and refused (nothing removed) when the snapshot
    changed or the turn was cancelled.
    """
    if not isinstance(request, dict):
        raise BoundaryError(E_INPUT_SHAPE, "request must be an object")
    items = request.get("input")
    if not isinstance(items, list):
        raise BoundaryError(E_INPUT_SHAPE, "request has no input array")
    if len(_canonical(items).encode("utf-8")) > WIRE_LIMIT:
        raise BoundaryError(E_WIRE_BOUND, "request input exceeds the wire bound")

    messages = normalize_input(items)
    report = {
        "host": HOST,
        "adapter": ADAPTER,
        "op": op,
        "invoked": [],
        "applied": [],
        "receipts": [],
        "notes": [],
    }
    if not registry["stages"]:
        report["notes"].append(
            {
                "stage": "jev-bus",
                "action": "disabled",
                "detail": "no enabled projection stage",
            }
        )
        return copy.deepcopy(request), report

    invoke = invoke or _subprocess_invoke
    changed: dict[str, set] = {}
    removed: dict[str, set] = {}

    def observe(stage, stage_request, stage_workspace, budget_ms):
        """Invoke one stage and record what its own output changed and removed.

        Which stages project is decided by that stage's own output, never by its
        note vocabulary: the ``action`` strings are another package's wording and
        the bus contract does not fix them, so a stage that substitutes bodies and
        also reports an unrelated ``passthrough`` note must still earn a receipt.

        Only an array the bus itself accepts counts as that stage's output.
        ``run_chain`` declines any response without ``ok: true`` -- and, for a
        ``transform``, without a message list -- keeps that stage's own input, and
        records the decline as a passthrough. Reading the rejected array here would
        credit a stage for an edit the bus threw away, and hand it a receipt for a
        projection that never reached the wire.
        Both changes and removals are located by boundary tag, from the array
        itself, so a removal is attributed to the stage that made it.
        """
        before = _tagged(stage_request.get("messages"))
        response = invoke(stage, stage_request, stage_workspace, budget_ms)
        # Only an array the bus accepts is this stage's output; anything else is
        # declined, and `run_chain` keeps the stage's own input and records the
        # decline as a passthrough.
        accepted = (
            op == "transform"
            and isinstance(response, dict)
            and response.get("ok") is True
            and isinstance(response.get("messages"), list)
        )
        after = _tagged(response["messages"]) if accepted else before
        name = stage["name"]
        removed[name] = set(before) - set(after)
        changed[name] = {
            index for index in set(before) & set(after) if before[index] != after[index]
        } | removed[name]
        if accepted and len(after) != len(response["messages"]):
            # The output cannot be compared tag-by-tag, so the stage is treated as
            # having projected: a receipt is never refused on unreadable evidence.
            changed[name].add(-1)
        return response

    produced, notes, _appends = jev_bus.run_chain(
        HOST,
        messages,
        session=session,
        workspace=workspace,
        op=op,
        accepts_system_append=False,
        registry=registry,
        chain_timeout_ms=chain_timeout_ms,
        invoke=observe,
    )
    report["notes"] = list(notes)
    refused = any(note.get("action") == "chain-refused" for note in notes)
    if not refused:
        skipped = {
            note.get("stage") for note in notes if note.get("action") == "skipped"
        }
        # A bare passthrough only downgrades a stage that changed nothing.
        silent = {
            note.get("stage") for note in notes if note.get("action") == "passthrough"
        }
        report["invoked"] = [
            stage["name"]
            for stage in registry["stages"]
            if stage["name"] not in skipped
        ]
        report["applied"] = [
            name
            for name in report["invoked"]
            if changed.get(name) or name not in silent
        ]

    outgoing = copy.deepcopy(request)
    if op == "transform":
        try:
            rebuilt = _rebuild(produced, items)
        except BoundaryError as exc:
            # Fail closed: keep the original stage input and record why.
            report["notes"].append(
                {"stage": "jev-bus", "action": "refused", "detail": f"{exc.code}"}
            )
            return outgoing, report
        if DEDUP_STAGE in report["applied"]:
            # Contract C3: keep only replacements a receipt proves; revert the rest.
            try:
                rebuilt, receipt_report = dedup_receipts.enforce(
                    items, rebuilt, session=session
                )
            except dedup_receipts.ReceiptError as exc:
                report["notes"].append(
                    {"stage": DEDUP_STAGE, "action": "refused", "detail": str(exc)}
                )
                return outgoing, report
            report["dedup"] = receipt_report
            for item in receipt_report["reverted"]:
                report["notes"].append(
                    {
                        "stage": DEDUP_STAGE,
                        "action": "reverted",
                        "detail": item["reason"],
                    }
                )
            reverted = {item["index"] for item in receipt_report["reverted"]}
            if changed.get(DEDUP_STAGE) and changed[DEDUP_STAGE] <= reverted:
                # Every change the stage made was reverted, so nothing it
                # projected reached the wire: a receipt records a projection
                # that landed, and the reverts above already record the refusal.
                report["applied"] = [
                    name for name in report["applied"] if name != DEDUP_STAGE
                ]
        if VIEW_STAGE in report["applied"] and view is not None:
            # Contract C4: apply an approved prose view bound to the post-dedup
            # snapshot, or refuse (remove nothing) when it is stale/cancelled.
            try:
                before = rebuilt
                rebuilt, view_report = fabric_views.filter(
                    before, view, cancelled=cancelled
                )
            except fabric_views.ViewError as exc:
                report["notes"].append(
                    {
                        "stage": VIEW_STAGE,
                        "action": "refused",
                        "detail": str(exc),
                    }
                )
                return outgoing, report
            report["view"] = view_report
            report["view_metrics"] = fabric_views.metrics(before, rebuilt)
        # `_rebuild` restores every item a stage dropped, and the approved-view
        # filter is the only thing that may keep one out. So a removal is approved
        # exactly when the filter kept that index out, and every other removal is
        # reported against the stage the array shows made it: any stage can drop an
        # item, so naming the view stage by construction misreported the owner, and
        # gating on `len(produced) < len(items)` left the same removal silent
        # whenever a view *was* supplied.
        kept_out = {
            item["index"] for item in (report.get("view") or {}).get("removed", [])
        }
        for name, indexes in removed.items():
            if indexes - kept_out:
                report["notes"].append(
                    {
                        "stage": name,
                        "action": "reverted",
                        "detail": "unapproved_removal",
                    }
                )
        # A receipt records a projection that reached the wire, so a stage whose
        # removals were all restored reached the wire with nothing.
        for name in list(report["applied"]):
            stage_removed = removed.get(name) or set()
            if (
                stage_removed
                and not stage_removed & kept_out
                and not changed.get(name, set()) - stage_removed
            ):
                report["applied"] = [
                    applied for applied in report["applied"] if applied != name
                ]
        outgoing["input"] = rebuilt
        by_name = {stage["name"]: stage["priority"] for stage in registry["stages"]}
        for name in report["applied"]:
            source_id = next(
                (
                    n.get("id")
                    for n in notes
                    if n.get("stage") == name and isinstance(n.get("id"), str)
                ),
                "",
            )
            report["receipts"].append(
                _receipt(
                    by_name.get(name, 0),
                    next(s["package"] for s in registry["stages"] if s["name"] == name),
                    session,
                    turn,
                    workspace,
                    source_id,
                )
            )
    return outgoing, report


def _subprocess_invoke(stage, request, workspace, budget_ms):
    """Run one stage over the bus wire. Mirrors ``jev_bus._invoke`` for the host."""
    return jev_bus._invoke(stage, request, workspace, budget_ms)  # noqa: SLF001 - same contract


# ------------------------------------------------------------------- CLI


def _parse_transport(values) -> dict:
    transports = {}
    for raw in values or []:
        name, _, command = raw.partition("=")
        if not command:
            raise BoundaryError(
                E_STAGE_MISMATCH, f"--stage expects name=command, got {raw!r}"
            )
        transports[name] = command.split()
    return transports


def _read_request(path):
    raw = (
        Path(path).read_text(encoding="utf-8")
        if path and path != "-"
        else sys.stdin.read()
    )
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "plan"):
        node = sub.add_parser(name, help=f"{name} the boundary")
        node.add_argument(
            "--request", default="-", help="wire request JSON file, or - for stdin"
        )
        node.add_argument("--session", default="")
        node.add_argument("--turn", default="")
        node.add_argument("--workspace", default="")
        node.add_argument("--stage", action="append", metavar="NAME=COMMAND")
        node.add_argument(
            "--chain-timeout-ms", type=int, default=jev_bus.DEFAULT_CHAIN_TIMEOUT_MS
        )
        node.add_argument("--view", default="", help="approved view JSON file")
        node.add_argument("--cancelled", action="store_true")
        node.add_argument("--json", action="store_true")
    sw = sub.add_parser("switch", help="report the resolved JEV switch state")
    sw.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        state = switch_state()
        if args.command == "switch":
            manifest = load_manifest()
            chain = [
                {
                    "id": sid,
                    "package": spec["package"],
                    "order": manifest_stage(manifest, sid),
                    "enabled": state.get(spec["switch"], False),
                }
                for sid, spec in STAGE_SPECS.items()
            ]
            print(
                json.dumps(
                    {"switches": state, "chain": chain}, indent=2 if args.json else None
                )
            )
            return 0
        request = _read_request(args.request)
        registry = build_registry(state, _parse_transport(args.stage))
        view = _read_request(args.view) if args.view else None
        outgoing, report = project(
            request,
            registry=registry,
            session=args.session,
            turn=args.turn,
            workspace=args.workspace,
            op="plan" if args.command == "plan" else "transform",
            chain_timeout_ms=args.chain_timeout_ms,
            view=view,
            cancelled=args.cancelled,
        )
        if args.json:
            print(json.dumps({"request": outgoing, "report": report}, indent=2))
        else:
            print(json.dumps(outgoing, indent=2))
        return 1 if any(n.get("action") == "refused" for n in report["notes"]) else 0
    except BoundaryError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def manifest_stage(manifest, stage_id) -> int:
    return next(
        int(s["order"]) for s in manifest["events"]["stages"] if s["id"] == stage_id
    )


if __name__ == "__main__":
    sys.exit(main())
