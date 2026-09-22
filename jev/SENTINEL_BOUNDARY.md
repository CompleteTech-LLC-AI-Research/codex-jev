# Sentinel boundary wiring and effective coverage

This document records the host half of contract `C5` in
[`CONTRACTS.md`](CONTRACTS.md): Sentinel observes the prompt, pre-tool, and
post-tool boundaries, starts in local shadow mode, and reports exactly which
tools and event paths are covered; installation alone is never reported as
activation. It backs issue
[#18](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/18) and,
since the host carrier became the wired command,
[#61](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/61). The
veto precedence and session latch it feeds are the same contract `C5`
([`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md), `#19`).
The phase-level claim — that wiring, veto precedence and screening compose on
one running session — is proven in `jev/tests/test_sentinel_phase.py`
([#6](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/6)).

## Where the boundary is

Codex runs lifecycle hooks from `hooks.json` in the profile home
(`<CODEX_HOME>/hooks.json`). Three events reach the evaluator:

| Event | Stage | Payload the host normalizes |
| --- | --- | --- |
| `UserPromptSubmit` | `ingress` | the submitted prompt |
| `PreToolUse` | `tool_before` | the tool name and its input |
| `PostToolUse` | `tool_after` | the tool name and its result |

`hooks.disableAllHooks=true` in the profile unwires every event at once, and the
coverage report says so rather than reporting the paths underneath it.

## Who owns what

The evaluation is the component's; the host only carries it.

- **Component (`jev-sentinel`)** owns detection rules, thresholds, the audit
  store, the policy file, and the decision (`DEFER`/`REVIEW`/`BLOCK`/
  `QUARANTINE`). It also owns writing the `hooks.json` entries (its installer
  writes `python -I <runtime>/launch.py hook --policy … --harness codex
  --event <event> --profile <profile>`, `jev_sentinel/installer.py`).
- **Host (`jev/scripts/sentinel_boundary.py`)** owns the native envelope, the
  correlation identity (`session_id`/`turn_id`/`tool_call_id`), the payload
  bound, the incident envelope, and the coverage/activation claim.
- **Host carrier (`sentinel_boundary.py hook`)** is the wired command on the
  live path (`#61`): Codex runs it as the hook and its stdout *is* the native
  response. `install-hooks` merges it into `hooks.json`; the verdict still comes
  only from the component through `sentinel_veto.handle` →
  `sentinel_boundary.observe` → `launch.py check`.

Because the host must bound and correlate a payload *before* it hands it over,
and must know which response keys are supported, it vendors the *serialization*
contract in `jev/scripts/jev_sentinel_adapter.py`. That module is a faithful
copy of the pinned component's `jev_sentinel/hooks.py` (`EVENTS`, `normalize`,
`render`) and `jev_sentinel/core.py` (`DECISIONS`, `STAGES`, `SOURCES`,
`strict_json`, `MAX_INPUT`). Detection is never reimplemented here: every verdict
comes from running the component's own `launch.py check`.

## The two switches

The manifest declares two Sentinel switches, both default **off**:

| Switch | Default | Meaning |
| --- | --- | --- |
| `sentinel.shadow` | `false` | the host calls the boundary and records findings; a finding never vetoes |
| `sentinel.enforcement` | `false` | a finding becomes a host veto where the platform can express one; **requires** `sentinel.shadow` (`E_SWITCH_ORDER`) |

They reach the runtime as `JEV_SWITCH_SENTINEL_SHADOW` /
`JEV_SWITCH_SENTINEL_ENFORCEMENT`, the only mechanism phases 2-6 may read (`C10`).
Enforcement is gated by the **switch**, never inferred from the policy's `mode`:
a policy that says `mode=enforce` under an off switch still returns `{}` for the
host, and a shadow policy still reports `decision: BLOCK` in its record while
vetoing nothing. With both switches off the host does not call the boundary at
all, and the coverage report names `integration_switch_off`.

## Bounded normalized payloads

The host refuses to forward a payload it cannot bound, and records a fail-closed
`REVIEW` incident instead of truncating. There are two bounds:

- total normalized size above the component's own input limit,
  `adapter.MAX_INPUT` (`131072` B); or
- content above the policy's `max_content_bytes` — the same threshold where the
  component's own `content_limit` rule returns `REVIEW`.

A truncated prompt would be assessed as if it were complete, so the host never
shortens: it refuses (`E_PAYLOAD_BOUND`) and forwards nothing. The refusal lands
as a `sentinel_incident` with `backend="host_boundary"`,
`outcome="boundary_refusal"`, `reason_codes=["host_payload_bound"]`, and no
content is sent to the component.

## Shadow, and the response the host honors

`sentinel.shadow` is observational by construction: a shadow finding is recorded
and the host response is `{}`. When enforcement is on, the response is the
component's `render` output, which the host emits only through the vendored
translation:

| Stage | Enforced response keys |
| --- | --- |
| `ingress` | `decision`, `reason` |
| `tool_before` | `hookSpecificOutput` |
| `tool_after` | `decision`, `hookSpecificOutput`, `reason` |

`render_probe` proves the shape and `assert_no_replacement` runs after every
render. No Codex response path carries an output replacement: the pinned
`REPLACEMENT_FIELDS` (`updatedToolOutput`, `updatedMCPToolOutput`) are Claude-only,
so the post-tool path is feedback plus the component's subsequent-action latch,
never replacement. A regression that starts emitting one is a boundary error.

## The wired command is the host carrier

`install-hooks` merges the host carrier into the profile's `hooks.json`:

```
<python> -I <…>/sentinel_boundary.py hook --harness codex --event <event> \
  --profile <profile> --policy <policy.json> --state-dir <state> --component <checkout>
```

Codex runs that command as the hook, so **its stdout is the native hook
response**. `hook` reads one payload under the component's own stdin bound,
resolves the host identity (the payload's `session_id`/`turn_id`/`tool_call_id`
first, then the flags), and calls `sentinel_veto.handle`, which observes through
`sentinel_boundary.observe` and runs the component's own `launch.py check`. The
host renders nothing the component did not decide.

Three properties are load-bearing:

- **Merge, never replace.** Only entries whose command names this carrier are
  replaced or removed; every unrelated entry (the Fabric capture hooks) and every
  unrelated key in `hooks.json` is preserved.
- **Gated on the switches.** With `sentinel.shadow` and `sentinel.enforcement`
  both off, `install-hooks` writes nothing and refuses with `E_SWITCH_OFF`, so a
  dark integration never puts a command in the host's path. `--remove` always
  works, because unwiring must not depend on a switch.
- **The `--state-dir` contract.** The carrier writes its host journal
  (`codex-jev-incidents.jsonl`) under the `--state-dir` pinned into the command,
  and `install-hooks` appends one record to
  `<state_dir>/codex-jev-sentinel-hooks.json`. The probe reads the correlated
  incident from that exact directory, so a carrier wired without it - or with a
  different one - is not credited as active. `--dry-run` reports the record and
  writes nothing.

**The carrier fails open at the process level.** A hook that exits non-zero shows
the operator a hard error, so the carrier prints a JSON response and exits `0` on
every path; when the boundary cannot decide it prints `{}` (no judge). The
fail-closed decision, where one is owed, is already carried by the response the
gate renders and by the durable session latch - never by a process exit code the
host does not read.

## Effective coverage

`coverage` reads the profile's real `hooks.json` and reports, per stage:

| Field | Meaning |
| --- | --- |
| `wired` / `entries` / `commands` | whether a command hook exists for the native event |
| `matchers` | the Codex matcher regex(es) |
| `covered_tools` | the tools the matchers cover; `*` means every tool; a plain alternation of literal names is enumerated exactly, and any regex syntax is reported as `<opaque:…>` rather than guessed |
| `reachable` / `reachable_launchers` | whether the named `launch.py` is a file on disk |

## Activation is not installation

`coverage.activation.activated` is `false` without `--probe`, whatever the files
say. A present `hooks.json`, a resolvable launcher, and a matching component
revision are **necessary, never sufficient**: Codex requires per-hook trust
approval that no file on disk records.

`--probe` runs the *wired command itself* and requires an audit row whose
`content_sha256` **and** `session_ref` match a per-run-unique canary session
(`canary-probe-<nonce>`); only then is `activated: true`,
`basis: "probe_canary"`. A launcher that merely echoes `{}` can never satisfy it,
because a shadow response is `{}` and the audit row is the only difference — and
the unique session means a stale row from an earlier run cannot either. A
saturated scan (100 rows, none matching) reports `inconclusive`, not "inactive".

When the wired command is the host carrier, an audit row alone is not enough: the
path must **also** show the host's own correlated incident (same `session_ref` and
`content_sha256`) in the carrier's `--state-dir`. A carrier that reached the
component without the host boundary between them is not activation — which is
exactly what wiring the carrier is for.

## Incidents

The host records one correlated `sentinel_incident` envelope per observed event
in `<state_dir>/codex-jev-incidents.jsonl`:

- every field in the manifest's `events.envelope_fields` is present;
- the correlation keys (`session_id`, `turn_id`, `tool_call_id`) tie the finding
  to the exact host activity;
- `sentinel{}` carries the component's verdict identity (`event_id`),
  `session_ref`, and the content/action digests, so a host incident joins to the
  component's own audit row;
- `redaction: "content_sha256_only"` — the raw prompt/result text is **never**
  written to an incident.

`parent_event_id` is `null` on the first event of a session and carries the
latched event's id once the session is latched, so a veto's cause is knowable; the
latch and its precedence are the same contract `C5`
([`VETO_PRECEDENCE.md`](VETO_PRECEDENCE.md), `#19`).

## Bypass surfaces

`coverage.bypass_surfaces` names every observed way a finding is skipped or an
action is left ungated, rather than assuming them away:

| Surface id | Kind | When it is named |
| --- | --- | --- |
| `native_disable_all_hooks` | bypass | `hooks.disableAllHooks=true` |
| `hook_not_wired.<stage>` | bypass | the native event has no command hook |
| `launcher_unreachable.<stage>` | bypass | the wired launcher is not a file |
| `tools_outside_matcher.<stage>` | bypass | the matcher covers some tools only |
| `matcher_opaque.<stage>` | bypass | the matcher uses regex syntax and cannot be enumerated |
| `post_tool_replacement_unsupported` | unsupported_replacement | always (Codex has no replacement field) |
| `ingress_scope_is_prompt_only` | bypass | always (model/tool text arrives at `tool_after`) |
| `local_rules_only` | bypass | `policy.backend=local` |
| `host_trust_unverified` | bypass | always (only a canary can show activation) |
| `component_revision_mismatch` | bypass | the checkout is not the manifest pin |
| `integration_switch_off` | bypass | `sentinel.shadow` is off |

## Disable behavior

With `sentinel.shadow` off (the `baseline` profile and every isolated profile:
`init` materializes an environment from a profile that enables **no** optional
switch, `C10`), the host does not call the boundary, `canary` returns
`skipped: switch_off`, no incident is written, and the coverage report names
`integration_switch_off`. With `hooks.disableAllHooks=true`, `wired_stages` is
empty and `native_disable_all_hooks` is named. Either way the pinned base
reproduces the original behavior: nothing is evaluated and nothing vetoes.

## Evidence

| Tier | Artifact | Result |
| --- | --- | --- |
| `component-fixture` | `jev/tests/sentinel_fixtures/codex-translation.json` | 26 goldens captured from `jev-sentinel @ 4ecd748d` (9 `normalize`, 5 refusal, 12 `render`), with a provenance block. |
| `component-stub` | `.github/scripts/test_jev_sentinel.py` | 41 tests, ok, against a hermetic `launch.py` stub that speaks the documented wire only (`hook`/`check`/`outbox`): the carrier install merge/remove/dry-run and its `E_SWITCH_OFF` gate, the wired response and correlated incident, the fail-closed bound, and the process-level fail-open. |
| `real-component` | `jev/tests/test_sentinel_boundary.py` | 13 tests, ok against the pinned checkout (skipped when no checkout resolves): goldens match live, shadow records and never vetoes, enforce vetoes each stage, oversize refuses before the component runs, probe finds a correlated audit row per path, installation alone is not activation, and the wired carrier vetoes while writing both the host incident and the component audit row. |
| `real-component` | `jev/tests/test_sentinel_phase.py` | 7 tests, ok against the pinned checkout (skipped when no checkout resolves): the phase criteria composed on one session. Installation alone is not activation while the wired probe is; a running session writes three correlated incidents across `ingress`/`tool_before`/`tool_after`; enforcement denies the exact action at the tool and prompt boundaries and never emits an `allow` or a replacement field, while shadow records the same finding and returns `{}` with no latch; a latched veto gates the next action and its incident names the latched cause; and quarantined context is withheld by the supported search path, journaled and re-proved, with no excerpt bytes in the plan or the journal. |
| `real-host-binary` | [`jev/smoke/run-sentinel-hook-smoke.sh`](../smoke/run-sentinel-hook-smoke.sh) + [`jev/evidence/sentinel-hook-real-host.md`](sentinel-hook-real-host.md) | A launched host on `main` runs the wired carrier: 33/33 named checks. Shadow stays observational and records three correlated incidents; enforce prevents the exact action before execution and the host quotes the component's reason; unwired records nothing under the same switches; and the coverage report says `activated: false` while three stages are wired, `true` only after a probe that the host corroborates, and `false` again after `--remove`. |

Required CI (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) is 424
tests, ok; `jev/tests` is 183 tests, ok. The vendored adapter is equal to the
pinned component on every golden case; it is a faithful translation rather than a
byte-identical copy, because the host module layout differs.

## Known limits

- **Host trust approval is unverifiable.** Codex demands a per-hook trust
  decision that no file records; `activation` can show the command ran and
  correlated, never that the operator trusted it (`host_trust_unverified`).
- **The carrier speaks Codex only.** Its event map is the Codex adapter's
  (`--harness codex`); another harness is refused and the response is `{}`.
- **A payload over the stdin bound records no incident.** The carrier refuses it
  before it reaches the component and hashes nothing to correlate, so under
  enforcement it returns the `REVIEW` shape but writes no correlated row — the
  same choice the component's own hook makes.
- **The component's installer is still the component's.** The host ships
  `install-hooks` for its own carrier; wiring the component's `launch.py` directly
  still uses the component's installer.
