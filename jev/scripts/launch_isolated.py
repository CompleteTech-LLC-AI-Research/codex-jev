#!/usr/bin/env python3
"""Run the pinned binary against the isolated home and the offline fixtures.

The fixture service is loopback-only and starts on the port recorded in the
environment plan. Every request and every invocation is written under
``<env>/logs`` so a run can be replayed and audited without live inference.

Exit codes: the binary's own exit code, 1 = environment failure, 2 = usage.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import isolated_env
import offline_fixtures

HEALTH_TIMEOUT_SECONDS = 10.0


def wait_for_health(port, timeout=HEALTH_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    return False


def start_fixture_service(fixtures, port):
    server = offline_fixtures.build_server(fixtures, port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not wait_for_health(server.server_address[1]):
        server.shutdown()
        raise isolated_env.EnvError("offline fixture service did not become healthy")
    return server


def run(env_dir, passthrough, dry_run=False, port_override=None, timeout=None):
    env_dir = Path(env_dir)
    plan = isolated_env.read_env(env_dir)
    fixtures = offline_fixtures.load_fixtures(
        isolated_env.repository_root() / "jev" / "fixtures"
    )
    port = port_override or plan["fixture_port"]
    server = start_fixture_service(fixtures, port)
    binary = Path(plan["binary"])
    command = [str(binary), *passthrough]
    try:
        if dry_run:
            return {
                "dry_run": True,
                "binary": str(binary),
                "binary_present": binary.is_file(),
                "command": command,
                "codex_home": plan["home"],
                "fixture_base_url": f"http://127.0.0.1:{port}/v1",
                "features": plan["features"],
                "switch_env": isolated_env.switch_env(plan),
                "bus_env": isolated_env.bus_env(plan),
            }
        if not binary.is_file():
            raise isolated_env.EnvError(
                f"pinned binary is missing: {binary}; build it first "
                "(jev/scripts/build_provenance.py build)"
            )
        child_env = dict(os.environ)
        child_env["CODEX_HOME"] = plan["home"]
        child_env["JEV_ISOLATED_ENV"] = "1"
        child_env["JEV_FIXTURE_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
        child_env.update(isolated_env.switch_env(plan))
        # The projection boundary is resolved by the host from this contract;
        # the stage commands come from the environment so that a stage without
        # a command is never registered.
        child_env.update(isolated_env.bus_env(plan))
        started = time.time()
        completed = subprocess.run(command, env=child_env, timeout=timeout, check=False)
        record = {
            "command": command,
            "exit_code": completed.returncode,
            "duration_ms": int((time.time() - started) * 1000),
            "codex_home": plan["home"],
            "profile": plan["profile"],
            "host_commit": plan["host_commit"],
            "binary_sha256": plan.get("binary_sha256"),
            "fixture_requests": list(server.requests),
            "tier": "offline-fixture + real-host-binary",
        }
        logs = env_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "fixture-requests.json").write_text(
            json.dumps(record["fixture_requests"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (logs / "invocations.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record
    finally:
        server.shutdown()
        server.server_close()


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-dir", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved launch without executing the binary.",
    )
    parser.add_argument("passthrough", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    passthrough = [item for item in args.passthrough if item != "--"]
    env_dir = args.env_dir or isolated_env.default_env_dir()
    try:
        result = run(
            env_dir,
            passthrough,
            dry_run=args.dry_run,
            port_override=args.port,
            timeout=args.timeout,
        )
    except isolated_env.EnvError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(json.dumps(result, sort_keys=True), file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
