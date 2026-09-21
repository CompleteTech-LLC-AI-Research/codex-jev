#!/usr/bin/env python3
"""Duplicate-read proof receipts: the host-side enforcement of contract ``C3``.

The dedup stage (priority 100, owned by ``jev-prune-kit``) may replace an older
read-result body in the outgoing request with a retained-evidence marker. This
module does **not** decide which reads are duplicates - that is the component's
policy. It proves that every replacement the stage proposes is supported by a
receipt: the source and witness must share exact request arguments and body, each
must resolve to a unique native identity, and neither may sit in the protected
current user turn or recent tail. Anything else keeps the original content.

The proof is derived from the same wire items the boundary already holds, so the
host never trusts a marker's claim on its own word. Enforcement is idempotent and
never removes unique evidence: only an eligible body with an identical retained
copy is ever replaced.

Subcommands:

``candidates``  print the eligible (source, witness) pairs and their receipts;
``validate``    validate a proposed outgoing request against the original.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# The component's declared policy and model pin (jev-prune-kit, an ingest rule).
POLICY = "exact-read-repeat-v1"
MODEL = "jev-1.13.0"
READ_TOOLS = frozenset({"read", "read_file", "file_read"})
RECENT = 16
MAX_RECEIPTS = 128
FORMAT = "openai"

MARKER = "[Jev prune: repeated read-result body omitted; retained witness: {witness}]"
MARKER_RE = re.compile(
    r"^\[Jev prune: repeated read-result body omitted; retained witness: (?P<witness>[^\]\s]+)\]$"
)

# Stable refusal reasons.
R_NOT_TOOL = "not_a_result"
R_NOT_MARKER = "unproven_replacement"
R_MISSING_WITNESS = "missing_witness"
R_AMBIGUOUS = "concurrent_result"
R_CHANGED_BODY = "changed_body"
R_CHANGED_ARGS = "changed_arguments"
R_NOT_FORWARD = "witness_not_retained"
R_PROTECTED_TURN = "protected_turn"
R_PROTECTED_TAIL = "protected_tail"
R_NON_READ = "non_read_tool"
R_OUTPUT_SHAPE = "output_shape_changed"


class ReceiptError(ValueError):
    """A bounded, non-sensitive refusal; never carries context text."""


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


def _shape(item):
    return item.get("type") if isinstance(item, dict) else None


def _body(item):
    value = item.get("output")
    return value if isinstance(value, str) else None


def _calls(items) -> dict:
    """call_id -> the single ``function_call`` identity, or a sentinel for none/many."""
    found = {}
    for index, item in enumerate(items):
        if _shape(item) != "function_call":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        found.setdefault(call_id, []).append(
            {
                "index": index,
                "name": item.get("name"),
                "arguments": item.get("arguments"),
            }
        )
    return found


def _results(items) -> dict:
    found = {}
    for index, item in enumerate(items):
        if _shape(item) != "function_call_output":
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str):
            found.setdefault(call_id, []).append(index)
    return found


def turn_start(items) -> int:
    """Index of the current (last) user message, or -1 when there is none."""
    turn_start = -1
    for index, item in enumerate(items):
        if _shape(item) == "message" and item.get("role") == "user":
            turn_start = index
    return turn_start


def protected_indices(items, keep_recent: int = RECENT) -> set:
    """The current user turn plus the recent tail; nothing here may be projected."""
    protected = set(range(max(len(items) - keep_recent, 0), len(items)))
    start = turn_start(items)
    if start >= 0:
        protected.update(range(start, len(items)))
    return protected


def _proof(call_id: str, call: dict, body: str) -> dict:
    """Bind a native call identity to its request and body hashes."""
    return {
        "id": call_id,
        "hash": _digest({"name": call["name"], "body": body}),
        "request": _digest({"name": call["name"], "arguments": call["arguments"]}),
    }


def _eligible(items, calls, results, protected, start, source_id, witness_id):
    """Return a receipt if ``source_id`` may be replaced by ``witness_id``, else a reason."""
    source_calls, witness_calls = calls.get(source_id, []), calls.get(witness_id, [])
    source_results, witness_results = (
        results.get(source_id, []),
        results.get(witness_id, []),
    )
    if (
        len(source_calls) != 1
        or len(witness_calls) != 1
        or len(source_results) != 1
        or len(witness_results) != 1
    ):
        return None, R_AMBIGUOUS
    source_call, witness_call = source_calls[0], witness_calls[0]
    source_index, witness_index = source_results[0], witness_results[0]
    if source_call["name"] not in READ_TOOLS or witness_call["name"] not in READ_TOOLS:
        return None, R_NON_READ
    source_body, witness_body = _body(items[source_index]), _body(items[witness_index])
    if source_body is None or witness_body is None:
        return None, R_NOT_TOOL
    if source_call["arguments"] != witness_call["arguments"]:
        return None, R_CHANGED_ARGS
    if source_body != witness_body:
        return None, R_CHANGED_BODY
    if witness_index <= source_index:
        return None, R_NOT_FORWARD
    if source_index in protected:
        return (
            None,
            R_PROTECTED_TURN if 0 <= start <= source_index else R_PROTECTED_TAIL,
        )
    if witness_index in protected:
        return None, R_PROTECTED_TAIL
    return {
        "policy": POLICY,
        "model": MODEL,
        "session": None,
        "format": FORMAT,
        "source": _proof(source_id, source_call, source_body),
        "witness": _proof(witness_id, witness_call, witness_body),
    }, None


def candidates(
    items, keep_recent: int = RECENT, session: str = "", limit: int = MAX_RECEIPTS
) -> list:
    """Eligible (source -> later retained witness) receipts, bounded and ordered."""
    if not isinstance(items, list):
        raise ReceiptError("items must be an array")
    calls, results = _calls(items), _results(items)
    protected = protected_indices(items, keep_recent)
    start = turn_start(items)
    groups = {}
    for call_id, call_list in calls.items():
        if len(call_list) != 1 or len(results.get(call_id, [])) != 1:
            continue
        call = call_list[0]
        body = _body(items[results[call_id][0]])
        if call["name"] in READ_TOOLS and body is not None:
            groups.setdefault((call["name"], call["arguments"], body), []).append(
                call_id
            )
    receipts = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda cid: results[cid][0])
        for source_id in ordered[:-1]:
            witness_id = ordered[-1]
            if source_id == witness_id:
                continue
            receipt, _reason = _eligible(
                items, calls, results, protected, start, source_id, witness_id
            )
            if receipt is not None:
                receipt = {**receipt, "session": session}
                receipts.append(receipt)
    receipts.sort(key=lambda r: r["source"]["id"])
    return receipts[:limit]


def enforce(items, outgoing, *, session: str = "", keep_recent: int = RECENT):
    """Keep only proven replacements; revert every other body to the original.

    Returns ``(items_out, report)``. The original ``items`` and ``outgoing`` are
    never mutated. Only the ``output`` string of a ``function_call_output`` may
    change; a different count, order, shape, or any other field is refused whole.
    """
    if not isinstance(outgoing, list) or len(outgoing) != len(items):
        raise ReceiptError(R_OUTPUT_SHAPE)
    calls, results = _calls(items), _results(items)
    protected = protected_indices(items, keep_recent)
    start = turn_start(items)
    accepted, reverted, receipts = [], [], []
    out = []
    for index, original in enumerate(items):
        proposed = outgoing[index]
        if _shape(original) != _shape(proposed):
            raise ReceiptError(R_OUTPUT_SHAPE)
        if _shape(original) != "function_call_output":
            # Non-result items belong to other stages; take the proposal unchanged.
            out.append(proposed)
            continue
        original_body, proposed_body = _body(original), _body(proposed)
        if proposed_body == original_body:
            out.append(original)
            continue
        if proposed_body is None:
            reverted.append({"index": index, "reason": R_NOT_TOOL})
            out.append(original)
            continue
        match = MARKER_RE.match(proposed_body)
        if match is None:
            reverted.append({"index": index, "reason": R_NOT_MARKER})
            out.append(original)
            continue
        source_id, witness_id = original.get("call_id"), match.group("witness")
        if not isinstance(source_id, str) or witness_id not in results:
            reverted.append({"index": index, "reason": R_MISSING_WITNESS})
            out.append(original)
            continue
        receipt, reason = _eligible(
            items, calls, results, protected, start, source_id, witness_id
        )
        if receipt is None:
            reverted.append({"index": index, "reason": reason})
            out.append(original)
            continue
        accepted.append(index)
        receipts.append({**receipt, "session": session})
        rebuilt = dict(original)
        rebuilt["output"] = proposed_body
        out.append(rebuilt)
    return out, {"accepted": accepted, "reverted": reverted, "receipts": receipts}


# --------------------------------------------------------------------- CLI


def _read(path):
    raw = (
        sys.stdin.read()
        if not path or path == "-"
        else Path(path).read_text(encoding="utf-8")
    )
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cand = sub.add_parser("candidates", help="list eligible receipts")
    cand.add_argument("--request", default="-")
    cand.add_argument("--session", default="")
    cand.add_argument("--keep-recent", type=int, default=RECENT)
    val = sub.add_parser("validate", help="validate a proposed outgoing request")
    val.add_argument("--request", default="-")
    val.add_argument("--outgoing", required=True)
    val.add_argument("--session", default="")
    val.add_argument("--keep-recent", type=int, default=RECENT)
    args = parser.parse_args(argv)

    try:
        if args.command == "candidates":
            items = _read(args.request)["input"]
            print(
                json.dumps(candidates(items, args.keep_recent, args.session), indent=2)
            )
            return 0
        items = _read(args.request)["input"]
        outgoing = _read(args.outgoing)["input"]
        _out, report = enforce(
            items, outgoing, session=args.session, keep_recent=args.keep_recent
        )
        print(json.dumps(report, indent=2))
        return 0
    except ReceiptError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
