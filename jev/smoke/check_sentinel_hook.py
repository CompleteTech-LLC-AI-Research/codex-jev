#!/usr/bin/env python3
"""Assertions over a recorded `run-sentinel-hook-smoke.sh` run.

The run drives a real Codex host through the wired Sentinel carrier in three
states. This checker reads only artifacts the run left behind - the marker the
blocked action would have created, the host's own JSON log, the carrier's
incident journal, the install record, and the component's own audit rows - and
decides whether the claims hold:

* the host itself invoked the wired command (the incidents carry the host's own
  session identity and join the component's audit rows);
* every stage that fired produced a correlated incident, so coverage is proven
  by activity rather than by a file on disk;
* under enforcement the exact action was prevented *before* execution, and the
  host surfaced the component's own reason;
* under the same switches with no wiring, the action runs and nothing is
  recorded, so the record is caused by the wiring.
* the coverage report calls a wiring activated only when a canary actually
  reached it: an installed-but-unprobed wiring reports `activated: false`, a
  probed one reports `activated: true` with the host's own correlated incidents,
  and a removed one reports `activated: false` again.

Every assertion is named, so a checker that stopped asserting one of them is
visible rather than silently weaker. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "jev" / "scripts"))

import sentinel_boundary as sb  # noqa: E402

#: The component's own message for a non-DEFER finding. Its presence in the host
#: log is what separates "the host surfaced the component's reason" from "the
#: host failed for some unrelated reason".
COMPONENT_VETO_MESSAGE = "JEV Sentinel: content or action requires security review"

STAGES = ("ingress", "tool_before", "tool_after")


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    def failures(self) -> list[tuple[str, str]]:
        return [(name, detail) for name, ok, detail in self.checks if not ok]

    def emit(self) -> int:
        for name, ok, detail in self.checks:
            status = "ok" if ok else "FAIL"
            line = f"{status} {name}"
            if detail and not ok:
                line += f" -- {detail}"
            print(line)
        failures = self.failures()
        print(f"\n{len(self.checks) - len(failures)}/{len(self.checks)} checks passed")
        return 1 if failures else 0


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def marker_state(workdir: Path, mode: str) -> str:
    path = workdir / f"marker-{mode}.state"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else "missing"


def exec_log(workdir: Path, mode: str) -> str:
    path = workdir / f"exec-{mode}.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def incidents(workdir: Path, mode: str) -> list:
    return sb.read_incidents(
        sb.incident_path(workdir / f"home-{mode}" / "sentinel-state")
    )


def audit_rows(component: Path, policy: Path) -> list:
    return sb.outbox(component, policy)


def stages(rows: list) -> set:
    return {row.get("stage") for row in rows if isinstance(row, dict)}


def corr_ok(rows: list, audit: list) -> tuple[bool, str]:
    """Every *decisive* incident joins a component audit row by ref and event id.

    The component's `outbox` lists pending rows only, and pending means
    "decision was not DEFER" — so an observational row is deliberately not
    exposed there. The join is therefore asserted for the incidents that carry a
    decision, which is exactly the set the component offers a row for; every
    incident still has to carry the correlation keys, which `keys_ok` asserts.
    """
    by_ref = {}
    for row in audit:
        if isinstance(row, dict):
            by_ref.setdefault(row.get("session_ref"), set()).add(row.get("id"))
    for row in rows:
        sentinel = row.get("sentinel") or {}
        if sentinel.get("decision") == "DEFER":
            continue
        ref = sentinel.get("session_ref")
        event_id = sentinel.get("event_id")
        if event_id not in by_ref.get(ref, set()):
            return False, f"incident {event_id} does not join an audit row for {ref}"
    return True, ""


def keys_ok(rows: list) -> tuple[bool, str]:
    """Every incident carries the keys an incident must be joinable by."""
    for row in rows:
        sentinel = row.get("sentinel") or {}
        for key in ("session_ref", "event_id", "content_sha256"):
            if not sentinel.get(key):
                return False, f"incident {row.get('event_id')} lacks sentinel.{key}"
        if not row.get("session_id"):
            return False, f"incident {row.get('event_id')} lacks a host session id"
    return True, ""


def decisive(rows: list) -> list:
    return [
        row for row in rows if (row.get("sentinel") or {}).get("decision") != "DEFER"
    ]


def no_raw_content(rows: list, canary: str) -> tuple[bool, str]:
    for row in rows:
        if row.get("redaction") != sb.REDACTION:
            return False, f"incident redaction is {row.get('redaction')!r}"
        blob = json.dumps(row)
        if canary in blob:
            return False, "incident carries the raw action text"
    return True, ""


def check_shadow(workdir: Path, report: Report, audit: list, canary: str) -> None:
    rows = incidents(workdir, "shadow")
    report.check(
        "shadow/action-ran",
        marker_state(workdir, "shadow") == "ran",
        "the observational run did not execute the action",
    )
    report.check(
        "shadow/incident-recorded",
        len(rows) >= 1,
        "the wired carrier recorded no incident for a running session",
    )
    report.check(
        "shadow/multi-stage",
        {"tool_before"} <= stages(rows),
        f"stages recorded: {sorted(stages(rows))}",
    )
    blocked = [
        row
        for row in rows
        if (row.get("sentinel") or {}).get("decision") == "BLOCK"
        and "installation_test_canary"
        in ((row.get("sentinel") or {}).get("reason_codes") or [])
    ]
    report.check(
        "shadow/canary-decision-recorded",
        bool(blocked),
        "no incident carried the component's deterministic canary finding",
    )
    report.check(
        "shadow/observational",
        all(not (row.get("sentinel") or {}).get("enforced") for row in blocked),
        "a shadow run reported an enforced finding",
    )
    ok, detail = corr_ok(rows, audit)
    report.check("shadow/correlated-to-component", ok, detail)
    ok, detail = keys_ok(rows)
    report.check("shadow/correlation-keys", ok, detail)
    report.check(
        "shadow/host-identity",
        bool(rows) and all(row.get("session_id") for row in rows),
        "an incident carried no host session identity",
    )
    ok, detail = no_raw_content(rows, canary)
    report.check("shadow/no-raw-content", ok, detail)


def check_enforce(workdir: Path, report: Report, audit: list, canary: str) -> None:
    rows = incidents(workdir, "enforce")
    log = exec_log(workdir, "enforce")
    report.check(
        "enforce/exact-action-prevented",
        marker_state(workdir, "enforce") == "absent",
        "the enforcing run still executed the action",
    )
    report.check(
        "enforce/veto-before-execution",
        {"tool_before"} <= stages(rows),
        f"stages recorded: {sorted(stages(rows))}",
    )
    report.check(
        "enforce/no-post-tool",
        "tool_after" not in stages(rows),
        "a post-tool stage fired even though the action never ran",
    )
    report.check(
        "enforce/host-surfaced-the-reason",
        COMPONENT_VETO_MESSAGE in log,
        "the host log did not carry the component's own veto message",
    )
    ok, detail = corr_ok(rows, audit)
    report.check("enforce/correlated-to-component", ok, detail)
    ok, detail = keys_ok(rows)
    report.check("enforce/correlation-keys", ok, detail)
    report.check(
        "enforce/decisive-incident-is-the-veto",
        len(decisive(rows)) == 1
        and next(iter(decisive(rows)))["sentinel"]["decision"] == "BLOCK",
        f"decisive incidents: {[row['sentinel']['decision'] for row in decisive(rows)]}",
    )
    report.check(
        "enforce/host-identity",
        bool(rows) and all(row.get("session_id") for row in rows),
        "an incident carried no host session identity",
    )
    ok, detail = no_raw_content(rows, canary)
    report.check("enforce/no-raw-content", ok, detail)


def check_unwired(workdir: Path, report: Report, canary: str) -> None:
    rows = incidents(workdir, "unwired")
    report.check(
        "unwired/action-ran",
        marker_state(workdir, "unwired") == "ran",
        "the unwired control did not execute the action",
    )
    report.check(
        "unwired/nothing-recorded",
        not rows,
        f"{len(rows)} incident(s) recorded without any wiring",
    )


def check_install(workdir: Path, report: Report) -> None:
    for mode in ("shadow", "enforce"):
        path = workdir / f"install-{mode}.json"
        if not report.check(
            f"{mode}/wiring-written", path.is_file(), "no install record was written"
        ):
            continue
        record = read_json(path)
        commands = json.dumps(record)
        state_dir = str(workdir / f"home-{mode}" / "sentinel-state")
        report.check(
            f"{mode}/state-dir-pinned",
            state_dir in commands,
            "the wired command does not pin the state dir the journal is read from",
        )
        hooks = read_json(workdir / f"home-{mode}" / "hooks.json")
        wired = json.dumps(hooks)
        report.check(
            f"{mode}/carrier-is-the-wired-command",
            "sentinel_boundary.py" in wired and " hook " in wired,
            "hooks.json does not run the host carrier's hook subcommand",
        )


def activation(workdir: Path, name: str) -> dict:
    path = workdir / name
    return read_json(path) if path.is_file() else {}


def check_activation(workdir: Path, report: Report) -> None:
    """The coverage report is the boundary's own claim; these are its limits.

    Installation alone is never activation: an installed wiring that no canary
    has travelled must not be reported as activated. The probe is what turns the
    claim on, and it may only do so when the host itself recorded the canary -
    the report is corroborated against the journal the host's own run wrote, not
    against the component's self-report.
    """
    installed = (
        activation(workdir, "coverage-activation-installed.json").get("activation")
    ) or {}
    report.check(
        "activation/installation-is-not-activation",
        installed.get("activated") is False and installed.get("basis") == "none",
        f"an installed but unprobed wiring reported {installed}",
    )

    probed = activation(workdir, "coverage-activation-probed.json")
    pact = probed.get("activation") or {}
    report.check(
        "activation/probe-activates-a-live-carrier",
        pact.get("activated") is True and pact.get("basis") == "probe_canary",
        f"probing a wired carrier did not activate it: {pact}",
    )
    evidence = pact.get("evidence") or {}
    report.check(
        "activation/probe-corroborated-by-the-host",
        bool(evidence)
        and all(
            entry.get("host_carrier") is True
            and (entry.get("host_incidents") or 0) >= 1
            for entry in evidence.values()
        ),
        f"a probed stage was not corroborated by a host incident: {evidence}",
    )
    # The probe names the host incidents it matched; they must be rows in the
    # journal this home holds, or the "correlation" is only claimed.
    journaled = {row.get("event_id") for row in incidents(workdir, "activation")}
    referenced = {
        event_id
        for entry in evidence.values()
        for event_id in (entry.get("host_incident_event_ids") or [])
    }
    report.check(
        "activation/probe-incidents-are-journaled",
        bool(referenced) and referenced <= journaled,
        f"probe referenced incidents not in the journal: {sorted(referenced - journaled)}",
    )

    removed = activation(workdir, "coverage-activation-removed.json")
    ract = removed.get("activation") or {}
    report.check(
        "activation/removal-deactivates",
        ract.get("activated") is False and not removed.get("wired_stages"),
        f"a removed carrier still reported activation: {ract}",
    )
    hooks = read_json(workdir / "home-activation" / "hooks.json")
    report.check(
        "activation/removal-removes-the-carrier",
        "sentinel_boundary.py" not in json.dumps(hooks),
        "hooks.json still runs the carrier after --remove",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--profile", default="codex-jev-sentinel-e2e")
    parser.add_argument("--canary", default="JEV_SENTINEL_TEST_BLOCK")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    component = Path(args.component)

    report = Report()
    # Each mode owns an isolated home, and the component keys its audit store by
    # the policy's own directory, so the component's rows are gathered from every
    # mode rather than from one of them.
    audit = []
    for mode in ("shadow", "enforce"):
        policy = workdir / f"home-{mode}" / "sentinel-policy.json"
        if policy.is_file():
            audit.extend(audit_rows(component, policy))
    report.check(
        "component/audit-rows-present",
        len(audit) >= 1,
        "the component recorded no audit row for any wired invocation",
    )
    check_install(workdir, report)
    check_shadow(workdir, report, audit, args.canary)
    check_enforce(workdir, report, audit, args.canary)
    check_unwired(workdir, report, args.canary)
    check_activation(workdir, report)
    return report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
