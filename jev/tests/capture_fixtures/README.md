# Capture fixtures

Synthetic Codex rollout transcripts, used by
`.github/scripts/test_jev_capture.py` to pin the canonical-capture contract
without a live host. They mirror the shape of the records the real host writes
under `$CODEX_HOME/sessions/<year>/<month>/<day>/rollout-*.jsonl`.

Every file here is tier **`rollout-fixture`**, never host evidence:
`jev/scripts/canonical_capture.py` labels any rollout found under `jev/tests/`
that way, and `index.json` records the tier of every source it read. A real
rollout is tier `real-host-rollout`.

| Fixture | What it pins |
| --- | --- |
| `01-simple-turn.jsonl` | One user turn and one assistant turn, each reported on two native surfaces (the typed item and the wire transcript), which must collapse to one event with a dedup receipt. |
| `02-tool-call.jsonl` | A tool call and its result, correlated by `call_id`. |
| `03-collab-plaintext.jsonl` | A `spawn_agent` collaboration call carrying a plaintext message, classified as `collab_message` and correlated by `call_id`. |
| `04-secret-bearing.jsonl` | Credential shapes in canonical content. Every value is fake and labelled as such; the retrieval view must replace them while canonical content keeps them. |
| `05-retry-duplicate.jsonl` | The host appended the same completed item twice; the repeat must not multiply records. |
| `06-gap.jsonl` | A record the host never wrote (ordinal 2 absent) must be reported as a capture gap. |
| `07-truncated.jsonl` | A transcript truncated mid-write must be reported as a capture gap, not silently ignored. |
| `08-orphan-tool-result.jsonl` | A tool result whose call is absent must be reported as a capture gap. |
| `09-injected-context.jsonl` | Injected user-role context (AGENTS.md instructions, world state) is not operator speech and must not become an event. |
| `10-hostile-instruction.jsonl` | A captured user turn carrying a prompt-injection instruction, so the retrieval and screening path can be exercised on captured bytes instead of a swapped-in candidate text. |

The credential values in `04-secret-bearing.jsonl` are deliberately
non-functional placeholders (`sk-FIXTURE-NOT-A-REAL-KEY-0000`,
`ghp_FIXTURENOTAREALTOKEN0000000`, a synthetic JWT), and the fixture text says
so, so no reader or scanner can mistake them for live credentials.
