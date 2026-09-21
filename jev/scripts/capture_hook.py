#!/usr/bin/env python3
"""Host capture adapter: turn one lifecycle-hook payload into canonical evidence.

The isolated profile installs this command as the *first* handler for every
captured lifecycle event, ahead of the context fabric's own hook. It records
the host event - its canonical bytes, their SHA-256, its correlation
identifiers - before any component projects context into the outgoing request,
which is what contract C1 requires.

This adapter is capture-only:

* it never writes to stdout, so it can never inject context or a decision;
* it never modifies the payload or the transcript;
* it exits 0 even when it cannot capture, because a capture problem must not
  become a host failure. Whatever it skipped is written to the capture log
  instead, so a gap is visible rather than silent.

The capture directory comes from ``JEV_CAPTURE_DIR``, falling back to
``$CODEX_HOME/capture``; nothing is ever written outside it.

Usage: ``capture_hook.py [--event <HookEventName>] [--capture-dir <dir>]``
reading the host hook payload as JSON on stdin.

Exit codes: 0 = recorded or deliberately skipped, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_envelope


def capture_dir(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("JEV_CAPTURE_DIR"):
        return Path(os.environ["JEV_CAPTURE_DIR"])
    home = os.environ.get("CODEX_HOME")
    return Path(home) / "capture" if home else Path.cwd() / ".jev" / "capture"


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event")
    parser.add_argument("--capture-dir")
    return parser.parse_args(argv)


def record(raw, payload, expected_event, directory):
    log = event_envelope.CaptureLog(directory / "events.jsonl")
    hook_event_name = (
        payload.get("hook_event_name") if isinstance(payload, dict) else None
    )
    if expected_event and hook_event_name and expected_event != hook_event_name:
        log.append_skipped(hook_event_name, "event_mismatch")
        return 0
    if hook_event_name is None:
        log.append_skipped("unknown", "missing_hook_event_name")
        return 0
    envelope = event_envelope.envelope_from_payload(payload)
    if envelope is None:
        log.append_skipped(hook_event_name, "unsupported_or_incomplete_event")
        return 0
    content = event_envelope.event_content(payload)
    log.append(envelope, raw=raw, content=content)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(argv)
    except SystemExit:
        return 2
    raw = sys.stdin.read()
    directory = capture_dir(args.capture_dir)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        event_envelope.CaptureLog(directory / "events.jsonl").append_skipped(
            args.event or "unknown", "unparseable_payload"
        )
        return 0
    try:
        return record(raw, payload, args.event, directory)
    except OSError as error:
        print(f"jev capture hook could not write evidence: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
