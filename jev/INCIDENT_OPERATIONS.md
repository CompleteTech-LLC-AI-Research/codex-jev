# Incident operations

This document is the long form of contract `C12` in
[`CONTRACTS.md`](CONTRACTS.md): *incident operations are bounded and
metadata-only, read-only, correlated to canonical evidence, and never dispatch
anything automatically.* It backs issue
[#20](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/20) and
operates on the incidents that [`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md)
records for #18 and [`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md) latches for #19.

The host's carrier is `jev/scripts/incident_ops.py`. The journal it reads is
`codex-jev-incidents.jsonl`; each row is a `sentinel_incident` envelope carrying
`redaction: content_sha256_only`, never raw content.

## Metadata-only, and refuse rather than launder

A row is validated against the manifest's own `events.envelope_fields` plus the
fields the host adds (`kind`, `native_event`, `outcome`, `sentinel`,
`identity`). A row that fails any check is **not printed**: only its identifier
and the failing checks are, so a journal written by a different, less careful
writer cannot be laundered into a report by being read here.

| Refusal | The fact |
| --- | --- |
| `not_an_object` | the row is not an object |
| `redaction_field_missing` | `redaction` is not `content_sha256_only` |
| `unexpected_field:<key>` | the row carries a field the envelope does not declare |
| `sentinel_shape` / `identity_shape` | the nested object is not an object |
| `unexpected_sentinel_field:<key>` | the component block carries an undeclared field |
| `unexpected_identity_field:<key>` | the correlation block carries an undeclared field |
| `digest_shape:<key>` | a digest field is not a 64-character lowercase hex digest |
| `oversized_field:<path>` | a metadata string is longer than 512 bytes |
| `secret_shaped:<path>:<rules>` | a string still matches the capture layer's credential rules |

The byte bound and the credential re-check are the two that most often do the
work: a metadata field longer than half a kilobyte is not metadata, and a string
that still looks like a credential means the redaction layer did not run.

## Read-only, and no automatic dispatch

Nothing in this module opens a file for writing. `disable` prints the exact
commands an operator would run - each step carries `executes: false` - and the
plan itself reports `reachable: false` and `dispatch: {network: none, executes:
false, writes: false}`. Disabling the integration can therefore never be an
automatic side effect of asking what disabling would do.

The module never reads the component's own audit store, so inspecting incidents
cannot mutate or taint the store the component owns.

## Correlated

`correlate` joins the journal to the canonical capture store by the content
digest the host recorded, and secondarily by the correlation identity
(`session_id` / `turn_id` / `tool_call_id`). The result says which key matched -
`matched_by_content`, `matched_by_identity`, or `unmatched` - and names the
`capture_event_id`/`capture_id` of a match. A join that matches neither is
reported as unmatched; it is never inferred into a match.

## Bounded reads

Reads default to 20 rows and cap at the component's own `outbox --limit` ceiling
(100), so a scan that saturates is reported `saturated: true` rather than as
"there is nothing else". A limit that is not a positive integer inside the cap
is refused (`E_LIMIT`), not coerced.

## The policy view is confined to the isolated profile

`policy` prints the isolated profile's policy and switch state, read-only, and
refuses a policy that does not live inside the profile root
(`E_POLICY_NOT_ISOLATED`): an operator must never be shown a policy that a
launch would not actually use. The switch view resolves **every** feature the
manifest declares, not only the Sentinel pair, so the screening switches this
phase adds are visible and unsettable from the same place.

## Command line

```
list       newest-first bounded incident rows, filtered by metadata
show       one incident row by its host event_id
summary    counts, sessions, and the correlated time range
correlate  join the journal to the canonical capture store
policy     the isolated profile's policy and switch state, read-only
disable    the exact disable/rollback steps, printed and never executed
```

Exit codes: `0` ok, `1` refused or incomplete, `2` usage error.

## Evidence tier

The required-CI suite drives `incident_ops` over synthetic and fixture journals,
including the mixed journal that must return one valid row while refusing the
malformed and credential-shaped ones without printing their values. The
`real-component` tier records a real `REVIEW` incident through
`sentinel_boundary.observe` (the component's own `instruction_override` rule)
and reads it back through `list` and `summary`; it skips with a reason when no
checkout is resolvable.
