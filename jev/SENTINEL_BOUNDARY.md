# Sentinel boundary wiring and effective coverage

This document records the host half of contract `C5` in
[`CONTRACTS.md`](CONTRACTS.md): Sentinel observes the prompt, pre-tool, and
post-tool boundaries, starts in local shadow mode, and reports exactly which
tools and event paths are covered; installation alone is never reported as
activation. It backs issue
[#18](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/18).

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

`parent_event_id` is `null` this phase; chaining an incident to its cause is
`#19`/`#20`.

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
| `component-stub` | `.github/scripts/test_jev_sentinel.py` | 31 tests, ok, against a hermetic `launch.py` stub that speaks the documented wire only (`hook`/`check`/`outbox`). |
| `real-component` | `jev/tests/test_sentinel_boundary.py` | 8 tests, ok against the pinned checkout (skipped when no checkout resolves): goldens match live, shadow records and never vetoes, enforce vetoes each stage, oversize refuses before the component runs, probe finds a correlated audit row per path, installation alone is not activation. |

Required CI (`unittest discover -s .github/scripts -p 'test_jev_*.py'`) is 185
tests, ok; `jev/tests` is 93 tests, ok. The vendored adapter is equal to the
pinned component on every golden case; it is a faithful translation rather than a
byte-identical copy, because the host module layout differs.

## Known limits

- **The host carrier is not the wired command.** The live path is the
  component's installed hook (`launch.py hook`), which evaluates and writes the
  component's audit store; the host carrier (`sentinel_boundary.py`) is the
  boundary the host owns, driven by `coverage`/`canary`/`observe`. Its `observe`
  output is a host record, not a native hook response, so making the host own the
  envelope on the live hook path is not part of this change. What is proved about
  the live path here is that the installed wiring evaluates (the activation
  probe) and exactly what it covers.
- **No host-side hook installer.** The component's installer writes the
  `hooks.json` entries; the host reads and reports that wiring, it does not
  create it. A profile that predicates Sentinel on the host's own wiring would
  need the corresponding installer first.
- **`parent_event_id` is unset.** Chaining an incident to its cause and latching
  a veto for the subsequent action are `#19`/`#20`.
