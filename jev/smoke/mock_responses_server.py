#!/usr/bin/env python3
"""Minimal Responses API mock that records every request body it receives.

This exists so the plaintext-collaboration change can be exercised by a real
Codex host process without any live provider traffic. It is a test fixture, not
a service: it speaks just enough of the streaming Responses protocol for
`codex exec` to complete a turn, and it writes every request body to disk
unchanged so the caller can assert on what the host actually sent.

Usage:
    mock_responses_server.py --requests-dir DIR [--port N] [--port-file FILE]

The response script is fixed:
  * the first uncorrelated turn answers with a `spawn_agent` function call whose
    `message` argument is the sentinel task text,
  * a turn whose request already contains that sentinel is treated as the child
    agent and answers with a plain assistant message,
  * once the parent's request contains the `spawn_agent` function call output,
    it answers with a final plain assistant message.

The parent's post-spawn turn is held open until the child turn has been served
(`--child-rendezvous-seconds`, default 20). A spawned agent runs concurrently
with its parent, and the host exits as soon as the root turn completes, so
without a rendezvous the child's request can race with process exit and the run
silently records no child turn at all.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENTINEL_TASK = "JEV-SMOKE-PLAINTEXT-TASK-7f3c"
SPAWN_CALL_ID = "call_jev_smoke_spawn_1"
# Collaboration tools are advertised inside a Responses API namespace, so a
# faithful function call must carry that namespace. A bare `spawn_agent` call is
# rejected as "unsupported call" by the tool router.
COLLAB_NAMESPACE = "collaboration"


def sse(payload: dict) -> bytes:
    return (
        f"event: {payload['type']}\n".encode()
        + b"data: "
        + json.dumps(payload, separators=(",", ":")).encode()
        + b"\n\n"
    )


def stream_bytes(events: list[dict]) -> bytes:
    body = b""
    for event in events:
        body += sse(event)
    return body


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
                "response": {
                    "id": response_id,
                    "end_turn": True,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            },
        ],
    )


def spawn_stream(response_id: str) -> bytes:
    item = {
        "type": "function_call",
        "id": f"fc_{response_id}",
        "name": "spawn_agent",
        "namespace": COLLAB_NAMESPACE,
        "call_id": SPAWN_CALL_ID,
        "arguments": json.dumps(
            {"task_name": "jev_smoke_child", "message": SENTINEL_TASK}
        ),
        "status": "completed",
    }
    return stream_bytes(
        [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "end_turn": True,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            },
        ],
    )


def classify(body: dict) -> str:
    """Return the scripted turn for a request body."""
    blob = json.dumps(body)
    if SPAWN_CALL_ID in blob and '"function_call_output"' in blob:
        return "parent_after_spawn"
    if SENTINEL_TASK in blob:
        return "child"
    return "parent_initial"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jev-smoke-mock/1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("mock: " + (fmt % args) + "\n")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _record(self, raw: bytes, content_encoding: str | None) -> int:
        directory = self.server.requests_dir  # type: ignore[attr-defined]
        os.makedirs(directory, exist_ok=True)
        with self.server.lock:  # type: ignore[attr-defined]
            index = self.server.next_index  # type: ignore[attr-defined]
            self.server.next_index = index + 1  # type: ignore[attr-defined]
        if content_encoding:
            # The host only compresses when a provider opts in; record the fact
            # and keep the raw bytes so nothing is silently lost.
            with open(
                os.path.join(directory, f"request-{index:02d}.body.bin"), "wb"
            ) as handle:
                handle.write(raw)
            with open(
                os.path.join(directory, f"request-{index:02d}.encoding"), "w"
            ) as handle:
                handle.write(content_encoding)
            return index
        with open(os.path.join(directory, f"request-{index:02d}.json"), "w") as handle:
            handle.write(raw.decode("utf-8", "replace"))
        return index

    def _label(self, index: int, turn: str) -> None:
        directory = self.server.requests_dir  # type: ignore[attr-defined]
        with open(os.path.join(directory, f"request-{index:02d}.turn"), "w") as handle:
            handle.write(turn)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        raw = self._read_body()
        index = self._record(raw, self.headers.get("Content-Encoding"))
        if not self.path.startswith("/v1/responses"):
            self._json(404, {"error": {"message": f"unrouted path {self.path}"}})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as error:  # pragma: no cover - defensive
            self._json(400, {"error": {"message": f"bad body: {error}"}})
            return
        turn = classify(body)
        self._label(index, turn)
        if turn == "parent_after_spawn":
            self._await_child_turn()
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.turns.append(turn)  # type: ignore[attr-defined]
            count = len(self.server.turns)  # type: ignore[attr-defined]
        response_id = f"resp_jev_smoke_{count}"
        payload = {
            "parent_initial": spawn_stream,
            "child": lambda rid: message_stream(
                rid, "child agent received plaintext task"
            ),
            "parent_after_spawn": lambda rid: message_stream(
                rid, "parent observed child"
            ),
        }[turn](response_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _await_child_turn(self) -> None:
        """Block the parent's turn until the child turn has been served.

        The host exits as soon as the root turn completes, so answering the
        parent immediately lets the process outrun its own child agent. Waiting
        here makes the child turn observable instead of a race.
        """
        budget = self.server.child_rendezvous_seconds  # type: ignore[attr-defined]
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            with self.server.lock:  # type: ignore[attr-defined]
                if "child" in self.server.turns:  # type: ignore[attr-defined]
                    return
            time.sleep(0.05)
        sys.stderr.write(
            f"mock: no child turn within {budget:g}s; answering the parent anyway\n"
        )

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": []})
            return
        self._json(404, {"error": {"message": f"unrouted path {self.path}"}})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", default=None)
    parser.add_argument("--child-rendezvous-seconds", type=float, default=20.0)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.requests_dir = os.path.abspath(args.requests_dir)  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    server.turns = []  # type: ignore[attr-defined]
    server.next_index = 0  # type: ignore[attr-defined]
    server.child_rendezvous_seconds = args.child_rendezvous_seconds  # type: ignore[attr-defined]

    port = server.server_address[1]
    if args.port_file:
        with open(args.port_file, "w") as handle:
            handle.write(str(port))

    def stop(_signum, _frame) -> None:
        # Serve in a separate thread so a signal can unwind the loop cleanly and
        # the transcription is still written on SIGTERM.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(port, flush=True)
    server.serve_forever()
    server.server_close()
    transcription = {
        "turns": server.turns,  # type: ignore[attr-defined]
        "sentinel_task": SENTINEL_TASK,
        "spawn_call_id": SPAWN_CALL_ID,
        "collab_namespace": COLLAB_NAMESPACE,
    }
    with open(
        os.path.join(server.requests_dir, "turns.json"),  # type: ignore[attr-defined]
        "w",
    ) as handle:
        json.dump(transcription, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
