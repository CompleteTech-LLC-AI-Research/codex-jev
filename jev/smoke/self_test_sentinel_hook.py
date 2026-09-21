#!/usr/bin/env python3
"""Self-test for the Sentinel hook smoke fixture.

Builds synthetic recorded runs, then asserts that `check_sentinel_hook.py`
accepts the honest shape and *rejects* each way the evidence could be hollow. A
checker that cannot fail is not evidence, so the negative controls live beside
the fixture itself.

The run is synthetic on purpose: this file has to run in CI, where no `codex`
binary is built. It therefore proves the *checker*, not the host. The host is
proven by `run-sentinel-hook-smoke.sh` against a real binary, and the component
here is a stub that prints rows, so this tier is `component-stub`: it says
nothing about the pinned component's behaviour.

Standard library only. Writes to TMPDIR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "check_sentinel_hook.py")

CANARY = "JEV_SENTINEL_TEST_BLOCK"
COMPONENT_MESSAGE = "JEV Sentinel: content or action requires security review"
SESSION = "01a0c54e-e450-79d0-8b96-35e9ddbd788d"
MODE_REF = {
    "shadow": "9f005be38d2ffd142a05739479d2445c4d8071a1ccf1cce4f2a20ad6d59f93bd",
    "enforce": "7e0ada89d415d95c9387b5314f80c8e8e2e85daed6b1b83fb998496b354308f3",
}

#: A stub component: the checker's only component read is `outbox`, so the stub
#: answers that subcommand from a file the test writes.
STUB_LAUNCH = """\
import json
import os
import sys

rows = json.load(open(os.path.join(os.path.dirname(__file__), "rows.json")))
if len(sys.argv) > 1 and sys.argv[1] == "outbox":
    sys.stdout.write(json.dumps(rows))
else:
    sys.stdout.write("{}")
"""


def incident(
    mode: str, stage: str, decision: str, enforced: bool, event_id: str
) -> dict:
    sentinel = {
        "event_id": event_id,
        "decision": decision,
        "enforced": enforced,
        "reason_codes": ["installation_test_canary"] if decision != "DEFER" else [],
        "route": "normal" if decision == "DEFER" else "administrator",
        "backend": "local",
        "session_ref": MODE_REF[mode],
        "content_sha256": "a" * 64,
        "action_sha256": "b" * 64,
        "content_bytes": 32,
        "tool_name": "exec_command",
        "harness": "codex",
        "profile": "codex-jev-sentinel-e2e",
    }
    return {
        "event_id": "e" * 32,
        "session_id": SESSION,
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
        "component": "jev-sentinel",
        "stage": stage,
        "kind": "sentinel_incident",
        "redaction": "content_sha256_only",
        "native_event": {
            "ingress": "UserPromptSubmit",
            "tool_before": "PreToolUse",
            "tool_after": "PostToolUse",
        }[stage],
        "outcome": "observed",
        "sentinel": sentinel,
    }


def build(root: str) -> str:
    """Write an honest three-mode run and return its directory."""
    component = os.path.join(root, "component")
    os.makedirs(component, exist_ok=True)
    with open(os.path.join(component, "launch.py"), "w") as handle:
        handle.write(STUB_LAUNCH)
    with open(os.path.join(component, "rows.json"), "w") as handle:
        # One row per decisive incident the fixture records: the component lists
        # pending rows, and pending means the decision was not DEFER.
        json.dump(
            [
                {
                    "id": "e6f58929247241f2bf606215d4dc982e",
                    "decision": "BLOCK",
                    "enforced": False,
                    "reason_codes": ["installation_test_canary"],
                    "route": "administrator",
                    "backend": "local",
                    "harness": "codex",
                    "stage": "ingress",
                    "session_ref": MODE_REF["shadow"],
                },
                {
                    "id": "5c762d532b5c4a4592a3d90a569f7e0b",
                    "decision": "BLOCK",
                    "enforced": False,
                    "reason_codes": ["installation_test_canary"],
                    "route": "administrator",
                    "backend": "local",
                    "harness": "codex",
                    "stage": "tool_before",
                    "session_ref": MODE_REF["shadow"],
                },
                {
                    "id": "52365ab20c4a42ccb5c837628dcee3cf",
                    "decision": "BLOCK",
                    "enforced": False,
                    "reason_codes": ["installation_test_canary"],
                    "route": "administrator",
                    "backend": "local",
                    "harness": "codex",
                    "stage": "tool_after",
                    "session_ref": MODE_REF["shadow"],
                },
                {
                    "id": "9742b1c7e54e4f9e8c61ac6b260c7cbf",
                    "decision": "BLOCK",
                    "enforced": True,
                    "reason_codes": ["installation_test_canary"],
                    "route": "administrator",
                    "backend": "local",
                    "harness": "codex",
                    "stage": "tool_before",
                    "session_ref": MODE_REF["enforce"],
                },
            ],
            handle,
        )

    modes = {
        "shadow": (
            "ran",
            [
                incident(
                    "shadow",
                    "ingress",
                    "BLOCK",
                    False,
                    "e6f58929247241f2bf606215d4dc982e",
                ),
                incident(
                    "shadow",
                    "tool_before",
                    "BLOCK",
                    False,
                    "5c762d532b5c4a4592a3d90a569f7e0b",
                ),
                incident(
                    "shadow",
                    "tool_after",
                    "BLOCK",
                    False,
                    "52365ab20c4a42ccb5c837628dcee3cf",
                ),
            ],
        ),
        "enforce": (
            "absent",
            [
                incident(
                    "enforce",
                    "ingress",
                    "DEFER",
                    False,
                    "1be2150da6da47e4b6ab556c702ea3dd",
                ),
                incident(
                    "enforce",
                    "tool_before",
                    "BLOCK",
                    True,
                    "9742b1c7e54e4f9e8c61ac6b260c7cbf",
                ),
            ],
        ),
        "unwired": ("ran", []),
    }
    for mode, (marker, rows) in modes.items():
        home = os.path.join(root, f"home-{mode}")
        state = os.path.join(home, "sentinel-state")
        os.makedirs(state, exist_ok=True)
        if rows:
            with open(os.path.join(state, "codex-jev-incidents.jsonl"), "w") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
        with open(os.path.join(root, f"marker-{mode}.state"), "w") as handle:
            handle.write(marker + "\n")
        with open(os.path.join(root, f"exec-{mode}.log"), "w") as handle:
            handle.write(
                "Command blocked by PreToolUse hook: " + COMPONENT_MESSAGE + "\n"
                if mode == "enforce"
                else "command completed\n"
            )
        if mode != "unwired":
            command = (
                f"python3 -I {os.path.join(root, 'sentinel_boundary.py')} hook --harness codex "
                f"--event PreToolUse --profile codex-jev-sentinel-e2e "
                f"--policy {home}/sentinel-policy.json --state-dir {state} "
                f"--component {component}"
            )
            # The checker reads the component's rows through the policy it ran
            # under, so the fixture has to have one.
            with open(os.path.join(home, "sentinel-policy.json"), "w") as handle:
                json.dump(
                    {"schema_version": 1, "mode": "enforce", "backend": "local"}, handle
                )
            with open(os.path.join(home, "hooks.json"), "w") as handle:
                json.dump(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": ".*",
                                    "hooks": [{"type": "command", "command": command}],
                                }
                            ]
                        }
                    },
                    handle,
                )
            with open(os.path.join(root, f"install-{mode}.json"), "w") as handle:
                json.dump(
                    {
                        "state_dir": state,
                        "events": {"PreToolUse": {"command": command}},
                    },
                    handle,
                )
    write_activation(root, component)
    return component


#: The probe's reported host incidents, one per stage, and the journal rows they
#: must match. The ids are the probe's own output shape, so the fixture exercises
#: the join the checker asserts rather than a shape the checker invented.
ACTIVATION_PROBE = {
    "ingress": "1a" * 32,
    "tool_before": "2b" * 32,
    "tool_after": "3c" * 32,
}


def write_activation(root: str, component: str) -> None:
    """Write the fourth mode: installed is not activation, probed is, removed is not."""
    home = os.path.join(root, "home-activation")
    state = os.path.join(home, "sentinel-state")
    os.makedirs(state, exist_ok=True)
    policy = os.path.join(home, "sentinel-policy.json")
    with open(policy, "w") as handle:
        json.dump({"schema_version": 1, "mode": "enforce", "backend": "local"}, handle)
    with open(os.path.join(state, "codex-jev-incidents.jsonl"), "w") as handle:
        for stage, event_id in ACTIVATION_PROBE.items():
            row = incident("shadow", stage, "BLOCK", True, event_id)
            row["event_id"] = event_id
            handle.write(json.dumps(row) + "\n")
    with open(os.path.join(root, "install-activation.json"), "w") as handle:
        json.dump(
            {
                "state_dir": state,
                "carrier": component,
                "events": {
                    "UserPromptSubmit": {
                        "command": f"python3 -I {component}/sentinel_boundary.py hook"
                    },
                },
            },
            handle,
        )
    with open(os.path.join(root, "remove-activation.json"), "w") as handle:
        json.dump({"installed": False, "removed": 3, "enabled": False}, handle)
    # The post-removal home: the carrier is gone from hooks.json.
    with open(os.path.join(home, "hooks.json"), "w") as handle:
        json.dump({"hooks": {}}, handle)
    write_json(
        os.path.join(root, "coverage-activation-installed.json"),
        {"activation": {"activated": False, "basis": "none"}},
    )
    write_json(
        os.path.join(root, "coverage-activation-probed.json"),
        {
            "activation": {
                "activated": True,
                "basis": "probe_canary",
                "evidence": {
                    stage: {
                        "probed": True,
                        "host_carrier": True,
                        "host_incidents": 1,
                        "host_incident_event_ids": [event_id],
                    }
                    for stage, event_id in ACTIVATION_PROBE.items()
                },
            }
        },
    )
    write_json(
        os.path.join(root, "coverage-activation-removed.json"),
        {"activation": {"activated": False, "basis": "none"}, "wired_stages": []},
    )
    with open(os.path.join(root, "coverage-activation-removed.status"), "w") as handle:
        handle.write("1\n")


def run_checker(root: str, component: str) -> tuple[int, str]:
    result = subprocess.run(
        [
            sys.executable,
            CHECKER,
            "--workdir",
            root,
            "--component",
            component,
            "--canary",
            CANARY,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def expect(name: str, condition: bool, detail: str, failures: list) -> None:
    if not condition:
        failures.append(f"{name}: {detail}")


def main() -> int:
    failures: list[str] = []
    root = tempfile.mkdtemp(prefix="jev-sentinel-selftest-")
    try:
        component = build(root)
        code, output = run_checker(root, component)
        expect(
            "honest-run-accepted",
            code == 0,
            f"checker rejected the honest shape (exit {code})\n{output}",
            failures,
        )

        # Each mutation is one way the same run could look right while proving
        # nothing, and each has to make the checker fail its own named check.
        mutations = [
            (
                "enforce-executed-anyway",
                lambda: write_marker(root, "enforce", "ran"),
                "enforce/exact-action-prevented",
            ),
            (
                "enforce-quiet-host",
                lambda: write_log(root, "enforce", "command completed\n"),
                "enforce/host-surfaced-the-reason",
            ),
            (
                "enforce-post-tool-fired",
                lambda: append_incident(
                    root,
                    "enforce",
                    incident("enforce", "tool_after", "BLOCK", True, "a" * 32),
                ),
                "enforce/no-post-tool",
            ),
            (
                "shadow-enforced",
                lambda: rewrite_incidents(
                    root,
                    "shadow",
                    [
                        incident(
                            "shadow",
                            "tool_before",
                            "BLOCK",
                            True,
                            "5c762d532b5c4a4592a3d90a569f7e0b",
                        ),
                    ],
                ),
                "shadow/observational",
            ),
            (
                "shadow-no-incident",
                lambda: rewrite_incidents(root, "shadow", []),
                "shadow/incident-recorded",
            ),
            (
                "incident-carries-raw-text",
                lambda: append_incident(
                    root,
                    "shadow",
                    dict(
                        incident(
                            "shadow",
                            "ingress",
                            "BLOCK",
                            False,
                            "e6f58929247241f2bf606215d4dc982e",
                        ),
                        leaked=CANARY,
                    ),
                ),
                "shadow/no-raw-content",
            ),
            (
                "incident-not-joinable",
                lambda: rewrite_incidents(
                    root,
                    "shadow",
                    [
                        incident("shadow", "tool_before", "BLOCK", False, "f" * 32),
                    ],
                ),
                "shadow/correlated-to-component",
            ),
            (
                "incident-without-keys",
                lambda: rewrite_incidents(
                    root,
                    "enforce",
                    [
                        {
                            **incident(
                                "enforce",
                                "tool_before",
                                "BLOCK",
                                True,
                                "9742b1c7e54e4f9e8c61ac6b260c7cbf",
                            ),
                            "sentinel": {
                                **incident(
                                    "enforce", "tool_before", "BLOCK", True, "9" * 32
                                )["sentinel"],
                                "session_ref": "",
                            },
                        }
                    ],
                ),
                "enforce/correlation-keys",
            ),
            (
                "unwired-recorded-anyway",
                lambda: append_incident(
                    root,
                    "unwired",
                    incident("shadow", "tool_before", "BLOCK", False, "1" * 32),
                ),
                "unwired/nothing-recorded",
            ),
            (
                "wiring-not-pinned",
                lambda: unpin_state_dir(root, "shadow"),
                "shadow/state-dir-pinned",
            ),
            (
                "installed-claims-activation",
                lambda: write_json(
                    os.path.join(root, "coverage-activation-installed.json"),
                    {"activation": {"activated": True, "basis": "probe_canary"}},
                ),
                "activation/installation-is-not-activation",
            ),
            (
                "probe-does-not-activate",
                lambda: write_json(
                    os.path.join(root, "coverage-activation-probed.json"),
                    {"activation": {"activated": False, "basis": "none"}},
                ),
                "activation/probe-activates-a-live-carrier",
            ),
            (
                "probe-unconfirmed-by-host",
                lambda: write_json(
                    os.path.join(root, "coverage-activation-probed.json"),
                    {
                        "activation": {
                            "activated": True,
                            "basis": "probe_canary",
                            "evidence": {
                                "ingress": {"host_carrier": False, "host_incidents": 0}
                            },
                        }
                    },
                ),
                "activation/probe-corroborated-by-the-host",
            ),
            (
                "probe-incident-not-journaled",
                lambda: write_json(
                    os.path.join(root, "coverage-activation-probed.json"),
                    {
                        "activation": {
                            "activated": True,
                            "basis": "probe_canary",
                            "evidence": {
                                "ingress": {
                                    "host_carrier": True,
                                    "host_incidents": 1,
                                    "host_incident_event_ids": ["f" * 64],
                                }
                            },
                        }
                    },
                ),
                "activation/probe-incidents-are-journaled",
            ),
            (
                "removed-still-activated",
                lambda: write_json(
                    os.path.join(root, "coverage-activation-removed.json"),
                    {
                        "activation": {"activated": True, "basis": "probe_canary"},
                        "wired_stages": ["ingress"],
                    },
                ),
                "activation/removal-deactivates",
            ),
            (
                "removal-left-the-carrier",
                lambda: write_json(
                    os.path.join(root, "home-activation", "hooks.json"),
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 -I /x/sentinel_boundary.py hook --event PreToolUse",
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                ),
                "activation/removal-removes-the-carrier",
            ),
        ]

        for name, mutate, expected in mutations:
            snapshot = snapshot_of(root)
            try:
                mutate()
                code, output = run_checker(root, component)
                expect(
                    f"rejects-{name}",
                    code != 0 and f"FAIL {expected}" in output,
                    f"checker did not fail {expected} (exit {code})\n{output}",
                    failures,
                )
            finally:
                restore(root, snapshot)

        # The honest run must still be accepted after every control is undone,
        # so a rejected control cannot be an accident of a dirty fixture.
        code, output = run_checker(root, component)
        expect(
            "honest-run-accepted-again",
            code == 0,
            f"checker rejected the restored honest shape (exit {code})\n{output}",
            failures,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print("self-test failures:")
        for failure in failures:
            print("  - " + failure)
        return 1
    print("sentinel hook smoke self-test: all controls behaved")
    return 0


# ---------------------------------------------------------------- mutations


def write_marker(root: str, mode: str, value: str) -> None:
    with open(os.path.join(root, f"marker-{mode}.state"), "w") as handle:
        handle.write(value + "\n")


def write_log(root: str, mode: str, text: str) -> None:
    with open(os.path.join(root, f"exec-{mode}.log"), "w") as handle:
        handle.write(text)


def write_json(path: str, obj) -> None:
    with open(path, "w") as handle:
        json.dump(obj, handle)


def journal_path(root: str, mode: str) -> str:
    return os.path.join(
        root, f"home-{mode}", "sentinel-state", "codex-jev-incidents.jsonl"
    )


def rewrite_incidents(root: str, mode: str, rows: list) -> None:
    path = journal_path(root, mode)
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def append_incident(root: str, mode: str, row: dict) -> None:
    with open(journal_path(root, mode), "a") as handle:
        handle.write(json.dumps(row) + "\n")


def unpin_state_dir(root: str, mode: str) -> None:
    """Rewrite the wiring so the command no longer names the journal's directory."""
    home = os.path.join(root, f"home-{mode}")
    hooks = json.load(open(os.path.join(home, "hooks.json")))
    for entries in hooks.get("hooks", {}).values():
        for entry in entries:
            for handler in entry.get("hooks", []):
                handler["command"] = handler["command"].replace(
                    f"--state-dir {home}/sentinel-state", "--state-dir /elsewhere"
                )
    with open(os.path.join(home, "hooks.json"), "w") as handle:
        json.dump(hooks, handle)
    with open(os.path.join(root, f"install-{mode}.json"), "w") as handle:
        json.dump({"state_dir": "/elsewhere", "events": hooks.get("hooks", {})}, handle)


def snapshot_of(root: str) -> dict:
    snapshot = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                snapshot[path] = handle.read()
    return snapshot


def restore(root: str, snapshot: dict) -> None:
    # A mutation may have created a file the honest run never had (an appended
    # journal, for instance), so the restore also removes what it did not start
    # with; otherwise one control would leak into the next.
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            if path not in snapshot:
                os.remove(path)
    for path, blob in snapshot.items():
        with open(path, "wb") as handle:
            handle.write(blob)


if __name__ == "__main__":
    raise SystemExit(main())
