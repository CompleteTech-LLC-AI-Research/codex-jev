# Rollback

Everything the isolated environment writes lives under a single directory,
`.jev/` inside the integration checkout, or under an explicit `--env-dir`. No
script reads or writes the ambient Codex home, the ambient session state, or any
WSL distribution.

## Disable the integration

Two independent switches, in increasing order of effort:

1. **Stop using the isolated environment.** Launch Codex as before. The pinned
   build only behaves differently when it runs against `.jev/isolated/home`.
2. **Return to the unpatched host behaviour.** Apply no patches
   (`jev/scripts/apply-patches.py` with an empty patch list, or
   `--patch-state absent` verification) and rebuild with `cargo build -p
   codex-cli`. The build then keeps the upstream encrypted collaboration schema,
   which is the manifest's `disable` entry for patch `0001-plaintext-collab`.

Both are reproducible: `python3 jev/scripts/verify-manifest.py --patch-state
absent` fails closed if a patch is still applied.

## Remove the isolated environment

```
python3 jev/scripts/isolated_env.py rollback
```

This moves `.jev/isolated` to `.jev/superseded/isolated-<UTC timestamp>` and
prints both paths. It never deletes, so an operator can inspect or restore the
record afterwards. Use `--env-dir` to roll back an environment created
elsewhere, and `--target-root` to choose where the record is kept.

To finish the removal, delete the `superseded` directory yourself once you have
inspected it. Nothing in the repository depends on it.

## After a bad build

The environment records the binary path and its SHA-256 in `isolated-env.json`
and `logs/invocations.jsonl`. If a rebuild changes the binary hash, `status`
reports `binary_matches_plan: false` and the next launch refuses nothing - it
records the new hash - so re-run
`python3 jev/scripts/build_provenance.py record --out <artifact>` to refresh the
provenance record before publishing results.

## What is deliberately not reversible

- **Ciphertext already stored by an older build is never decrypted.** Rolling
  back the patch does not recover plaintext for records written while the
  encrypted schema was active; those records stay opaque.
- **Remote inference stays off.** Rollback does not enable it, and no script
  here can, because `remote_inference.enabled` also requires explicit operator
  consent and a budget in the manifest.
