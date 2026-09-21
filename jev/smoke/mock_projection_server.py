#!/usr/bin/env python3
"""A deterministic loopback Responses-API mock for the projection/reset harness.

Unlike ``mock_responses_server.py`` (which scripts a parent/child collaboration
turn), this mock is deliberately content-free: every ``POST /v1/responses`` is
recorded verbatim and answered with the same final assistant message. The
transcript a run is judged on therefore comes entirely from the seeded rollout
the host resumes, not from anything this mock invents, so the harness can treat
the recorded request body as the host's own outgoing payload.

Recorded shapes (matching ``mock_responses_server.py`` so one checker can read
both): ``request-NN.json`` for an uncompressed body, or ``request-NN.body.bin``
plus ``request-NN.encoding`` when the host compressed it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def sse(payload: dict) -> bytes:
    return (
        f"event: {payload['type']}\n".encode()
        + b"data: "
        + json.dumps(payload, separators=(",", ":")).encode()
        + b"\n\n"
    )


def message_stream(response_id: str, text: str) -> bytes:
    item = {
        "type": "message",
        "id": f"msg_{response_id}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    body = sse({"type": "response.created", "response": {"id": response_id}})
    body += sse({"type": "response.output_item.done", "item": item})
    body += sse(
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
        }
    )
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jev-projection-mock/1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("mock: " + (fmt % args) + "\n")

    def _record(self, raw: bytes, content_encoding: str | None) -> int:
        directory = self.server.requests_dir  # type: ignore[attr-defined]
        os.makedirs(directory, exist_ok=True)
        with self.server.lock:  # type: ignore[attr-defined]
            index = self.server.next_index  # type: ignore[attr-defined]
            self.server.next_index = index + 1  # type: ignore[attr-defined]
        if content_encoding:
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

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        index = self._record(raw, self.headers.get("Content-Encoding"))
        if not self.path.startswith("/v1/responses"):
            self._json(404, {"error": {"message": f"unrouted path {self.path}"}})
            return
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.count += 1  # type: ignore[attr-defined]
            count = self.server.count  # type: ignore[attr-defined]
        reply_text = self.server.reply_text  # type: ignore[attr-defined]
        payload = message_stream(f"resp_jev_projection_{count}", reply_text)
        with open(
            os.path.join(self.server.requests_dir, f"request-{index:02d}.turn"), "w"
        ) as handle:  # type: ignore[attr-defined]
            handle.write(f"turn-{count}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        self._json(404, {"error": {"message": f"unrouted path {self.path}"}})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--reply-text",
        default="acknowledged; no further action taken",
        help="final assistant text returned for every request",
    )
    args = parser.parse_args()

    os.makedirs(args.requests_dir, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.requests_dir = os.path.abspath(args.requests_dir)  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    server.next_index = 0  # type: ignore[attr-defined]
    server.count = 0  # type: ignore[attr-defined]
    server.reply_text = args.reply_text  # type: ignore[attr-defined]

    def stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with open(args.port_file, "w") as handle:
        handle.write(str(server.server_address[1]))
    sys.stderr.write(f"mock: listening on {args.host}:{server.server_address[1]}\n")
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
