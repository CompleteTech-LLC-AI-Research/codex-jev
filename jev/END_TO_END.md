# Composed end-to-end regression

Phase 6.1 asks for one harness over the _composed_ JEV stack, so a regression
anywhere in the chain fails in one place instead of only in the owning boundary's
own suite. The harness is [`scripts/e2e_regression.py`](scripts/e2e_regression.py);
its required-CI home is [`../.github/scripts/test_jev_e2e.py`](../.github/scripts/test_jev_e2e.py),
and the real-component tier is covered by
[`tests/test_e2e_real_component.py`](tests/test_e2e_real_component.py).

Everything here is offline and hermetic: the inputs are checked-in fixtures, the
component calls are local subprocesses, no provider is contacted, and no paid
inference is used. Nothing in this harness measures model accuracy, safety, or
speed.

## Composed order

The order is the request lifecycle in [`ARCHITECTURE.md`](ARCHITECTURE.md). Each
step consumes the artifact the previous one produced, rather than re-deriving it:

| #   | Step                | What runs                                                                                                              | Artifact carried forward                                              | Tier of its assertions                                               |
| --- | ------------------- | ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1   | `capture`           | The checked-in rollout JSONL replayed through the shipped capture reader (`canonical_capture`).                        | Envelope events, dedup receipts, gaps, a redacted retrieval view.     | `rollout-fixture`, plus one `offline-fixture` default-profile check. |
| 2   | `retrieval`         | Budgeted search + provenance hydration over that capture.                                                              | Sourced, untrusted excerpts and hydrated references.                  | `rollout-fixture` / `offline-fixture`.                               |
| 3   | `screening`         | The screening planner and the withhold journal, including the fail-closed corrupt policy.                              | Accepted evidence, the withheld journal, the injection block.         | `rollout-fixture` / `offline-fixture`.                               |
| 4   | `projection`        | Bus stage 100 dedup over the real subprocess transport, then stage 200 approved Fabric prose view; then the negatives. | The deduped request, the approved view, the projection receipts.      | `bus-stage-stub` (stage 100), `offline-fixture` (the rest).          |
| 5   | `collab`            | The plaintext parent→child collaboration turn as it lands in the capture.                                              | A first-class `collab_message` event and its paired result.           | `rollout-fixture` / `offline-fixture`.                               |
| 6   | `sentinel_and_veto` | The Sentinel boundary over three wired paths, then the veto carrier and the session latch.                             | The effective decisions, the correlated incidents, the session latch. | `component-stub`, plus one `offline-fixture` shape check.            |
| 7   | `approval`          | The shipped approval streams, the shadow comparator, and the enforcement gate over the latched verdict.                | The correlated report, the readiness gate answer, the frozen split.   | `offline-fixture` / `component-stub`.                                |
| 8   | `execution`         | Asserts the composed order and that the trace itself is honest.                                                        | The declared order, the tier census, the honesty checks.              | `offline-fixture`.                                                   |

## Evidence tiers

Every assertion carries the tier that produced it, because the vocabulary
matters more than a single "passed" bit. The document also carries a
`tier_labels` census, so a run that claims more than it did is visibly wrong.

| Tier              | Meaning                                                                                                                                                               |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rollout-fixture` | A checked-in rollout JSONL replayed through the _shipped_ capture reader: the envelope, correlation ids, and redaction are real code over synthetic input.            |
| `offline-fixture` | Checked-in fixtures driven through shipped host modules (screening, approval correlation, the isolated env).                                                          |
| `bus-stage-stub`  | The bus boundary over its real wire/subprocess transport with the checked-in stage doubles under `tests/bus_stage_stub`. The doubles are not the components.          |
| `component-stub`  | A minimal `launch.py` implementing only the Sentinel component's documented wire, so the host carrier can be driven without the component checkout.                   |
| `real-component`  | Only when `--component` names a pinned `jev-sentinel` checkout, or `JEV_SENTINEL_ROOT` is set: the _real_ evaluator, policy handling, audit store, and session latch. |

The ternary distinction is explicit in `tier_labels`: `mocked_service` holds the
stub fixtures, while `real_host` and `live_provider` are **empty**. There is
deliberately no `real-host` or `live-provider` tier here; those are the smokes
under `smoke/` and the live run tracked by #25.

## Run it

```sh
# stub component (the default; no checkout needed)
python3 jev/scripts/e2e_regression.py run --json /tmp/e2e-trace.json

# real component (pinned checkout resolved through launch.py)
JEV_SENTINEL_ROOT=/path/to/jev-sentinel python3 jev/scripts/e2e_regression.py run

# keep the scratch tree for inspection
python3 jev/scripts/e2e_regression.py run --scratch /tmp/e2e --keep
```

Exit codes: `0` every assertion passed, `1` at least one assertion failed, `2`
the inputs were unusable (a missing or out-of-tree fixture directory, or a
`--component` root with no `launch.py`). An unusable fixture directory is a
usage error rather than a silent fallback, so a fixture copy can never be
mislabelled `real-host` evidence.

The trace document is deterministic: two runs over the same fixtures produce the
same `digest` and the same JSON, which is what makes a change in the digest
meaningful.

## What the scenarios pin

- `capture` replays the rollout into the declared envelope events and proves
  every store invariant; `retrieval` marks recalled context as untrusted
  evidence, refuses cross-workspace hydration, and refuses unauthorized remote
  enrichment.
- `screening` withholds the duplicate and injects only accepted evidence,
  journals a withheld row before it is honoured, and quarantines on a corrupt
  policy instead of injecting.
- `projection` replaces only the proven duplicate read body, keeps the retained
  witness, never mutates the canonical request, applies an approved view that
  removes exactly the planned prose, separates measured bytes from estimated
  tokens, and refuses a stale view, reverts an unapproved removal, and leaves the
  request byte-identical when the switches are off or the turn is cancelled.
- `collab` reads the parent→child message as plaintext and captures it before any
  projection; `sentinel_and_veto` activates all three wired paths with a canary,
  correlates content-free incidents, latches on an enforced finding, denies an
  ordinary tool call that inherits the latch, requires explicit confirmation to
  clear, and observes without vetoing in shadow mode.
- `approval` correlates the shipped streams one-to-one, claims no speedup and no
  ground truth, never flips a switch, freezes a scenario-family split, and proves
  a later allowance-shaped verdict cannot clear a latched veto.

## What this harness does not prove

- **No live provider.** No provider call is made and no model is measured; the
  token figure is a byte-derived estimate, reported separately from measured
  bytes, and `token_measurement.measured` is `null`.
- **Stage 200 is answered in-process.** The pinned context fabric exposes no
  bus-stage entry point, so only stage 100 dedup runs over the real subprocess
  transport; the view stage exercises the shipped host-side `fabric_views` code
  against a fixture, not a component subprocess.
- **`real-component` needs a checkout.** Without a pinned `jev-sentinel`
  checkout the Sentinel and veto assertions are `component-stub`-tier only; the
  real-component test skips with that reason rather than passing quietly.
- **Synthetic input.** The fixtures are labelled and deterministic; this is
  regression evidence about the wiring, not evidence about any real transcript's
  quality or safety.

## Reproduce the counts

```sh
python3 -m unittest discover -s .github/scripts -p 'test_jev_*.py'   # required-CI home
python3 -m unittest discover -s jev/tests -t jev/tests                # component + real-component homes
```

At the time of writing the required-CI battery runs 365 tests and `jev/tests`
runs 167; both counts move as suites are added. The composed run reports 55
assertions: `rollout-fixture` 10, `offline-fixture` 33, `bus-stage-stub` 4,
`component-stub` 8 — or, with a pinned checkout, `component-stub` 8 becomes
`real-component` 8.
