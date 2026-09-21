#!/usr/bin/env python3
"""Minimal Responses API mock that makes a real host attempt one shell action.

This exists so the Sentinel hook path can be exercised by a real Codex host
process without any live provider traffic: the host is driven to try exactly one
`exec_command` action, and the marker that action would create is the
externally observable difference between "the action ran" and "the action was
prevented before execution". It is a test fixture, not a service.

Usage:
    mock_sentinel_server.py --requests-dir DIR [--marker PATH] [--canary TEXT] \
        [--port N] [--port-file FILE]

The script is fixed:
  * the first turn answers with an `exec_command` function call whose `cmd`
    creates `--marker`, and whose text carries `--canary`,
  * any turn whose request already carries a `function_call_output` answers with
    a plain assistant message, so the run completes whether the action executed
    or was prevented.

Every request body is written to disk unchanged, so the caller can assert on
what the host actually sent. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The component's own deterministic rule: any text carrying this marker is a
#: `BLOCK` with reason `installation_test_canary`. Under `mode = "enforce"` and
#: the declared enforcement switch that is a veto the host has to honor.
DEFAULT_CANARY = "JEV_SENTINEL_TEST_BLOCK"

SENTINEL_CALL_ID = "call_jev_sentinel_action_1"
SENTINEL_FINAL_TEXT = "sentinel transcript complete"


def sse(payload: dict) -> bytes:
    return (
        f"event: {payload['type']}\n".encode()
        + b"data: "
        + json.dumps(payload, separators=(",", ":")).encode()
        + b"\n\n"
    )


def stream_bytes(events: list[dict]) -> bytes:
    return b"".join(sse(event) for event in events)


def usage() -> dict:
    return {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }


def message_stream(response_id: str, text: str) -> bytes:
    item = {
        "type": "message",
        "id": f"msg_{response_id}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return stream_bytes(
        [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {"id": response_id, "end_turn": True, "usage": usage()},
            },
        ]
    )


def command_stream(response_id: str, command: str) -> bytes:
    item = {
        "type": "function_call",
        "id": f"fc_{response_id}",
        "name": "exec_command",
        "call_id": SENTINEL_CALL_ID,
        "arguments": json.dumps({"cmd": command}),
        "status": "completed",
    }
    return stream_bytes(
        [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {"id": response_id, "end_turn": True, "usage": usage()},
            },
        ]
    )


def answered(body: dict) -> bool:
    """Whether the transcript already carries a result for the action.

    The host resends the whole transcript on every turn, so the presence of a
    `function_call_output` is a stable signal that the action has been resolved
    - executed or prevented - and the turn only needs its closing message.
    """
    items = body.get("input")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in items
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jev-sentinel-mock/1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("mock: " + (fmt % args) + "\n")

    def _record(self, raw: bytes) -> None:
        directory = self.server.requests_dir  # type: ignore[attr-defined]
        os.makedirs(directory, exist_ok=True)
        with self.server.lock:  # type: ignore[attr-defined]
            index = self.server.next_index  # type: ignore[attr-defined]
            self.server.next_index = index + 1  # type: ignore[attr-defined]
        with open(os.path.join(directory, f"request-{index:02d}.json"), "w") as handle:
            handle.write(raw.decode("utf-8", "replace"))

    def _reply(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.startswith("/v1/responses"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._record(raw)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # pragma: no cover - defensive
            body = {}
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.turns.append(body)  # type: ignore[attr-defined]
            count = len(self.server.turns)  # type: ignore[attr-defined]
        response_id = f"resp_jev_sentinel_{count}"
        if answered(body):
            self._reply(message_stream(response_id, SENTINEL_FINAL_TEXT))
        else:
            self._reply(command_stream(response_id, self.server.command))  # type: ignore[attr-defined]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--marker", default="")
    parser.add_argument("--canary", default=DEFAULT_CANARY)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", default="")
    args = parser.parse_args(argv)

    marker = args.marker
    if not marker:
        parser.error("--marker is required: the action needs something to create")
    # The action is one shell command that would create the marker and would
    # also carry the canary, so the same action is both the thing to prevent and
    # the thing the component is meant to notice.
    command = f"printf '%s\\n' {args.canary} > {marker} && printf '%s\\n' {args.canary}"

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.requests_dir = args.requests_dir  # type: ignore[attr-defined]
    server.command = command  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    server.next_index = 0  # type: ignore[attr-defined]
    server.turns = []  # type: ignore[attr-defined]

    if args.port_file:
        with open(args.port_file, "w") as handle:
            handle.write(str(server.server_address[1]))

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
