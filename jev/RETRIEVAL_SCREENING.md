# Retrieval screening and memory-write authorization

This document is the long form of contract `C11` in
[`CONTRACTS.md`](CONTRACTS.md): *retrieved context is screened before it is
injected, and a memory write is authorized by the host's own rule rather than by
a component's opinion.* It backs issue
[#20](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/20) and
joins the budgeted retrieval view of
[`RETRIEVAL.md`](RETRIEVAL.md) to the Sentinel boundary of
[`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) and the veto latch of
[`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md).

The host's carrier is `jev/scripts/retrieval_screening.py`. Nothing there
classifies content.

## Division of labour

| Question | Owner |
| --- | --- |
| Is this excerpt an instruction override or a secret transfer? | the pinned component (`context` stage) |
| Is this proposed memory write an untrusted write? | the pinned component (`memory` stage) |
| Is the excerpt's origin proven, in-workspace, still redacted, unique, in budget? | the host |
| May the component be consulted at all? | the host (`screening.retrieval`) |
| Does a component finding withhold? | the host (`screening.enforcement`, and the screening policy) |
| Is a write target authorized? | the host (its own policy) |
| Is the session under an uncleared veto? | the host (the #19 latch) |

The component answers `DEFER`/`REVIEW`/`BLOCK`/`QUARANTINE`; the host never
invents one. A candidate the component deferred carries `component_defer`, and
a run with the component switched off carries `assessed: false` rather than a
`DEFER` the component never returned.

## The two switches are not the same switch

`screening.retrieval` decides whether the component is consulted. Off, no
assessment happens, every row is `assessed: false`, and no component finding
exists.

`screening.enforcement` decides whether a *component finding* withholds. It is
declared only behind `screening.retrieval`, because there is nothing to enforce
without an assessment. Off, a finding is injected anyway and recorded as the
shadow signal `would_withhold`.

Both default to **false** in the manifest, so the isolated profile starts dark:
the integration never screens, and every row is `assessed: false`.

## What withholds, and why

A host-owned fact withholds a candidate **unconditionally**, because the host
already holds the proof and no switch can make it safe to inject:

| Reason code | The fact |
| --- | --- |
| `not_canonical_evidence` | the candidate is not the canonical shape |
| `unproven_origin` | no captured event, or a capture/digest mismatch |
| `cross_workspace` | the evidence came from another workspace |
| `redaction_regression` | the excerpt still contains a credential |
| `duplicate_of_accepted` | this evidence was already injected |
| `over_budget` | past the candidate or byte budget |
| `over_bound` | larger than the component's own input bound |
| `component_unavailable` | the assessment was attempted and did not complete |
| `policy_unavailable` | the screening policy is corrupt (the policy then fails closed) |
| `session_vetoed` | the session carries an uncleared #19 veto |

A **component finding** withholds only when enforcement is on *and* the policy's
`withhold_on` names that decision. The default policy names `BLOCK` and
`QUARANTINE`; a `REVIEW` finding is therefore injected (marked untrusted) and
still recorded, and an operator can widen the set. `REVIEW` is not silently
dropped: `component_review` is recorded whenever the component returns it.

`would_withhold` is the shadow signal: it is true when a finding exists and the
candidate was injected anyway. It is a property of the *row*, so it survives a
run even when the policy narrows what it withholds on.

## The screening policy

`screening_policy(path)` resolves the host's thresholds. A **missing** file means
the documented defaults; a **present** file that cannot be read or validated
fails closed (it withholds on every ladder decision and sets `fail_closed`),
because the file that was supposed to bound the run cannot be trusted.

| Key | Default | Meaning |
| --- | --- | --- |
| `schema_version` | `1` | refused unless `1` |
| `withhold_on` | `["BLOCK", "QUARANTINE"]` | component decisions the host withholds on |
| `deduplicate` | `true` | withhold a repeat of an accepted candidate |
| `max_candidates` | the retrieval view's bound | candidates per plan |
| `max_bytes` | the retrieval view's bound | accepted bytes per plan |
| `memory_write.authorized_targets` | `[]` | the only targets a write may name |

An unstated key keeps its default, and the resolved policy is never aliased to
the defaults, so a caller that edits the resolved copy cannot move a later run's
thresholds. An unknown key is refused rather than ignored: a policy that names a
threshold the host does not implement, or misspells one it does, must not run
with the operator's intent silently absent.

## Memory-write authorization

Two rules compose, and they are not the same rule:

* the **host's** rule is absolute: the write target must be named in
  `memory_write.authorized_targets`. A target that is not named is refused
  whatever the switches say.
* the **component's** finding is advisory until `screening.enforcement` is on.
  Under enforcement, any component finding on a proposed write refuses it; in
  shadow, the write is authorized and recorded as `would_withhold`.

An uncleared session veto refuses the write regardless, and a corrupt policy
refuses it too. `memory_write` writes nothing: it returns a decision and its
reason codes, and the caller decides.

## The withheld journal is an evidence pointer

A withheld row carries identifiers and hashes, never the excerpt bytes, so the
refusal can be inspected and joined without re-exposing the content that was not
safe to inject. It is written to `codex-jev-withheld.jsonl` under the state
directory.

`verify_withheld(env_dir, state_dir)` re-proves every row against the canonical
store: the row must still resolve to a captured event whose recorded content
hash matches the row, and whose canonical bytes still hash to it. A row that
cannot be re-proved is reported as a failure with its identifier, never
approximated. Quarantine never erases evidence; it points at it.

A memory-write decision has no canonical event behind it, so it is journalled
separately, to `codex-jev-memory-decisions.jsonl`. Sharing one file would make
`verify` report a memory decision as an unresolvable withheld row.

## Command line

```
screen            search, screen, and print the injection plan (or --json)
withheld          read the withheld journal, newest rows first
verify            re-prove the withheld journal against the canonical store
memory-write      authorize one write from a --text-file
memory-decisions  read the memory-write journal
```

Each subcommand takes `--state-dir` and reads the #19 latch from it, so the CLI
sees the same session veto the API sees.

## Evidence tier

The required-CI suite (`test_jev_screening.py`) drives this layer with a
`component-stub` that implements only the component's documented `check` wire
and answers from markers - it is a transport proof, not a detection proof. The
`real-component` tier (`jev/tests/test_screening_incidents.py`) drives the pinned
`jev-sentinel` checkout, so the `instruction_override` and
`untrusted_memory_write` rules that fire there are the component's own. It skips
with a reason when no checkout is resolvable.
