# Duplicate-read proof receipts

This document records how the host enforces contract `C3` in
[`CONTRACTS.md`](CONTRACTS.md): the dedup stage of the jev-bus boundary may
replace an older read-result body with a marker only when a receipt proves the
replacement. It backs issue
[#16](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/16) and
the stage-100 half of contract `C2`.

## What the stage may do

The dedup stage (priority 100, owner `jev-prune-kit`) is invoked once at the
request boundary described in [`BUS_BOUNDARY.md`](BUS_BOUNDARY.md). It may drop
the body of a repeated read result and leave a marker that names the retained
copy:

```
[Jev prune: repeated read-result body omitted; retained witness: <call_id>]
```

The stage decides *which* reads are duplicates. The host does not take that
decision on trust: the component's policy (`exact-read-repeat-v1`, model pin
`jev-1.13.0`) is only accepted where the host can re-derive the same proof from
the wire items it already holds.

## What the host proves

`jev/scripts/dedup_receipts.py` re-derives the proof for every proposed
replacement. A source result body may be replaced by a witness marker only when
all of the following hold:

- the source and the witness are both read tools (`read`, `read_file`,
  `file_read`);
- their request arguments are **identical** and their result **bodies are
  identical** (`changed_arguments` / `changed_body` otherwise);
- each resolves to exactly **one** native `function_call` / `function_call_output`
  identity (`concurrent_result` when a call id has more than one result);
- the witness is a **later** retained copy than the source (`witness_not_retained`
  when it is not forward);
- neither sits in the **protected** current user turn or the recent tail
  (`protected_turn` / `protected_tail`).

The proof is recorded as a receipt binding both native identities to their
argument hash and body hash:

```json
{
  "policy": "exact-read-repeat-v1",
  "model": "jev-1.13.0",
  "format": "openai",
  "source":  {"id": "call_1", "hash": "<sha256>", "request": "<sha256>"},
  "witness": {"id": "call_2", "hash": "<sha256>", "request": "<sha256>"}
}
```

`source` and `witness` are only equal on `hash` and `request`, and only when the
arguments and body actually match — so a witness cannot vouch for a body it does
not carry.

## What the host refuses

`dedup_receipts.enforce(items, outgoing)` compares the stage's proposal against
the original items and **reverts every replacement the proof does not support**,
recording a stable reason:

| Reason | Meaning |
| --- | --- |
| `unproven_replacement` | the new body is not a retained-witness marker |
| `missing_witness` | the named witness call id is not in the request |
| `changed_body` | the witness body differs from the source body |
| `changed_arguments` | the witness request arguments differ |
| `concurrent_result` | a call id has more than one result, so identity is ambiguous |
| `witness_not_retained` | the witness is not a later copy than the source |
| `protected_turn` / `protected_tail` | the source is inside the current turn or recent tail |
| `non_read_tool` | the call is not one of the read tools |
| `not_a_result` / `output_shape_changed` | the item is not a tool result, or the proposal changed the count, order, or shape |

Only the `output` string of a `function_call_output` may change. A proposal that
changes the number, order, or shape of the items is refused whole
(`ReceiptError`), and the request is returned unchanged. The caller's request and
the stage's proposal are never mutated in place.

## Invariants

- **Evidence is never removed.** Only an eligible body with an identical retained
  copy is ever replaced; a unique result keeps its body.
- **Idempotent.** Enforcing an already-enforced request is a no-op, and the
  accepted set is stable.
- **Bounded and non-sensitive.** The report holds indices, reasons, and hashes;
  it never carries result text.
- **Fail closed.** When the evidence cannot be established, the original bytes
  win.

`bus_boundary.py` runs `dedup_receipts.enforce` only when the dedup stage
actually applied, records the outcome under `report["dedup"]`, and adds a
`reverted` note per refused item. A `ReceiptError` becomes a `refused` note and
the original request is returned.

## Evidence

- **`offline-fixture`** — `jev/tests/test_dedup_receipts.py` and the
  required-CI suite `.github/scripts/test_jev_receipts.py` drive the contract
  directly and through the boundary with a deterministic stage double.
- **`real-host`** — the transport run recorded for [#15](BUS_BOUNDARY.md) shows
  the dedup stage invoked at the real boundary; the enforcement path is
  `real-host` for the boundary mechanics and `offline-fixture` for the receipts.

Live-provider evidence is never produced here: remote inference stays disabled
and unbudgeted.
