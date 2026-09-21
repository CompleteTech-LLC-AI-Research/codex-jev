# Budgeted retrieval and hydration

Phase #4 (#14) makes the canonical capture store usable without ever turning
recalled content into authority. `jev/scripts/retrieval.py` reads the store
built by [`canonical_capture.py`](CAPTURE.md) and exposes it two ways:

| Subcommand | What it returns |
| --- | --- |
| `search` | Source-backed excerpts that match a query, from the redacted retrieval view. |
| `hydrate` | The exact canonical bytes behind one event, after re-proving its origin. |

This is contract [C1](CONTRACTS.md) and [C9](CONTRACTS.md): retrieval returns
source-backed excerpts marked untrusted and possibly stale, and no component
receives authority through a recalled excerpt.

## Budgets are explicit

Every call spends a declared budget and reports exactly how much it spent:

| Budget | Default | Meaning |
| --- | --- | --- |
| `--max-excerpts` | 8 | Most excerpts a single search may return. |
| `--max-bytes` | 4096 | Most bytes of excerpt text a single call may return. |
| `--max-tokens` | 1024 | Approximate token ceiling (`bytes / 4`), applied alongside the byte cap. |

The effective byte cap is `min(max-bytes, max-tokens * 4)`. A search that hits
the cap stops and sets `truncated: true`, reporting `matched` and `dropped`
counts, so an operator can see that more evidence exists without pulling it in.
A hydration longer than the cap is clipped the same way. A degenerate budget
(zero or negative) is refused rather than silently widened.

## Provenance, not authority

Every excerpt and every hydration carries:

- `untrusted: true` and `possibly_stale: true`;
- provenance: the source rollout, its line number, the ordinal, the origin
  record's SHA-256, and the content hash;
- the rule that recalled content is evidence and never authorization.

`search` reads the redacted retrieval view, so credential-shaped spans are
already replaced. `hydrate` returns the canonical bytes, because canonical
content never leaves the isolated store, but only after re-proving that the
content file still matches its recorded hash and that the origin record still
matches its recorded hash.

## Refusals

Hydration reports drift instead of returning stale bytes as if they were
current. Each refusal returns `ok: false` and a `status`, and the CLI exits `1`:

| Status | Meaning |
| --- | --- |
| `workspace_mismatch` | The event was captured in a different workspace; retrieval refuses to cross workspaces. |
| `missing_content` | The canonical content file is gone. |
| `content_changed` | The stored content no longer matches its recorded hash. |
| `missing_source` | The origin rollout is gone, so provenance cannot be re-proved. |
| `source_changed` | The origin record changed since capture; the transcript is no longer canonical evidence. |

A search scoped to a workspace with no captures returns no excerpts and reports
`workspace_matched: false` while listing the workspaces that are present, so a
mismatch is visible rather than silently empty.

## No implicit remote call

The manifest records no consent and no budget for `remote_inference`, so
`--remote-enrichment` is refused with `E_REMOTE_ENRICHMENT_UNAUTHORIZED` and
retrieval stays local. The module makes no network call: the tests patch
`socket.socket` to raise and confirm that search and hydration still succeed.

## Commands

```sh
python3 jev/scripts/retrieval.py search --query "offline plaintext" --workspace /workspace/demo
python3 jev/scripts/retrieval.py search --kind tool_result --max-excerpts 4 --max-bytes 1024
python3 jev/scripts/retrieval.py hydrate --event-id evt_... --max-tokens 256
```

Exit codes: `0` ok, `1` a refusal or an unproven reference, `2` usage error.
