# Compatibility, upgrade, and rollback

## Resolution

A clean checkout resolves to exactly the inputs recorded in `manifest.json`.
`tools/verify_manifest.py` performs that resolution and exits non-zero on any
deviation, so an unpinned component cannot be pulled into a build silently.

```sh
python3 jev/tools/verify_manifest.py --checkout .
python3 jev/tools/verify_manifest.py --checkout . --json
```

Checks performed:

1. The host checkout revision equals `host.revision`.
2. Each installed component path reports the revision recorded for it.
3. Every feature switch starts at its manifest default, and the default set is off.
4. Each `requires` edge between switches is satisfied.
5. The runtime satisfies the manifest's Python requirement.
6. The platform is in the supported matrix.
7. No excluded component (OmniRoute) is referenced.
8. Unsupported combinations fail with a single explicit reason.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The checkout resolves to the pinned inputs. |
| `2` | A named unsupported combination matched. The reason is printed verbatim from the manifest. |
| `3` | The manifest or a required input is missing, unreadable, or malformed. |

An exit code of `2` is a refusal, not a warning: there is no override flag. Fixing
the request means changing the manifest through review, not passing a bypass.

## Pins and patch order

Source transformations apply in a fixed order — plaintext collaboration (`10`),
native adapter (`20`), approval port (`30`) — because later transforms are written
against the file contents left by earlier ones. The plaintext transform is
fail-closed: `scripts/apply-patch.py <checkout> --require-changes` exits non-zero if
upstream reworked the collaboration code, so a stale transform stops the build
instead of producing an unpatched binary.

## Disable and rollback

Every feature switch defaults to off, so a rollback to original behaviour is
available without rebuilding:

| Goal | Action |
| --- | --- |
| Disable all JEV behaviour | Leave `jev.enabled=false` (the default). |
| Disable projection only | Leave `jev.projection.enabled=false`; capture and retrieval stand alone. |
| Disable Sentinel enforcement | Set `jev.sentinel.mode="off"`. Host permission routing is unchanged in every mode. |
| Disable approval preflight | Set `jev.approval.mode="off"`; review falls back to the existing path. |
| Remove the plaintext transform | Revert patch `10` and rebuild from the pinned host revision. |
| Restore an unmodified host | Check out `host.upstream.revision`; the plaintext transform is the only source change that is not additive. |

The integration never writes into an existing agent profile: it uses an isolated
`CODEX_HOME` and isolated worktrees, so rolling back the integration leaves an
operator's existing profiles untouched.

## Known limitations

- Only `linux-x86_64-gnu` is validated in this environment. The other platforms in
  the matrix are documented, not measured.
- Offline fixture results are not live-provider evidence: they certify the
  integration's shape and failure behaviour, not model accuracy, safety, or speed.
- `omniroute-codex-docker` is deliberately absent and must not be added as a
  dependency.
