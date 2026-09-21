# Deterministic offline fixtures

These fixtures are the recorded service inputs for the isolated build. They are
deterministic: fixed ids, no timestamps, no run-dependent nonces, and no
credentials. The validator in `jev/scripts/run-offline-fixtures.py` enforces all
of that and checks each file against the digest recorded in `manifest.json`.

Every fixture is used at the **offline fixture** tier. It is never presented as
evidence of live-model accuracy, safety, or performance.

| Path | Purpose |
| --- | --- |
| `manifest.json` | Fixture registry: path, kind, tier, digest, and description. |
| `responses/plaintext_collab_smoke.sse` | A parent turn that delegates with plaintext `spawn_agent` and `followup_task` arguments. |
| `responses/encrypted_collab_replay.sse` | A returned collaboration call that still carries `encrypted_function_args`, proving the encrypted transport stays reachable. |
| `codex-home/config.toml` | The isolated `CODEX_HOME` profile with every optional product feature disabled. |

## Format

`responses/*.sse` files use the same event framing the host consumes from the
Responses API: one `event:` line followed by one `data:` line holding a single
JSON object, with a blank line between events. The `data` object's `type` must
match its `event` name. `response.output_item.done` events carry the
`function_call` or `message` item the model returned.

## Adding a fixture

1. Add the file under this directory.
2. Compute `sha256sum` of the file.
3. Add an entry to `manifest.json` with the digest and a one-line description.
4. Run `python3 jev/scripts/run-offline-fixtures.py` and confirm it reports `ok`.
