#!/usr/bin/env python3
"""Minimal Responses API mock that records every request body it receives.

This exists so the plaintext-collaboration change can be exercised by a real
Codex host process without any live provider traffic. It is a test fixture, not
a service: it speaks just enough of the streaming Responses protocol for
`codex exec` to complete a turn, and it writes every request body to disk
unchanged so the caller can assert on what the host actually sent.

Usage:
    mock_responses_server.py --requests-dir DIR [--script NAME] [--port N] [--port-file FILE]

The `plaintext` script is fixed:
  * the first uncorrelated turn answers with a `spawn_agent` function call whose
    `message` argument is the sentinel task text,
  * a turn whose request already contains that sentinel is treated as the child
    agent and answers with a plain assistant message,
  * once the parent's request contains the `spawn_agent` function call output,
    it answers with a final plain assistant message.

The `projection` script drives the same host through a longer transcript that
contains an eligible duplicate read pair, so the jev-bus boundary has something
it can actually project:
  * the first `PROJECTION_READS` turns answer with a `memories` `read` function
    call; the first two are identical in tool and arguments, so the host answers
    both with the same body, while every later call names a different path and
    so forms its own group,
  * every turn after that answers with a plain assistant message, so a second
    `codex exec resume` turn is the request that carries the whole transcript
    and therefore the one the boundary can project.

The duplicate pair is deliberately early and the transcript deliberately long:
the host only accepts a dedup receipt for a pair that is outside the current
user turn *and* outside the last `RECENT = 16` items, so a short transcript
cannot demonstrate a projection at all.

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

#: The projection script's read tool, and the namespace it has to be called
#: through. The pinned dedup policy only proves a replacement for a read-family
#: call (`read`, `read_file`, `file_read`), and the host re-derives the receipt
#: from the call's own name and arguments rather than trusting the stage that
#: proposed the replacement. The bus view carries the bare `name` and never
#: reads the namespace, so the name has to stay bare while the namespace rides
#: alongside in its own field -- which is exactly the pair the pinned policy
#: proves and the router routes.
#:
#: This fork ships no *unconditional* built-in read tool, and the routes that do
#: exist are unusable as evidence: a bare `read_file` call is answered by the
#: router with a 27-byte `unsupported call` string (byte-identical, but shorter
#: than the marker that would replace it, so nothing can be pruned), an MCP read
#: prepends a nondeterministic `Wall time:` header and returns a content-item
#: list that the boundary treats as opaque, and the notes/skills read tools need
#: a live provider or an orchestrator provider. The memories extension is the
#: one route that returns a *deterministic* body (`JsonToolOutput` over the
#: parsed `ReadMemoryResponse`, no wall-clock header), so the fixture drives it
#: and the harness enables it with `[features] memories = true` plus
#: `[memories] dedicated_tools = true`.
PROJECTION_READ_TOOL = "read"
PROJECTION_READ_NAMESPACE = "memories"
#: Read turns the projection transcript drives before it stops calling tools.
PROJECTION_READS = 11
#: Paths are relative to the memories root (`$CODEX_HOME/memories`), which the
#: harness populates before the run. The first two calls are identical in path,
#: so their bodies are identical too; every later call reads a different path so
#: it can never join the same group.
PROJECTION_PATHS = ("jev-probe.txt", "jev-probe.txt") + tuple(
    f"probe-{index:02d}.txt" for index in range(3, PROJECTION_READS + 1)
)
#: The projection transcript's closing assistant turn, once every read is spent.
PROJECTION_FINAL_TEXT = "projection transcript complete"
PROJECTION_SCRIPTS = ("plaintext", "projection")


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


def read_stream(response_id: str, call_id: str, name: str, arguments: dict) -> bytes:
    """Answer with one function call to the fixture's read tool.

    The namespace has to be attached for the call to route at all -- this fork
    has no built-in read tool -- while the *name* has to stay bare so it reaches
    the bus view as the pinned policy spells it: `bus_boundary.normalize_item`
    passes the wire name through unchanged and does not read the namespace.
    """
    item = {
        "type": "function_call",
        "id": f"fc_{response_id}",
        "name": name,
        "namespace": PROJECTION_READ_NAMESPACE,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
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


def projection_reads(body: dict) -> int:
    """How many read calls the transcript in ``body`` already carries.

    The transcript is resent in full on every turn, so counting is a stable way
    to decide which read comes next without keeping state between requests.
    """
    items = body.get("input")
    if not isinstance(items, list):
        return 0
    return sum(
        1
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("name") == PROJECTION_READ_TOOL
    )


def classify(body: dict, script: str) -> str:
    """Return the scripted turn for a request body."""
    if script == "projection":
        if projection_reads(body) < PROJECTION_READS:
            return "projection_read"
        return "projection_final"
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
        script = self.server.script  # type: ignore[attr-defined]
        turn = classify(body, script)
        self._label(index, turn)
        if turn == "parent_after_spawn":
            self._await_child_turn()
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.turns.append(turn)  # type: ignore[attr-defined]
            count = len(self.server.turns)  # type: ignore[attr-defined]
        response_id = f"resp_jev_smoke_{count}"
        if turn == "projection_read":
            position = projection_reads(body)
            payload = read_stream(
                response_id,
                f"call_jev_projection_{position + 1:02d}",
                PROJECTION_READ_TOOL,
                {"path": PROJECTION_PATHS[position]},
            )
        elif turn == "projection_final":
            payload = message_stream(response_id, PROJECTION_FINAL_TEXT)
        else:
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
    parser.add_argument(
        "--script",
        choices=PROJECTION_SCRIPTS,
        default="plaintext",
        help="which response script to serve (default: plaintext)",
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", default=None)
    parser.add_argument("--child-rendezvous-seconds", type=float, default=20.0)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.requests_dir = os.path.abspath(args.requests_dir)  # type: ignore[attr-defined]
    server.script = args.script  # type: ignore[attr-defined]
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
        "script": args.script,
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
