#!/usr/bin/env python3
"""Validate the deterministic offline fixtures used by the isolated build.

The isolated build never calls a live provider. It replays recorded service
responses ("offline fixture" tier) and, for the mocked-service tier, points the
real host code path at those bytes through a local mock server. This runner
keeps that input honest and reproducible:

* every fixture is present and matches the digest recorded in
  ``jev/fixtures/manifest.json``;
* every SSE fixture is well formed and structurally deterministic (fixed ids,
  no timestamps or random nonces);
* no fixture carries a credential marker, so fixtures stay safe to commit.

It performs no network access and no model call. It only reads files.

Exit codes: 0 = valid, 1 = a fixture failed validation, 2 = usage or unreadable
input.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_manifest

FIXTURE_TIER = "offline fixture"
NONDETERMINISTIC_KEYS = {"created_at", "created", "timestamp", "nonce", "request_id", "trace_id"}
SECRET_MARKERS = ("sk-", "api_key", "authorization: bearer", "-----begin")
SSE_EVENT_RE = re.compile(r"^event: (?P<event>.+)$")
SSE_DATA_RE = re.compile(r"^data: (?P<data>.+)$")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Validate the deterministic offline service fixtures."
    )
    parser.add_argument(
        "--fixtures-root",
        default=None,
        help="Fixture root (default: jev/fixtures in the integration checkout).",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Fixture manifest (default: <fixtures-root>/manifest.json).",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    parser.add_argument("--quiet", action="store_true", help="Print nothing on success.")
    return parser.parse_args(argv)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def check_sse(path, text):
    """Return a list of ``CODE: message`` errors for one SSE fixture."""
    errors = []
    events = []
    current = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            current = None
            continue
        event_match = SSE_EVENT_RE.match(line)
        data_match = SSE_DATA_RE.match(line)
        if event_match:
            current = {"event": event_match.group("event"), "line": number}
            events.append(current)
            continue
        if data_match:
            if current is None:
                errors.append(
                    f"E_FIXTURE_FORMAT: {path.name}:{number} data line without a preceding event line"
                )
                continue
            try:
                payload = json.loads(data_match.group("data"))
            except json.JSONDecodeError as error:
                errors.append(
                    f"E_FIXTURE_FORMAT: {path.name}:{number} data line is not JSON: {error}"
                )
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                errors.append(
                    f"E_FIXTURE_FORMAT: {path.name}:{number} data must be a JSON object with a string type"
                )
                continue
            if payload["type"] != current["event"]:
                errors.append(
                    f"E_FIXTURE_FORMAT: {path.name}:{number} event {current['event']} does not match data type {payload['type']}"
                )
            current.setdefault("payloads", []).append(payload)
            continue
        errors.append(f"E_FIXTURE_FORMAT: {path.name}:{number} unexpected line: {line[:40]}")

    if not events:
        errors.append(f"E_FIXTURE_FORMAT: {path.name} contains no SSE events")
    for event in events:
        if "payloads" not in event:
            errors.append(
                f"E_FIXTURE_FORMAT: {path.name}:{event['line']} event {event['event']} has no data payload"
            )
            continue
        for payload in event["payloads"]:
            for key in _walk_keys(payload):
                if key.lower() in NONDETERMINISTIC_KEYS:
                    errors.append(
                        f"E_FIXTURE_NONDETERMINISTIC: {path.name} carries a time- or run-dependent field {key!r}"
                    )
    lower = text.lower()
    for marker in SECRET_MARKERS:
        if marker in lower:
            errors.append(
                f"E_FIXTURE_SECRET: {path.name} contains a credential-like marker {marker!r}"
            )
    return errors


def validate(fixtures_root, manifest_path):
    report = {"fixtures_root": str(fixtures_root), "manifest": str(manifest_path)}
    errors = []
    try:
        manifest = jev_manifest.load_json(manifest_path, "fixture manifest")
    except jev_manifest.ManifestError as error:
        return None, [f"E_FIXTURE_SCHEMA: {error}"]
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or not entries:
        return None, ["E_FIXTURE_SCHEMA: fixtures must be a non-empty array"]

    results = []
    for entry in entries:
        fixture_id = entry.get("id", "<missing id>")
        relative = entry.get("path")
        if not isinstance(relative, str):
            errors.append(f"E_FIXTURE_SCHEMA: fixture {fixture_id} is missing a path")
            continue
        path = fixtures_root / relative
        record = {"id": fixture_id, "path": relative, "tier": entry.get("tier", FIXTURE_TIER)}
        if not path.is_file():
            errors.append(f"E_FIXTURE_MISSING: fixture {fixture_id} not found at {relative}")
            results.append(record)
            continue
        actual = sha256_file(path)
        if actual != entry.get("sha256"):
            errors.append(
                f"E_FIXTURE_HASH: fixture {fixture_id} digest mismatch: manifest {entry.get('sha256')}, file {actual}"
            )
        record["sha256"] = actual
        if entry.get("kind") == "sse":
            errors.extend(check_sse(path, path.read_text(encoding="utf-8")))
        results.append(record)

    report["fixtures"] = results
    return report, errors


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.fixtures_root:
        fixtures_root = Path(args.fixtures_root).resolve()
    else:
        fixtures_root = jev_manifest.repository_root() / "jev" / "fixtures"
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else fixtures_root / "manifest.json"
    )
    report, errors = validate(fixtures_root, manifest_path)
    if report is not None:
        report["ok"] = not errors
        report["errors"] = errors
    if args.json and report is not None:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
    elif not args.quiet and report is not None:
        count = len(report["fixtures"])
        print(f"ok: {count} deterministic fixture(s) at tier '{FIXTURE_TIER}'")
    if report is None:
        return 2
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
