# Sentinel veto precedence and the subsequent-action latch

This document records the host half of the second sentence of contract `C5` in
[`CONTRACTS.md`](CONTRACTS.md): *a configured veto prevents the exact action
before execution, cannot be cleared by a later approval, and latches for the
subsequent action.* It backs issue
[#19](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/19) and
builds directly on the boundary that
[`SENTINEL_BOUNDARY.md`](SENTINEL_BOUNDARY.md) describes for #18.

The host's carrier is `jev/scripts/sentinel_veto.py`. It calls
`sentinel_boundary.observe` for every verdict, so detection still belongs to the
pinned `jev-sentinel` component; what this module owns is only what a *host* can
own.

## Division of labour

| Question | Owner |
| --- | --- |
| What is the finding for this event? | the pinned component (`DEFER`/`REVIEW`/`BLOCK`/`QUARANTINE`) |
| Does the component remember that a session is tainted? | the component's own audit store (`sessions.tainted`) |
| Which veto can the platform actually express for this stage? | the host (`render` translation, vendored) |
| Is a veto still in force after a later event defers? | the host (the latch in this document) |
| May two evaluations of one session interleave? | the host (the per-session lock) |
| What happens when the assessment fails? | both, and they must agree: the component reports `REVIEW`/`backend=unavailable`, the host latches `REVIEW` |

The component already latches a session by tainting it, and that taint survives a
new user turn. The host does **not** reimplement that detection. It keeps its own
latch for a different reason: the host must be able to say *"this session is
vetoed"* even for an event whose own component verdict is `DEFER`, and it must do
so without another component call. The two latches are independent stores; see
[Unavoidable races](#unavoidable-races).

## The lattice

Severity orders the decisions so the strongest finding wins:

```
DEFER (0)  <  REVIEW (1)  <  BLOCK (2)  <  QUARANTINE (3)
```

A **veto** is any enforced decision above `DEFER` — the component's own predicate
(`enforced and decision != DEFER`), not a host invention. `REVIEW` vetoes under
enforcement exactly as much as `BLOCK` does; the ranks differ only so that a
later, weaker finding can never replace a stronger one.

## Mapping onto supported host behavior

The host emits only the keys the pinned translation produces for Codex, per
stage. Nothing else is added, and no path ever carries `permissionDecision:
allow` or an output replacement (Codex has no replacement field).

| Stage (native event) | Enforced response | Effect |
| --- | --- | --- |
| `ingress` (`UserPromptSubmit`) | `decision`, `reason` | the prompt is blocked |
| `tool_before` (`PreToolUse`) | `hookSpecificOutput.hookEventName`, `.permissionDecision="deny"`, `.permissionDecisionReason` | the exact action is prevented **before execution** |
| `tool_after` (`PostToolUse`) | `decision`, `reason`, `hookSpecificOutput.additionalContext` | the result is flagged; the action already ran |

`assert_no_replacement` runs after every render, and the required-CI suite
asserts the key set per stage, so a regression that widens the response is a
failure rather than a surprise. Host permissions and sandbox enforcement are
untouched: the host adds a deny where the stage supports one and never removes or
rewrites a field the host owns.

## Precedence and the latch

For one event, the host computes

```
effective = max( this event's enforced decision , the session latch )
```

and vetoes when the effective rank is above `DEFER`. If the event's own finding
is at least as strong as the latch, the component's own response is emitted
verbatim and the veto is attributed to the event. Otherwise the veto is
attributed to the **latch**, and the reason names the latched decision and its
cause, because the component produced no message for a deferring event.

The latch itself is a fold over an append-only ledger:

| Row | Effect on the session's state |
| --- | --- |
| `escalate` | the state becomes the maximum rank seen so far |
| `clear` (operator only) | the state resets |

Three properties follow, and each has a test:

- **A veto is never downgraded.** A later approval-shaped event, a later
  `DEFER`, or a weaker finding cannot reduce the latch. The strongest decision
  keeps the session.
- **A veto is never lost.** `escalate` is idempotent — it writes a row only when
  the new rank is strictly higher — and it is read-modify-written under the
  session lock, so a concurrent raise is never dropped.
- **The state is replayable from a bounded suffix.** The fold's value is "the
  strongest escalation since the last clear", so reading the last
  `MAX_LATCH_ROWS` rows reproduces the full state exactly; truncation is
  lossless for the latch rather than merely conservative.

### The subsequent-action latch

After a veto the session stays vetoed, so the *next* action is prevented before
it executes even if its own finding defers. This is the only honest shape for the
post-tool path: a result that already executed cannot be un-executed, so the
post-tool veto is feedback plus the latch that gates what follows. Object
replacement is not available to Codex, and the host never fabricates one.

The latch is keyed by the component's own session key,
`sha256(canonical([harness, profile, session_id]))`, so a host latch row and the
component's audit row join on the same value. `parent_event_id` — left unset in
#18 — now chains each incident to the event that latched its session, so the
cause is readable from the incident stream.

### Without a session identity

With no `session_id` there is no key, so nothing latches and the veto is
single-shot. This mirrors the component, whose `assess` computes an empty key
when the session id is absent. A harness that does not surface a session id
therefore gets per-event vetoes only; the asymmetry is deliberate and tested
rather than hidden.

## Concurrent actions

Evaluations of one session are serialized by a per-session lock: a
`threading.Lock` for threads in one process and an `flock(2)` file lock in
`<state_dir>/locks/<session_ref>.lock` across processes. Different sessions do
not contend, and a session with no identity takes no lock at all.

What serialization buys:

- the latch is never read from stale state, so two parallel calls cannot both
  decide from the pre-veto ledger;
- a raise is never lost, so the strongest concurrent finding survives;
- the ledger stays strictly increasing in rank, so the state is unambiguous.

What it does not buy — and the tests assert the weaker, honest property — is that
the *earlier arriving* event wins. Arrival order is not observable to the host:
an event the host evaluates before the veto exists legitimately has no latch to
inherit. The guarantee is *no stale read and no lost raise*, not *first come,
first vetoed*.

## Failing closed

Every failure path produces the component's own `failure_result` shape —
`REVIEW`, `enforced`, `route=security_review`, `backend=unavailable` — and latches
the session under enforcement. A failure is never an allow.

| Failure | Detection | Host behavior |
| --- | --- | --- |
| component timeout | the host's bounded component call raises | `REVIEW` veto + `REVIEW` latch |
| malformed component response | strict JSON parse of stdout fails | `REVIEW` veto + `REVIEW` latch |
| malformed verdict shape | `decision` is not a decision, or identity fields are missing | `REVIEW` veto + `REVIEW` latch |
| non-zero component exit | exit status is not 0 | `REVIEW` veto + `REVIEW` latch |
| unboundable payload | the boundary refuses before forwarding | the boundary's `REVIEW` incident becomes a `REVIEW` veto |
| malformed native payload | normalization fails | `REVIEW` veto; no content is echoed |
| corrupt or missing policy | `load_policy` refuses | the policy is treated as **enforcing** (`fail_closed`), so the gate stays closed |
| cancellation | the evaluation is interrupted | `REVIEW` latches **before** the interrupt propagates, so a cancelled run never leaves the session ungated |

A failure also records a `sentinel_incident` with `outcome="fail_closed"` when the
payload can still be normalized, so the failure is visible in the incident stream
and not only in the latch ledger.

## The ledger

`<state_dir>/codex-jev-veto-latch.jsonl`, one row per append:

| Field | Meaning |
| --- | --- |
| `kind`, `schema` | `sentinel_veto_latch`, `jev-sentinel.veto-latch.v1` |
| `latch_id` | digest of the row's identity fields |
| `session_ref` | the component's session key |
| `action`, `decision`, `rank` | `escalate`/`clear`, the decision, its severity |
| `source` | `event`, `failure`, `boundary_refusal`, or `operator` |
| `event_id` | the component's verdict id for the causing event |
| `incident_event_id` | the host incident id, so the chain is followable |
| `stage`, `native_event`, `turn_id`, `tool_call_id` | the correlation keys |
| `reason_codes`, `failure_code`, `enforced` | why, and whether it was enforced |
| `redaction` | `content_sha256_only` — no context text is ever written |

Rows carry no content: the ledger says *that* a session is vetoed, never what it
was that triggered the veto.

## Operator controls

```sh
python3 jev/scripts/sentinel_veto.py enforce --event PreToolUse --profile default \
    --session <id> --turn <id> --request payload.json   # prints the host response
python3 jev/scripts/sentinel_veto.py latch --state-dir <dir> --json
python3 jev/scripts/sentinel_veto.py clear --state-dir <dir> \
    --session-ref <64 hex> --confirm
```

`enforce` prints exactly the response object on stdout, because that is what a
harness reads; `--json` prints the operator envelope (verdict, effective
decision, which side supplied the veto, and the latch transition). It exits `0`
even when it vetoes — the response *is* the contract — matching the component's
own `hook`. `clear` mirrors the component's `clear-session`: it needs an explicit
`--confirm` and a full 64-character session reference, and it appends a row
rather than rewriting the ledger.

Clearing the host latch does not clear the component's taint, and vice versa:
they are separate stores, and the component stays the authority on its own taint.
A session is only fully un-gated when both are cleared — and until the component's
own taint expires or is cleared, its findings continue to veto on their own.

## Disable behavior

Enforcement follows the declared switches, never the policy alone. With
`sentinel.shadow` off the host does not call the boundary at all, and with
`sentinel.enforcement` off a finding is recorded and never vetoes. A policy that
says `mode=enforce` under an off switch still produces `{}` for the host, and a
shadow policy under an on switch produces a `DEFER` rank. Under shadow the latch
is neither applied nor cleared: switching to shadow suspends enforcement and
preserves the record, and switching back re-applies it. With the integration off
the pinned base reproduces the original behavior — nothing is evaluated, nothing
latches, and nothing vetoes.

## Unavoidable races

These are documented rather than assumed away; each is either tested as an
observable property or named as a limit of the host.

- **The post-tool veto cannot un-execute.** A tool the harness already ran stays
  run. Only `tool_before` prevents the exact action before execution; after that
  the latch gates what follows.
- **A harness that kills the hook may fail open.** The component's own note
  applies: the underlying harness may fail open if it terminates the launcher.
  The host cannot prevent that; it makes the fail-closed path the default and
  records the failure.
- **Two independent latches.** The component's taint lives in its audit store and
  the host latch in the state directory. They agree on the key and neither
  rewrites the other. A `clear` on one does not clear the other (demonstrated in
  the real-component tier).
- **Hook trust is a precondition.** A veto is enforced only where the harness
  honors the stage's response keys and only after the hook is trusted; an
  untrusted or unwired hook never runs, so nothing vetoes and nothing latches.
  `SENTINEL_BOUNDARY.md` names that bypass surface.
- **Concurrent arrival order is not the host's to decide.** Serialization gives no
  lost raise and no stale read; it cannot make an event that the host evaluated
  before the veto inherit it.
- **The lock spans one state directory.** Two hosts with different state
  directories keep two ledgers; correlation across them is by `session_ref`.
- **No session id, no latch.** A harness that omits the session identity gets
  single-shot vetoes, exactly as the component gets no taint.

## Evidence

| Tier | Artifact | Result |
| --- | --- | --- |
| `component-stub` | `.github/scripts/test_jev_veto.py` | 39 tests, ok: the stage mapping for every configured outcome, precedence and downgrade refusal, the latch's per-session and no-session behavior, the shadow and switch rules, every fail-closed path including a cancellation, the lock's exclusivity and a concurrent raise, the bounded replay, and the command line. |
| `real-component` | `jev/tests/test_veto_precedence.py` | 8 tests, ok against the pinned checkout: the host's verdict equals the component's for the same event, the latch key equals the component's own `session_ref` and joins its audit row, a real `BLOCK` latches and denies the next read-only action, a `DEFER` after a veto is still denied, the latch survives a turn, shadow is observational, and clearing the host latch leaves the component's taint in force. |

Required CI (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) is 433
tests, ok; `jev/tests` is 183 tests, ok. All offline: the stub tier runs a
hermetic `launch.py`, and the real-component tier runs the pinned checkout with
`backend=local`. No network or paid inference is involved.

## Known limits

- **The host carrier is still not the wired command.** As recorded for #18, the
  live path is the component's installed hook; `sentinel_veto.py` is the host's
  own boundary, driven through `enforce`/`handle`. Making the host own the
  envelope on the live hook path is follow-up
  [#61](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/61), and
  #55 does the same for the bus.
- **Latch expiry is manual.** The component's taint expires after a day of
  session inactivity; the host ledger has no clock-based expiry, so a cleared
  latch is an explicit operator action or an explicit ledger edit. That is a
  deliberate fail-closed choice, not an oversight.
- **Incident operations are #20.** This change chains `parent_event_id` for the
  latch cause; retrieval screening and the broader incident operations remain
  [#20](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/20).
