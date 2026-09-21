#!/usr/bin/env python3
"""CI negative check: an unsupported combination must fail explicitly.

Builds a manifest that enables optional remote inference without consent or a
budget and asserts that the validator rejects it with a stable error code.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main():
    manifest_path = REPO_ROOT / "jev" / "compatibility-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["features"]["remote_inference.enabled"]["default"] = True
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "manifest.json"
        candidate.write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "jev" / "scripts" / "verify-manifest.py"),
                "--manifest",
                str(candidate),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 1 or "E_REMOTE_INFERENCE_UNAUTHORIZED" not in result.stderr:
        print(
            "expected an explicit rejection of unauthorized remote inference",
            file=sys.stderr,
        )
        print(f"exit={result.returncode}", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return 1
    print("unsupported combination rejected with E_REMOTE_INFERENCE_UNAUTHORIZED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
