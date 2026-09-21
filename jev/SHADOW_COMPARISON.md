# Shadow comparison and the approval enforcement gate

This document records how a JEV approval judgment is compared against the host's
own final decision, how the evaluation set is labelled and frozen, and what has
to hold before approval enforcement may be switched on. It backs issue
[#23](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/23),
contract `C6` in [`CONTRACTS.md`](CONTRACTS.md), and the manifest features
`approval.shadow_comparison`, `approval.enforcement_gate`, and
`approval.enforcement`.

Everything here is **off by default**. The shipped state is enforcement
disabled, and the shipped criteria are *not* met. No model was called, no
provider answered, and no live calibration has happened: the tier is
`offline-fixture`. The live evaluation that would have to happen before
enforcement could be enabled is recorded as a blocker in
[`ORCHESTRATION.md`](ORCHESTRATION.md) and is not claimed here.

## What is compared

The preflight under test and the host keep separate vocabularies on purpose; the
mapping between them is declared rather than inferred, so a vocabulary drift is
a failing test instead of a silent agreement.

| Producer | Vocabulary | Record |
| --- | --- | --- |
| `jev-codex-approval` preflight | `allow`, `deny`, `abstain` | `judgment()` |
| Host | `approved`, `denied`, `abstained` | `host_decision()` |

`correlate()` joins one judgment to one host record and returns exactly one
`kind`:

| Kind | Meaning |
| --- | --- |
| `agreement` | The judgment's decision maps onto the outcome the host reached. |
| `disagreement` | It does not, and the record names both decisions. |
| `deferral` | The case was handed to the existing reviewer, with a declared reason. |
| `failure` | The component produced no comparable decision, with a declared reason. |

A deferred or failed case **must** abstain. `judgment()` refuses a record that
carries both a reason and a comparable decision (`E_DEFERRAL_DECISION`), because
that is exactly how a deferral gets rounded into an agreement. `metrics()` counts
a deferral and a failure in their own rates, never in the numerator of
`agreement_rate`, and `label_mismatch_rate` divides by the comparable cases
only, so "did not decide" is never dressed up as "decided correctly".

Timing is recorded per case as a component span and a host span. The report
carries the unit, and it carries the sentence that says what the number is:
*wall clock inside this fixture, not a provider measurement*. Nothing in this
phase measures provider latency, model accuracy, safety, or cost.

## Correlation without raw commands or secrets

A record may carry only the fields `JUDGMENT_FIELDS` and `HOST_FIELDS` declare;
an undeclared field is refused (`E_HOST_UNSUPPORTED_FIELD`), so a producer
cannot quietly add raw command text or a token to a record that is meant to be
safe to attach to an issue. The only form of a command that may leave the
process is `hash_command()`'s `sha256` digest.

`redaction_audit()` re-checks that claim instead of asserting it. It serializes
the correlated cases **and** every attached document - the set, the criteria,
the report - and reports each piece of raw text it was handed that still
appears, with the digest of the text rather than the text itself. The shipped
report records `documents_scanned`; the required-CI tests hand the audit real
strings (a destructive command, a key header, a secret variable name) and
require `clean`, plus a negative control where a leak *is* present and the audit
must say so. An operator re-running this against their own labelled corpus can
pass the raw text with `--raw-command`, so the audit is run against the corpus
it actually came from.

## The labelled set and the frozen procedure

Two splits live in [`fixtures/shadow/`](fixtures/shadow), each labelled from the
published approval policy by a reader who did not see the component's judgment,
with the procedure recorded in the document's `labeling_procedure` field:

| Split | Cases | Content |
| --- | --- | --- |
| `calibration.json` | 8 | May be used to tune. |
| `holdout.json` | 12 | Evaluated once; never used to tune. Contains 8 agreements, 1 disagreement, 2 deferrals (2 reasons) and 1 failure, so the completeness checks are exercised rather than assumed. |

The procedure is frozen as a digest rather than described:

```
python3 jev/scripts/shadow_comparison.py freeze \
  --split calibration=jev/fixtures/shadow/calibration.json \
  --split holdout=jev/fixtures/shadow/holdout.json \
  --out jev/fixtures/shadow/frozen.json
```

`verify_freeze()` re-reads the pin and names every split whose bytes no longer
match. Drift does not raise: it leaves enforcement disabled
(`evaluation_set_frozen` in `blocked_by`), because an edited evaluation set is a
reason to stop, not a reason to error out mid-run.

The gate evaluates the criteria **on the holdout only**. It refuses a set whose
declared split is not the one being evaluated (`E_HOLDOUT_REQUIRED`) and refuses
criteria that declare a different `evaluated_on`, so the split that was tuned on
cannot be the split that justifies enforcement.

## The gate

`enforcement_state()` computes one verdict from eight inputs, each reported
separately, so "disabled" always says why:

| Check | Input |
| --- | --- |
| `gate_declared` | `JEV_SWITCH_APPROVAL_ENFORCEMENT_GATE=1`, the switch the active profile's `approval.enforcement_gate` feature materializes. |
| `switch_on` | `JEV_SWITCH_APPROVAL_ENFORCEMENT=1` |
| `guardian_only_kill_switch_off` | `JEV_SWITCH_APPROVAL_GUARDIAN_ONLY` is not `1` |
| `remote_consent_declared` | `JEV_REMOTE_INFERENCE_CONSENT=1` |
| `model_available` | `JEV_APPROVAL_MODEL_AVAILABLE=1` |
| `evaluation_set_frozen` | The evaluated split matches its frozen digest. |
| `categories_opted_in` | At least one declared action category is opted in. |
| `criteria_met` | Every declared criterion is measured **and** satisfied. |

Enforcement is enabled only when all eight hold. There is no "unknown" and no
partial state: a missing input leaves it disabled. A criterion with no
measurement is unmet (`reason: unmeasured`), an operator the phase does not
support is a refusal (`reason: unsupported_operator`), and a near miss is
reported with both the required and the observed number rather than rounded into
a pass. An action category the module does not declare is refused instead of
being counted as consent (`E_UNKNOWN_CATEGORY`).

`enforcement-eval.json` carries the evaluation switches. It enables
`approval.enforcement`, and the manifest requires
`approval.enforcement_gate` -> `approval.shadow_comparison` ->
`approval.preflight`, so a profile cannot reach enforcement without the gate.
The profile carries the *switches*; the gate is what decides whether the
enforcement switch may be set at all, and the shipped answer is no.

**Immediate return to Guardian-only.** Setting
`JEV_SWITCH_APPROVAL_GUARDIAN_ONLY=1` forces the disabled state whatever else
holds, and unsetting `JEV_SWITCH_APPROVAL_ENFORCEMENT` returns the preflight to
a candidate answer. Neither needs a source change, a rebuild, or a restart of
anything but the host: the fallback named in the gate's own report is the
existing synchronous reviewer (Guardian) answering the attempt inside the
original overall deadline, exactly as [`APPROVAL_PREFLIGHT.md`](APPROVAL_PREFLIGHT.md)
describes.

Exit codes: `0` = enforcement is disabled as declared (or would be, with
reasons), `10` = every declared condition holds and enforcement would be
enabled, `1` = a refusal or an unproven input, `2` = usage error.

## What is not measured

Every report carries `unmeasured`, and the rendered report prints it:
`provider_latency`, `model_accuracy`, `safety_in_the_wild`, `cost`. The shipped
criteria include two rows that an offline fixture cannot satisfy
(`availability_probed_on_a_live_model`, `no_unmeasured_safety_claim`); both
report `unmeasured`, and that is what keeps enforcement off until a consented,
budgeted live evaluation exists.

`verify_report()` re-derives a report from itself: a case counted in `topline`
but absent from the list it is counted from is a failure, a deferral or a
failure missing from the rendered report is a failure, a report with no
`unmeasured` list is a failure, and a machine-readable claim in
`FORBIDDEN_CLAIM_KEYS` (`latency_saved`, `speedup`, `tokens_saved`,
`safety_improvement`, and the like) is a failure. The required-CI tests mutate a
good report one way at a time to show each of those is caught.

## Evidence

Tier: **offline-fixture**. No live model, no provider, no network.

| Check | Command | Result |
| --- | --- | --- |
| Shipped holdout report is self-consistent | `python3 jev/scripts/shadow_comparison.py verify --set jev/fixtures/shadow/holdout.json` | `problems: []`; 12 cases, 8 agreements, 1 disagreement, 2 deferrals, 1 failure. |
| Shipped state keeps enforcement off | `python3 jev/scripts/shadow_comparison.py gate --set ... --criteria ... --freeze ...` | `enabled: false`, `guardian_only: true`, exit `0`; `blocked_by` names the switch, consent, model availability, opted-in categories, and criteria. |
| Every input is load-bearing | `python3 -m unittest discover -s .github/scripts -p 'test_jev_shadow.py'` | The all-conditions-satisfied case returns exit `10`; the switch, consent, model-availability, category and gate-declaration checks are each removed in turn, the freeze is perturbed, and the criteria are left unmet - the gate goes dark for every one of them. |
| The gate is not a rubber stamp on the shipped fixtures | same suite | All five measurable criteria *fail* on the shipped holdout (agreement 0.667 < 0.95, deferral 0.167 > 0.1, failure 0.083 > 0.05, disagreement 0.083 > 0.02, label mismatch 0.111 > 0.0), so the shipped answer is a refusal to enforce, not a fixture-shaped pass. |
| Redaction audit bites | same suite | Real raw text (a destructive command, a key header, a secret variable name) is checked against the records and every attached document; a positive case is clean and a negative control that leaks is reported. |
| Freeze drift is detected | same suite | An edited split is reported as drift and leaves enforcement disabled. |
| Manifest configuration cannot reach enforcement without the gate | same suite + `python3 jev/scripts/verify-manifest.py --patch-state applied --native-adapter applied` | Every shipped profile that enables `approval.enforcement` also enables the gate and shadow comparison; the manifest validates. |
| Formatting | `uv run --frozen --project scripts ruff format --check .` | `jev/scripts/shadow_comparison.py` and `.github/scripts/test_jev_shadow.py` are formatted. |
| Required-CI jev suites | `python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'` | 315 passed. |
| JEV component suites | `python3 -m unittest discover -s jev/tests -t jev/tests` | 119 passed. |

Not performed, and not claimed: a live-model calibration run, a real provider
availability probe, a token or cost measurement, an accuracy comparison against
a human reviewer in the wild, and any per-OS run. The labelled splits are
synthetic transcripts, and the labelling is procedurally independent (the
labels are recorded before the component was run, from the published policy)
rather than organizationally independent, because this repository has a single
GitHub identity.

## Disable and rollback

1. Set `JEV_SWITCH_APPROVAL_GUARDIAN_ONLY=1`, or unset
   `JEV_SWITCH_APPROVAL_ENFORCEMENT`, and restart the host. The existing
   synchronous reviewer answers every eligible attempt; no source change is
   needed and no evaluation state is consumed.
2. To disable the whole phase, run the `baseline` profile: both switches are
   absent, so `shadow_comparison.py` is never consulted and the manifest
   declares the features `false`.
3. Removing the evaluation artifacts (the fixtures, the frozen pin, and this
   module) only removes the ability to *enable* enforcement, never the ability
   to disable it. [`ROLLBACK.md`](ROLLBACK.md) covers the wider integration.
