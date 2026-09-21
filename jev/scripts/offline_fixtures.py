#!/usr/bin/env python3
"""Deterministic offline Responses-API fixtures for the isolated JEV profile.

The fixtures in ``jev/fixtures`` are labelled, checked-in, and byte-stable: the
same request always produces the same response, with no clock, no randomness,
and no network. They stand in for a provider so the pinned build can be
exercised without remote inference.

Exit codes: 0 = ok, 1 = fixture or request error, 2 = usage error.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE_VERSION = 1
FIXTURE_TIER = "offline-fixture"
DEFAULT_MAX_MATCHES = 1
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
RESPONSE_PATHS = ("/responses", "/v1/responses")


class FixtureError(Exception):
    """A fixture on disk cannot be used."""


def load_fixtures(fixture_dir=DEFAULT_FIXTURE_DIR):
    """Load and validate every fixture, ordered by priority then file name."""
    fixture_dir = Path(fixture_dir)
    if not fixture_dir.is_dir():
        raise FixtureError(f"fixture directory not found: {fixture_dir}")
    fixtures = []
    for path in sorted(fixture_dir.glob("*.json")):
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise FixtureError(f"{path.name} is not valid JSON: {error}") from error
        for key in ("fixture_version", "id", "tier", "priority", "match", "events"):
            if key not in fixture:
                raise FixtureError(f"{path.name} is missing {key!r}")
        if fixture["fixture_version"] != FIXTURE_VERSION:
            raise FixtureError(
                f"{path.name} has fixture_version {fixture['fixture_version']!r}, "
                f"expected {FIXTURE_VERSION}"
            )
        if fixture["tier"] != FIXTURE_TIER:
            raise FixtureError(
                f"{path.name} must declare tier {FIXTURE_TIER!r} so no fixture is "
                "mistaken for live evidence"
            )
        if not isinstance(fixture["events"], list) or not fixture["events"]:
            raise FixtureError(f"{path.name} must carry at least one event")
        # A fixture that answers every request (the empty-match fallback) would
        # never let a conversation finish, so it is unlimited while a targeted
        # fixture answers a bounded number of times before falling through.
        bound = fixture.get("max_matches")
        if bound is None:
            bound = None if not fixture["match"] else DEFAULT_MAX_MATCHES
        if bound is not None and (not isinstance(bound, int) or bound < 1):
            raise FixtureError(f"{path.name} has a non-positive max_matches {bound!r}")
        fixture["max_matches"] = bound
        fixture["path"] = path.name
        fixtures.append(fixture)
    if not fixtures:
        raise FixtureError(f"no fixtures in {fixture_dir}")
    fixtures.sort(key=lambda fixture: (fixture["priority"], fixture["path"]))
    return fixtures


def select_fixture(fixtures, body, served=None):
    """Return the first fixture whose match clause accepts ``body``.

    ``served`` maps fixture id to the number of times it has answered already,
    so a bounded fixture stops matching once its budget is spent and the
    request falls through to the next fixture (ultimately the fallback).
    """
    served = served or {}
    text = body if isinstance(body, str) else json.dumps(body, sort_keys=True)
    for fixture in fixtures:
        bound = fixture.get("max_matches")
        if bound is not None and served.get(fixture["id"], 0) >= bound:
            continue
        match = fixture.get("match") or {}
        needed = match.get("body_contains")
        if needed is not None and needed not in text:
            continue
        return fixture
    return None


def render_sse(fixture):
    """Render a fixture as the SSE bytes the Responses API client expects."""
    out = []
    for event in fixture["events"]:
        kind = event.get("type")
        out.append(f"event: {kind}\n")
        if len(event) == 1:
            out.append("\n")
        else:
            out.append(f"data: {json.dumps(event, sort_keys=True)}\n\n")
    return "".join(out)


def fixture_catalog(fixtures):
    """A stable description of the catalog, suitable for evidence artifacts."""
    return [
        {
            "id": fixture["id"],
            "path": fixture["path"],
            "tier": fixture["tier"],
            "priority": fixture["priority"],
            "max_matches": fixture.get("max_matches"),
            "match": fixture.get("match") or {},
            "event_types": [event.get("type") for event in fixture["events"]],
        }
        for fixture in fixtures
    ]


class FixtureHandler(BaseHTTPRequestHandler):
    """Serve the fixture catalog; record every request for evidence."""

    protocol_version = "HTTP/1.1"
    server_version = "jev-offline-fixtures"

    def log_message(self, fmt, *args):  # keep stderr clean; requests are recorded
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._json({"status": "ok", "fixtures": len(self.server.fixtures)})
            return
        if self.path == "/fixtures":
            self._json({"fixtures": fixture_catalog(self.server.fixtures)})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?")[0] not in RESPONSE_PATHS:
            self._json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fixture = select_fixture(self.server.fixtures, body, self.server.served)
        self.server.requests.append(
            {"path": self.path, "fixture": fixture["id"] if fixture else None}
        )
        if fixture is None:
            self._json(
                {"error": "no fixture matched", "tier": FIXTURE_TIER}, status=400
            )
            return
        self.server.served[fixture["id"]] = self.server.served.get(fixture["id"], 0) + 1
        payload = render_sse(fixture).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_server(fixtures, host="127.0.0.1", port=0):
    """Create a loopback-only fixture server; port 0 picks a free port."""
    server = ThreadingHTTPServer((host, port), FixtureHandler)
    server.fixtures = fixtures
    server.requests = []
    server.served = {}
    return server


def serve(fixtures, host, port, port_file=None):
    server = build_server(fixtures, host=host, port=port)
    envelope = {
        "host": server.server_address[0],
        "port": server.server_address[1],
        "base_url": f"http://{server.server_address[0]}:{server.server_address[1]}/v1",
        "fixtures": fixture_catalog(fixtures),
    }
    if port_file:
        Path(port_file).write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(envelope, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURE_DIR))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file", default=None)
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the catalog and exit instead of serving.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        fixtures = load_fixtures(args.fixtures)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.list:
        print(
            json.dumps(
                {"fixtures": fixture_catalog(fixtures)}, indent=2, sort_keys=True
            )
        )
        return 0
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "error: fixtures are loopback-only; refusing to bind to a routable host",
            file=sys.stderr,
        )
        return 2
    return serve(fixtures, args.host, args.port, args.port_file)


if __name__ == "__main__":
    sys.exit(main())
