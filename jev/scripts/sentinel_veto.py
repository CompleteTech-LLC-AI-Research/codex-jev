#!/usr/bin/env python3
"""Host veto precedence, the subsequent-action latch, and concurrent-action handling.

Phase 4, issue #19. The pinned ``jev-sentinel`` component owns detection: it
decides ``DEFER``/``REVIEW``/``BLOCK``/``QUARANTINE`` and keeps its own session
taint latch in its audit store. This module owns only what a *host* must own:

* **Mapping.** An enforced decision becomes the one veto the platform can
  express for that stage. ``REVIEW``, ``BLOCK``, and ``QUARANTINE`` all veto
  under enforcement - the component's own ``render`` vetoes on any enforced
  non-``DEFER`` - and their severity is ordered so the strongest finding wins.
* **Precedence.** ``QUARANTINE`` > ``BLOCK`` > ``REVIEW`` > ``DEFER``. A veto
  that has been latched for a session can never be downgraded: a later approval,
  a later ``DEFER``, or a lower-severity finding cannot clear it.
* **The subsequent-action latch.** Once a session has been vetoed, every later
  event in that session is at least as severe, so the action that follows a veto
  is prevented *before execution* even though its own finding defers. This
  mirrors the component's taint latch, which also survives a new user turn.
* **Concurrency.** Evaluations of one session are serialized (an in-process lock
  plus a POSIX file lock), so the latch is never read-modify-written from stale
  state and two parallel calls cannot both be the one that "wins".
* **Fail closed.** A timeout, a malformed component response, a cancellation, or
  a policy-shape failure becomes the component's own ``failure_result`` shape -
  ``REVIEW``, ``enforced``, ``route=security_review``, ``backend=unavailable`` -
  and latches the session. A failure never turns into an allow.

Every verdict still comes from the component: this module calls
``sentinel_boundary.observe`` and never re-derives a decision. What it adds is
the host's effective decision (``max`` of this event and the session latch) and
the response the harness reads.

Identity is the component's own session key,
``sha256(canonical([harness, profile, session_id]))``. With no ``session_id``
there is no key, so - exactly like the component - nothing can latch and the
veto is single-shot. That asymmetry is deliberate and documented.

Subcommands:

``enforce``  normalize, evaluate, apply precedence, and print the host response;
             ``--json`` prints the operator envelope instead;
``latch``    list the latch ledger and the effective state of each session;
``clear``    clear one session latch (``--session-ref`` plus ``--confirm``).

Exit codes: 0 ok, 1 refused or incomplete, 2 usage error. ``enforce`` exits 0
even when it vetoes: the response *is* the contract, and the caller must inspect
it rather than the exit code (the component's ``hook`` behaves the same way).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402

try:  # POSIX file locking; the host runs on Linux/WSL.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None

HARNESS = adapter.HARNESS
LATCH_KIND = "sentinel_veto_latch"
LATCH_SCHEMA = "jev-sentinel.veto-latch.v1"
LATCH_FILENAME = "codex-jev-veto-latch.jsonl"
LOCK_DIRNAME = "locks"

# The component's decision lattice, ordered by severity. A veto is any enforced
# decision above DEFER; the rank decides which veto survives.
SEVERITY = {"DEFER": 0, "REVIEW": 1, "BLOCK": 2, "QUARANTINE": 3}
LADDER = {rank: decision for decision, rank in SEVERITY.items()}
VETO_DECISIONS = ("REVIEW", "BLOCK", "QUARANTINE")

# The fail-closed shape, copied from the component's ``core.failure_result``.
FAILURE_ROUTE = "security_review"
FAILURE_BACKEND = "unavailable"
FAILURE_MESSAGE = (
    "JEV Sentinel requires security review; the assessment did not complete."
)

# The latch ledger is bounded. Replaying a bounded tail is *lossless*: the
# state is a fold whose value is "the strongest escalation since the last clear",
# so a suffix that starts at or after the last clear reproduces it exactly.
MAX_LATCH_ROWS = 4096

E_CONFIRM = "E_CONFIRM"
E_SESSION_REF = "E_SESSION_REF"
E_LATCH_SHAPE = "E_LATCH_SHAPE"

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_policy() -> Path:
    env = os.environ.get("JEV_SENTINEL_POLICY")
    return Path(env) if env else Path.home() / ".jev-sentinel" / "policy.json"


class VetoError(ValueError):
    """A bounded, non-sensitive refusal. Its message never contains context text."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------- the lattice


def rank(decision) -> int:
    """Severity of a decision; unknown decisions rank as DEFER (never a veto)."""
    return SEVERITY.get(decision, 0)


def decision_for(value: int) -> str:
    return LADDER.get(value, "DEFER")


def is_veto(verdict) -> bool:
    """The component's own veto predicate: enforced, and not DEFER."""
    return (
        bool(verdict)
        and bool(verdict.get("enforced"))
        and verdict.get("decision") != "DEFER"
    )


def failure_verdict(code: str, enforced: bool = True) -> dict:
    """Mirror of the component's ``core.failure_result`` fail-closed verdict."""
    return {
        "id": hashlib.sha256(f"host-failure:{code}".encode("utf-8")).hexdigest()[:32],
        "decision": "REVIEW",
        "enforced": bool(enforced),
        "reason_codes": [code],
        "route": FAILURE_ROUTE,
        "backend": FAILURE_BACKEND,
        "session_ref": "",
        "message": FAILURE_MESSAGE,
    }


def enforcement_enabled(policy: dict, switches: dict) -> bool:
    """A veto needs the enforcing policy *and* both declared switches.

    Enforcement follows the switch, never the policy alone: ``mode=enforce``
    under an off switch does not veto, and ``sentinel.enforcement`` requires
    ``sentinel.shadow`` (the manifest declares that ordering).
    """
    return (
        policy.get("mode") == "enforce"
        and bool(switches.get(sb.SHADOW_SWITCH))
        and bool(switches.get(sb.ENFORCE_SWITCH))
    )


# ---------------------------------------------------------------- the latch


def latch_path(state_dir) -> Path:
    return Path(state_dir) / LATCH_FILENAME


def read_latch(path) -> list:
    """Read the latch ledger tail, in append order."""
    path = Path(path)
    if not path.is_file():
        return []
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if len(lines) > MAX_LATCH_ROWS:
        lines = lines[-MAX_LATCH_ROWS:]
    rows = []
    for line in lines:
        try:
            row = adapter.strict_json(line)
        except adapter.AdapterError as exc:
            raise VetoError(
                E_LATCH_SHAPE, f"latch row is not valid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict) or row.get("kind") != LATCH_KIND:
            raise VetoError(E_LATCH_SHAPE, "latch ledger carries a foreign row")
        rows.append(row)
    return rows


def latch_row(
    *,
    session_ref: str,
    action: str,
    decision: str,
    source: str,
    stage: str = "",
    native_event: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    reason_codes=(),
    event_id: str = "",
    incident_event_id: str = "",
    failure_code: str = "",
    enforced: bool = True,
    occurred_at_ms=None,
) -> dict:
    """One append-only latch row: an escalation or an operator clear."""
    body = {
        "session_ref": session_ref,
        "action": action,
        "decision": decision,
        "rank": rank(decision),
        "source": source,
        "stage": stage,
        "native_event": native_event,
        "event_id": event_id,
        "occurred_at_ms": int(_now_ms() if occurred_at_ms is None else occurred_at_ms),
    }
    return {
        "kind": LATCH_KIND,
        "schema": LATCH_SCHEMA,
        "latch_id": hashlib.sha256(adapter.dumps(body).encode("utf-8")).hexdigest(),
        **body,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "reason_codes": sorted(reason_codes),
        "incident_event_id": incident_event_id,
        "failure_code": failure_code,
        "enforced": bool(enforced),
        "harness": HARNESS,
        "redaction": sb.REDACTION,
    }


def append_latch(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(adapter.dumps(row) + "\n")


def latched(rows, session_ref: str):
    """Replay the ledger into one session's effective latch, or ``None``.

    Escalations raise the latch to the maximum rank seen; an operator ``clear``
    resets it. The *earliest* row at the surviving rank is kept, so the latch
    names the cause that first bound the session.
    """
    if not session_ref:
        return None
    state = None
    for row in rows:
        if row.get("session_ref") != session_ref:
            continue
        action = row.get("action")
        if action == "clear":
            state = None
            continue
        if action != "escalate" or not isinstance(row.get("rank"), int):
            raise VetoError(E_LATCH_SHAPE, "latch row carries no usable action")
        if state is None or row["rank"] > state["rank"]:
            state = {
                "rank": row["rank"],
                "decision": row.get("decision", ""),
                "latch_id": row.get("latch_id", ""),
                "event_id": row.get("event_id", ""),
                "incident_event_id": row.get("incident_event_id", ""),
                "source": row.get("source", ""),
                "stage": row.get("stage", ""),
                "native_event": row.get("native_event", ""),
                "occurred_at_ms": row.get("occurred_at_ms"),
            }
    return state


def escalate(
    path,
    *,
    session_ref: str,
    decision: str,
    source: str,
    stage: str = "",
    native_event: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    reason_codes=(),
    event_id: str = "",
    incident_event_id: str = "",
    failure_code: str = "",
    enforced: bool = True,
    occurred_at_ms=None,
) -> dict:
    """Raise a session latch. Idempotent: a covered latch writes nothing."""
    if not session_ref:
        return {
            "written": False,
            "reason": "no_session_identity",
            "row": None,
            "state": None,
        }
    if decision not in SEVERITY or SEVERITY[decision] == 0:
        return {"written": False, "reason": "not_a_veto", "row": None, "state": None}
    rows = read_latch(path)
    current = latched(rows, session_ref)
    if current is not None and current["rank"] >= SEVERITY[decision]:
        return {
            "written": False,
            "reason": "already_latched",
            "row": None,
            "state": current,
        }
    row = latch_row(
        session_ref=session_ref,
        action="escalate",
        decision=decision,
        source=source,
        stage=stage,
        native_event=native_event,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        reason_codes=reason_codes,
        event_id=event_id,
        incident_event_id=incident_event_id,
        failure_code=failure_code,
        enforced=enforced,
        occurred_at_ms=occurred_at_ms,
    )
    append_latch(path, row)
    return {
        "written": True,
        "reason": "escalated",
        "row": row,
        "state": latched(rows + [row], session_ref),
    }


def clear(path, *, session_ref: str, confirm: bool, occurred_at_ms=None) -> dict:
    """Clear one session latch, exactly like the component's ``clear-session``."""
    if not confirm:
        raise VetoError(
            E_CONFIRM, "clearing a session latch requires explicit --confirm"
        )
    if len(session_ref) != 64:
        raise VetoError(E_SESSION_REF, "a 64-character session_ref is required")
    row = latch_row(
        session_ref=session_ref,
        action="clear",
        decision="DEFER",
        source="operator",
        occurred_at_ms=occurred_at_ms,
    )
    append_latch(path, row)
    return {"cleared": session_ref, "row": row}


def latch_summary(rows) -> list:
    """Operator view: the effective state of every session in the ledger."""
    seen = []
    for row in rows:
        ref = row.get("session_ref")
        if ref and ref not in seen:
            seen.append(ref)
    summary = []
    for ref in seen:
        state = latched(rows, ref)
        summary.append(
            {
                "session_ref": ref,
                "rows": sum(1 for row in rows if row.get("session_ref") == ref),
                "latched": state is not None,
                "decision": (state or {}).get("decision", "DEFER"),
                "rank": (state or {}).get("rank", 0),
                "cause_event_id": (state or {}).get("event_id", ""),
                "source": (state or {}).get("source", ""),
            }
        )
    return summary


# ---------------------------------------------------------------- serialization


@contextlib.contextmanager
def session_lock(state_dir, session_ref: str):
    """Serialize evaluations of one session, in-process and across processes.

    With no session identity there is nothing to serialize (and nothing that can
    latch), so the lock is a no-op rather than a global bottleneck.
    """
    if not session_ref:
        yield
        return
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(session_ref, threading.Lock())
    with thread_lock:
        if fcntl is None:
            yield
            return
        path = Path(state_dir) / LOCK_DIRNAME / f"{session_ref}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- the gate


def latch_verdict(verdict, state: dict) -> dict:
    """A host-composed verdict for a veto the latch carries, not this event.

    The component's message is not available for an event that deferred, so the
    host names the provenance instead: the veto belongs to an earlier latched
    finding and stays until an operator clears the session.
    """
    verdict = verdict or {}
    return {
        "id": verdict.get("id") or state.get("event_id", ""),
        "decision": state["decision"],
        "enforced": True,
        "reason_codes": sorted(
            set(verdict.get("reason_codes", [])) | {"latched_session_veto"}
        ),
        "route": verdict.get("route") or FAILURE_ROUTE,
        "backend": verdict.get("backend") or "local",
        "session_ref": verdict.get("session_ref", ""),
        "message": (
            f"JEV Sentinel: the session already carries a latched {state['decision']} veto; "
            "this action stays vetoed until an operator clears the session latch."
        ),
    }


def gate(*, verdict, latched_state, enforced: bool, event_name: str, raw=None) -> dict:
    """Apply precedence and produce the response the harness honors.

    ``effective = max(this event, the session latch)``. Under enforcement any
    effective rank above ``DEFER`` vetoes; the response is the vendored pinned
    translation for the stage, so the host only ever emits supported keys. Under
    shadow the response is ``{}`` and the latch is neither applied nor cleared.
    """
    current_rank = rank(verdict.get("decision")) if is_veto(verdict) else 0
    latch_rank = latched_state["rank"] if latched_state else 0
    result = {
        "enforced": bool(enforced),
        "current_rank": current_rank,
        "latch_rank": latch_rank,
        "effective_decision": "DEFER",
        "effective_rank": 0,
        "vetoed": False,
        "source": "shadow" if not enforced else "defer",
        "response": {},
        "reason": "",
    }
    if not enforced:
        return result
    effective = max(current_rank, latch_rank)
    result["effective_rank"] = effective
    result["effective_decision"] = decision_for(effective)
    if effective == 0:
        return result
    result["vetoed"] = True
    if current_rank >= latch_rank and current_rank > 0:
        # This event's own finding vetoes: emit the component's response verbatim.
        result["source"] = "event"
        response = adapter.render(event_name, verdict, raw)
    else:
        # A later approval or DEFER cannot clear an earlier veto; the latch vetoes.
        result["source"] = "latch"
        response = adapter.render(
            event_name, latch_verdict(verdict, latched_state), raw
        )
    adapter.assert_no_replacement(response)
    result["response"] = response
    result["reason"] = response.get("reason", "")
    return result


# ---------------------------------------------------------------- handler


def _failure_incident(raw, *, event_name, profile, identity, state_dir, code, enforce):
    """Record a fail-closed incident when the payload can still be normalized."""
    try:
        event = adapter.normalize(event_name, raw, profile)
    except adapter.AdapterError:
        return None
    if identity.get("session_id"):
        event["session_id"] = identity["session_id"]
    record = sb.incident(
        event,
        native_event=event_name,
        identity=identity,
        outcome="fail_closed",
        verdict=None,
        enforced=enforce,
        decision="REVIEW",
        reason_codes=["host_" + code.removeprefix("E_").lower()],
        route=FAILURE_ROUTE,
        backend=FAILURE_BACKEND,
    )
    sb.append_incident(sb.incident_path(state_dir), record)
    return record


def handle(
    raw,
    *,
    event_name: str,
    profile: str,
    identity: dict,
    component: Path,
    policy_path: Path,
    policy: dict,
    enforce: bool,
    state_dir,
) -> dict:
    """The concurrency-safe entry: observe, apply precedence, latch, respond.

    Returns the host response plus the provenance a reviewer needs: the
    component's verdict, the effective decision, which side supplied the veto,
    and the latch transition. A component failure, a malformed response, a
    policy-shape failure, or a cancellation is converted into the fail-closed
    ``REVIEW`` and latched under enforcement; a cancellation is re-raised only
    after the latch is durable.
    """
    state_dir = Path(state_dir)
    path = latch_path(state_dir)
    stage = adapter.EVENTS.get(event_name, "")
    session_ref = sb.session_ref_for(HARNESS, profile, identity.get("session_id") or "")
    identity = {**identity, "session_ref": session_ref}

    with session_lock(state_dir, session_ref):
        before = latched(read_latch(path), session_ref)
        # Chain the incident to the event that latched the session (#18 left this
        # unset on purpose; the latch is what makes the cause knowable).
        if before is not None:
            identity = {
                **identity,
                "parent_event_id": before.get("incident_event_id", ""),
            }

        observed = None
        failure = ""
        try:
            observed = sb.observe(
                raw,
                event_name=event_name,
                profile=profile,
                identity=identity,
                component=component,
                policy_path=policy_path,
                policy=policy,
                enforced=enforce,
                state_dir=state_dir,
            )
        except sb.BoundaryError as exc:
            failure = exc.code
        except BaseException:
            # A cancelled evaluation must not leave the session ungated.
            if enforce:
                escalate(
                    path,
                    session_ref=session_ref,
                    decision="REVIEW",
                    source="failure",
                    stage=stage,
                    native_event=event_name,
                    turn_id=identity.get("turn_id", ""),
                    tool_call_id=identity.get("tool_call_id", ""),
                    reason_codes=["host_cancelled"],
                    failure_code="host_cancelled",
                    enforced=enforce,
                )
            raise

        if failure:
            incident = _failure_incident(
                raw,
                event_name=event_name,
                profile=profile,
                identity=identity,
                state_dir=state_dir,
                code=failure,
                enforce=enforce,
            )
            verdict = failure_verdict(
                "host_" + failure.removeprefix("E_").lower(), enforce
            )
            source = "failure"
        elif observed.get("refused"):
            # The boundary refused a payload it could not bound and recorded a
            # REVIEW incident. Under enforcement that REVIEW is a veto too.
            verdict = failure_verdict(
                "host_" + observed["refused"].removeprefix("E_").lower(), enforce
            )
            incident = observed.get("incident")
            source = "boundary_refusal"
        else:
            verdict = observed["verdict"]
            incident = observed.get("incident")
            source = "event"

        outcome = gate(
            verdict=verdict,
            latched_state=before,
            enforced=enforce,
            event_name=event_name,
            raw=raw,
        )

        transition = {
            "written": False,
            "reason": "shadow" if not enforce else "no_veto",
            "row": None,
        }
        if enforce and outcome["vetoed"]:
            transition = escalate(
                path,
                session_ref=session_ref,
                decision=outcome["effective_decision"],
                source=source,
                stage=stage,
                native_event=event_name,
                turn_id=identity.get("turn_id", ""),
                tool_call_id=identity.get("tool_call_id", ""),
                reason_codes=verdict.get("reason_codes", []),
                event_id=verdict.get("id", ""),
                incident_event_id=(incident or {}).get("event_id", ""),
                failure_code=failure,
                enforced=enforce,
            )

        # ``source`` names the side that supplied the effective veto: the session
        # latch, this event's own finding, or the host's own fail-closed path.
        veto_source = outcome["source"]
        if veto_source == "event":
            veto_source = source

        return {
            "event_name": event_name,
            "stage": stage,
            "profile": profile,
            "session_ref": session_ref,
            "enforce": bool(enforce),
            "failure": failure,
            "refused": (observed or {}).get("refused", ""),
            "verdict": verdict,
            "verdict_source": source,
            "effective_decision": outcome["effective_decision"],
            "vetoed": outcome["vetoed"],
            "source": veto_source,
            "response": outcome["response"],
            "latch_before": before,
            "latch": transition,
            "latch_after": latched(read_latch(path), session_ref),
            "incident_event_id": (incident or {}).get("event_id", ""),
            "observed": observed,
        }


# ---------------------------------------------------------------- CLI


def _identity(args) -> dict:
    return {
        "profile": args.profile,
        "session_id": args.session,
        "turn_id": args.turn,
        "tool_call_id": args.tool_call_id,
        "workspace": args.workspace,
        "parent_event_id": "",
    }


def _read_request(path):
    raw = (
        Path(path).read_text(encoding="utf-8")
        if path and path != "-"
        else sys.stdin.read()
    )
    return adapter.strict_json(raw)


def _load_policy_fail_closed(path: Path) -> dict:
    """A corrupt or missing policy must not silently disable the gate."""
    try:
        return sb.load_policy(path)
    except sb.BoundaryError:
        return {
            "mode": "enforce",
            "backend": "local",
            "max_content_bytes": 65536,
            "fail_closed": True,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("enforce", "apply veto precedence and print the host response"),
        ("latch", "list the latch ledger"),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument(
            "--component", default="", help="pinned jev-sentinel checkout"
        )
        node.add_argument("--policy", default="", help="sentinel policy.json")
        node.add_argument("--state-dir", default="")
        node.add_argument("--json", action="store_true")
        if name == "enforce":
            node.add_argument(
                "--event", required=True, help="native event name or boundary stage"
            )
            node.add_argument("--profile", default="default")
            node.add_argument("--session", default="")
            node.add_argument("--turn", default="")
            node.add_argument("--tool-call-id", default="")
            node.add_argument("--workspace", default="")
            node.add_argument(
                "--request", default="-", help="native payload JSON, or - for stdin"
            )
            node.add_argument(
                "--force-enforce",
                action="store_true",
                help="treat the switches as on (operator probe; the profile still decides)",
            )

    clr = sub.add_parser("clear", help="clear one session latch")
    clr.add_argument("--state-dir", default="")
    clr.add_argument("--session-ref", required=True)
    clr.add_argument("--confirm", action="store_true")
    clr.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "latch":
            state_dir = (
                Path(args.state_dir) if args.state_dir else _default_policy().parent
            )
            rows = read_latch(latch_path(state_dir))
            report = {"rows": len(rows), "sessions": latch_summary(rows)}
            print(json.dumps(report, indent=2 if args.json else None))
            return 0

        if args.command == "clear":
            state_dir = (
                Path(args.state_dir) if args.state_dir else _default_policy().parent
            )
            result = clear(
                latch_path(state_dir),
                session_ref=args.session_ref,
                confirm=args.confirm,
            )
            print(json.dumps(result, indent=2 if args.json else None))
            return 0

        manifest = sb.load_manifest()
        switches = sb.feature_switches(manifest)
        policy_path = Path(args.policy) if args.policy else _default_policy()
        policy = _load_policy_fail_closed(policy_path)
        if args.force_enforce:
            switches = {sb.SHADOW_SWITCH: True, sb.ENFORCE_SWITCH: True}
        enforce = enforcement_enabled(policy, switches)
        component = sb.resolve_component(args.component, manifest=manifest)
        event_name = sb._resolve_event_name(args.event)
        result = handle(
            _read_request(args.request),
            event_name=event_name,
            profile=args.profile,
            identity=_identity(args),
            component=component,
            policy_path=policy_path,
            policy=policy,
            enforce=enforce,
            state_dir=Path(args.state_dir) if args.state_dir else policy_path.parent,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result["response"]))
        return 0
    except sb.BoundaryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except VetoError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
