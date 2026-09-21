#!/usr/bin/env python3
"""Bounded, metadata-only operations over the host's incident journal.

Phase 4.3 connects incident *operations* to the incidents that #18 records and
#19 latches. Contract `C12` in [`CONTRACTS.md`](../CONTRACTS.md) states the
rules; [`INCIDENT_OPERATIONS.md`](../INCIDENT_OPERATIONS.md) is the long form.

Four properties are load-bearing, and each one is a refusable check rather than
a promise:

* **Metadata-only.** Every row is validated against the manifest's own
  ``events.envelope_fields`` plus the documented extras, its digests must be
  digests, its metadata must stay inside a byte bound, and no string may still
  match the capture layer's own credential rules. A row that fails any check is
  *not printed* - only its identifier and the failing checks are - so a journal
  written by a different, less careful writer cannot be laundered into a report.
* **Read-only.** Nothing here opens a file for writing, and ``disable`` does not
  execute anything: it prints the exact commands an operator would run. The
  module never reads the component's own audit store, so inspection cannot
  mutate or taint the store the component owns.
* **Correlated.** An incident is joined to the canonical capture store by the
  content digest the host recorded, and secondarily by its correlation identity
  (``session_id``/``turn_id``/``tool_call_id``). A join reports which key
  matched; it never infers one.
* **No automatic external dispatch.** No network path exists in this module, and
  no step is dispatched anywhere: an operator reads a plan and runs it.

Subcommands:

``list``       newest-first bounded incident rows, filtered by metadata;
``show``       one incident row by its host ``event_id``;
``summary``    counts, sessions, and the correlated time range;
``correlate``  join the journal to the canonical capture store;
``policy``     the isolated profile's policy and switch state, read-only;
``disable``    the exact disable/rollback steps, printed and never executed.

Exit codes: 0 ok, 1 refused or incomplete, 2 usage error.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_capture  # noqa: E402
import jev_manifest  # noqa: E402
import jev_sentinel_adapter as adapter  # noqa: E402
import sentinel_boundary as sb  # noqa: E402

HOST = sb.HOST
INCIDENT_KIND = sb.INCIDENT_KIND
REDACTION = sb.REDACTION
COMPONENT_ID = sb.COMPONENT_ID

OPS_SCHEMA = "jev-incident-ops.v1"

DEFAULT_LIMIT = 20
# Matches the component's own ``outbox --limit`` ceiling: a scan that saturates
# is reported as saturated rather than as "there is nothing else".
MAX_LIMIT = sb.OUTBOX_SCAN_LIMIT
# A metadata field longer than this is not metadata. An excerpt that reached the
# journal is caught here even when it does not look like a credential.
MAX_METADATA_STRING = 512
MAX_FINDINGS = 16

# Digests the envelope must carry as digests.
DIGEST_FIELDS = ("content_sha256", "action_sha256")

# The envelope fields the manifest fixes, plus the four the host adds. The
# manifest is the authority for the first group, so a manifest change moves the
# validator rather than drifting from it.
HOST_FIELDS = ("kind", "native_event", "outcome", "sentinel", "identity")
SENTINEL_FIELDS = (
    "event_id",
    "decision",
    "enforced",
    "reason_codes",
    "route",
    "backend",
    "session_ref",
    "content_sha256",
    "action_sha256",
    "content_bytes",
    "tool_name",
    "harness",
    "profile",
)
IDENTITY_FIELDS = ("harness", "profile", "session_id", "turn_id", "tool_call_id")

E_LIMIT = "E_LIMIT"
E_INCIDENT_SHAPE = "E_INCIDENT_SHAPE"
E_INCIDENT_UNREDACTED = "E_INCIDENT_UNREDACTED"
E_INCIDENT_MISSING = "E_INCIDENT_MISSING"
E_POLICY_NOT_ISOLATED = "E_POLICY_NOT_ISOLATED"
E_COVERAGE = "E_COVERAGE"


class IncidentOpsError(ValueError):
    """A bounded refusal whose message never carries journal content."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical(value) -> str:
    return adapter.dumps(value)


def _is_digest(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def load_manifest():
    return jev_manifest.load_json(jev_manifest.default_manifest_path(), "manifest")


def allowed_fields(manifest=None) -> frozenset:
    manifest = manifest or load_manifest()
    envelope = manifest.get("events", {}).get("envelope_fields", [])
    if not isinstance(envelope, list) or not envelope:
        raise IncidentOpsError(E_INCIDENT_SHAPE, "manifest declares no envelope fields")
    return frozenset(envelope) | set(HOST_FIELDS)


def declared_switches(manifest=None, env=None) -> dict:
    """Resolve *every* switch the manifest declares, in name order.

    Incident operations span more than one phase, so a view that echoed the
    Sentinel pair alone would hide the screening switches an operator has to be
    able to see and to unset. The manifest stays the authority for the list.
    """
    manifest = manifest if manifest is not None else load_manifest()
    return sb.feature_switches_for(manifest, sorted(manifest.get("features", {})), env)


# ---------------------------------------------------------------- validation


def _walk_strings(value, prefix=""):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{prefix}[{index}]")


def redaction_findings(record, allowed=None) -> list:
    """Every reason this row must not be printed, in a bounded list."""
    allowed = allowed if allowed is not None else allowed_fields()
    findings = []
    if not isinstance(record, dict):
        return ["not_an_object"]
    if record.get("redaction") != REDACTION:
        findings.append("redaction_field_missing")
    findings += [f"unexpected_field:{key}" for key in sorted(set(record) - allowed)]

    sentinel = record.get("sentinel")
    if not isinstance(sentinel, dict):
        findings.append("sentinel_shape")
    else:
        findings += [
            f"unexpected_sentinel_field:{key}"
            for key in sorted(set(sentinel) - set(SENTINEL_FIELDS))
        ]
        for key in DIGEST_FIELDS:
            if not _is_digest(sentinel.get(key, "")):
                findings.append(f"digest_shape:{key}")

    identity = record.get("identity")
    if not isinstance(identity, dict):
        findings.append("identity_shape")
    else:
        findings += [
            f"unexpected_identity_field:{key}"
            for key in sorted(set(identity) - set(IDENTITY_FIELDS))
        ]

    for path, text in _walk_strings(record):
        if len(text) > MAX_METADATA_STRING:
            findings.append(f"oversized_field:{path}")
        hits = canonical_capture.secret_hits(text)
        if hits:
            findings.append(f"secret_shaped:{path}:{','.join(hits)}")
    return findings


def inspect(record) -> dict:
    """Project one validated incident onto a bounded, metadata-only row."""
    sentinel = record.get("sentinel", {}) or {}
    return {
        "event_id": record.get("event_id", ""),
        "parent_event_id": record.get("parent_event_id") or "",
        "kind": record.get("kind", ""),
        "component": record.get("component", ""),
        "stage": record.get("stage", ""),
        "native_event": record.get("native_event", ""),
        "outcome": record.get("outcome", ""),
        "decision": record.get("decision", ""),
        "session_id": record.get("session_id", ""),
        "turn_id": record.get("turn_id", ""),
        "tool_call_id": record.get("tool_call_id", ""),
        "origin_workspace": record.get("origin_workspace", ""),
        "capture_id": record.get("capture_id", ""),
        "occurred_at_ms": int(record.get("occurred_at_ms") or 0),
        "redaction": record.get("redaction", ""),
        "component_event_id": sentinel.get("event_id", ""),
        "component_session_ref": sentinel.get("session_ref", ""),
        "component_decision": sentinel.get("decision", ""),
        "component_enforced": bool(sentinel.get("enforced")),
        "reason_codes": sorted(sentinel.get("reason_codes", []) or []),
        "route": sentinel.get("route", ""),
        "backend": sentinel.get("backend", ""),
        "content_sha256": sentinel.get("content_sha256", ""),
        "action_sha256": sentinel.get("action_sha256", ""),
        "content_bytes": int(sentinel.get("content_bytes") or 0),
        "tool_name": sentinel.get("tool_name", ""),
        "harness": sentinel.get("harness", ""),
        "profile": sentinel.get("profile", ""),
    }


def _read_journal(path):
    """Yield parsed rows and count what could not be parsed. Read-only."""
    path = Path(path)
    if not path.is_file():
        raise IncidentOpsError(E_INCIDENT_MISSING, f"no incident journal at {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield adapter.strict_json(line), ""
            except adapter.AdapterError as exc:
                yield None, type(exc).__name__


def _bounded_limit(limit) -> int:
    if type(limit) is not int or limit < 1 or limit > MAX_LIMIT:
        raise IncidentOpsError(E_LIMIT, f"limit must be an integer in 1..{MAX_LIMIT}")
    return limit


# ---------------------------------------------------------------- operations


def list_incidents(
    path,
    *,
    limit=DEFAULT_LIMIT,
    kind=None,
    session_id=None,
    decision=None,
    outcome=None,
    since_ms=None,
) -> dict:
    """Newest-first bounded rows. A refused row is reported, never printed."""
    limit = _bounded_limit(limit)
    allowed = allowed_fields()
    window = collections.deque(maxlen=limit)
    total = 0
    matched = 0
    malformed = 0
    refused = []
    for record, error in _read_journal(path):
        total += 1
        if record is None:
            malformed += 1
            continue
        findings = redaction_findings(record, allowed)
        if findings:
            refused.append(
                {
                    "event_id": record.get("event_id", ""),
                    "findings": findings[:MAX_FINDINGS],
                    "findings_truncated": max(0, len(findings) - MAX_FINDINGS),
                }
            )
            continue
        row = inspect(record)
        if kind and row["kind"] != kind:
            continue
        if session_id and row["session_id"] != session_id:
            continue
        if decision and row["decision"] != decision:
            continue
        if outcome and row["outcome"] != outcome:
            continue
        if since_ms is not None and row["occurred_at_ms"] < int(since_ms):
            continue
        matched += 1
        window.append(row)
    rows = list(window)
    rows.reverse()
    return {
        "schema": OPS_SCHEMA,
        "kind": "incident_list",
        "rows": rows,
        "returned": len(rows),
        "matched": matched,
        "total": total,
        "malformed": malformed,
        "refused": refused[:MAX_FINDINGS],
        "refused_count": len(refused),
        "saturated": matched > len(rows),
        "bounded_by": limit,
        "filter": {
            "kind": kind or "",
            "session_id": session_id or "",
            "decision": decision or "",
            "outcome": outcome or "",
            "since_ms": since_ms,
        },
        "dispatch": {"network": "none", "executes": False, "writes": False},
    }


def show_incident(path, event_id) -> dict:
    """One incident row by its host ``event_id``."""
    if not isinstance(event_id, str) or not event_id:
        raise IncidentOpsError(E_INCIDENT_SHAPE, "event_id is required")
    allowed = allowed_fields()
    for record, _error in _read_journal(path):
        if record is None or record.get("event_id") != event_id:
            continue
        findings = redaction_findings(record, allowed)
        if findings:
            raise IncidentOpsError(
                E_INCIDENT_UNREDACTED, ",".join(findings[:MAX_FINDINGS])
            )
        return {
            "schema": OPS_SCHEMA,
            "kind": "incident_show",
            "row": inspect(record),
            "dispatch": {"network": "none", "executes": False, "writes": False},
        }
    raise IncidentOpsError(E_INCIDENT_MISSING, "no incident with that event_id")


def summary(path, *, limit=MAX_LIMIT) -> dict:
    """Counts and the correlated time range, over a bounded scan."""
    limit = _bounded_limit(limit)
    allowed = allowed_fields()
    decisions = collections.Counter()
    outcomes = collections.Counter()
    routes = collections.Counter()
    backends = collections.Counter()
    sessions = set()
    components = set()
    total = 0
    malformed = 0
    refused = 0
    first = None
    last = None
    for record, _error in _read_journal(path):
        total += 1
        if record is None:
            malformed += 1
            continue
        if redaction_findings(record, allowed):
            refused += 1
            continue
        row = inspect(record)
        decisions[row["decision"]] += 1
        outcomes[row["outcome"]] += 1
        routes[row["route"]] += 1
        backends[row["backend"]] += 1
        if row["session_id"]:
            sessions.add(row["session_id"])
        if row["component"]:
            components.add(row["component"])
        stamp = row["occurred_at_ms"]
        if stamp:
            first = stamp if first is None else min(first, stamp)
            last = stamp if last is None else max(last, stamp)
        if total > limit:
            break
    return {
        "schema": OPS_SCHEMA,
        "kind": "incident_summary",
        "total": total,
        "malformed": malformed,
        "refused": refused,
        "decisions": dict(sorted(decisions.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "routes": dict(sorted(routes.items())),
        "backends": dict(sorted(backends.items())),
        "sessions": len(sessions),
        "components": sorted(components),
        "occurred_at_ms": {"first": first, "last": last},
        "saturated": total >= limit,
        "bounded_by": limit,
        "dispatch": {"network": "none", "executes": False, "writes": False},
    }


def correlate(path, env_dir, *, limit=MAX_LIMIT) -> dict:
    """Join incidents to canonical capture events, reporting which key matched.

    The primary key is the content digest the host recorded; the secondary key
    is the correlation identity. A join that matches neither is reported as
    unmatched - it is never approximated into a match.
    """
    limit = _bounded_limit(limit)
    env_dir = Path(env_dir)
    events = canonical_capture.read_jsonl(
        canonical_capture.capture_root(env_dir) / "events.jsonl"
    )
    by_content = {}
    by_identity = {}
    for event in events:
        if event.get("content_sha256"):
            by_content.setdefault(event["content_sha256"], event)
        key = (
            event.get("session_id") or "",
            event.get("turn_id") or "",
            event.get("tool_call_id") or "",
        )
        by_identity.setdefault(key, event)

    allowed = allowed_fields()
    matched_content = []
    matched_identity = []
    unmatched = []
    refused = 0
    total = 0
    for record, _error in _read_journal(path):
        total += 1
        if total > limit:
            break
        if record is None or redaction_findings(record, allowed):
            refused += 1
            continue
        row = inspect(record)
        event = by_content.get(row["content_sha256"])
        if event is not None:
            matched_content.append(
                {
                    "event_id": row["event_id"],
                    "capture_event_id": event["event_id"],
                    "capture_id": event["capture_id"],
                    "kind": event["kind"],
                }
            )
            continue
        key = (row["session_id"], row["turn_id"], row["tool_call_id"])
        event = by_identity.get(key)
        if event is not None and any(key):
            matched_identity.append(
                {
                    "event_id": row["event_id"],
                    "capture_event_id": event["event_id"],
                    "capture_id": event["capture_id"],
                    "kind": event["kind"],
                }
            )
            continue
        unmatched.append(
            {
                "event_id": row["event_id"],
                "kind": row["kind"],
                "native_event": row["native_event"],
                "outcome": row["outcome"],
                "content_sha256": row["content_sha256"],
            }
        )
    return {
        "schema": OPS_SCHEMA,
        "kind": "incident_correlation",
        "scanned": min(total, limit),
        "refused": refused,
        "canonical_events": len(events),
        "matched_by_content": len(matched_content),
        "matched_by_identity": len(matched_identity),
        "unmatched": len(unmatched),
        "matches": {"content": matched_content, "identity": matched_identity},
        "unmatched_rows": unmatched[:MAX_FINDINGS],
        "saturated": total > limit,
        "dispatch": {"network": "none", "executes": False, "writes": False},
    }


def policy_view(profile_root, *, policy_path=None, switches=None) -> dict:
    """The isolated profile's policy and switch state, read-only.

    The policy is refused unless it lives inside the isolated profile root, so
    an operator cannot be shown a policy that a launch would not use.
    """
    root = Path(profile_root).expanduser().resolve()
    path = (
        Path(policy_path).expanduser().resolve()
        if policy_path
        else root / "policy.json"
    )
    if root not in path.parents and path != root:
        raise IncidentOpsError(
            E_POLICY_NOT_ISOLATED,
            "policy must live inside the isolated profile root",
        )
    manifest = load_manifest()
    if switches is None:
        switches = declared_switches(manifest)
    declared = manifest.get("features", {})
    closure = []
    for name, enabled in sorted(switches.items()):
        if not enabled:
            continue
        for required in declared.get(name, {}).get("requires", []):
            if not switches.get(required):
                closure.append(f"{name} requires {required}")
    policy = sb.load_policy(path) if path.is_file() else None
    return {
        "schema": OPS_SCHEMA,
        "kind": "policy_view",
        "profile_root": str(root),
        "policy_path": str(path),
        "policy_present": path.is_file(),
        "policy_sha256": jev_manifest.sha256_file(path) if path.is_file() else "",
        "policy": policy,
        "switches": dict(sorted(switches.items())),
        "closure_errors": closure,
        "isolated": True,
        "read_only": True,
        "dispatch": {"network": "none", "executes": False, "writes": False},
    }


def disable_plan(*, profile_root=None, hooks_path=None, switches=None) -> dict:
    """The exact disable/rollback steps. Printed, never executed.

    Every step is a string an operator runs, not an action this module takes, so
    disabling the integration can never be an automatic side effect of asking
    what disabling would do.
    """
    manifest = load_manifest()
    if switches is None:
        switches = declared_switches(manifest)
    steps = []
    for name, enabled in sorted(switches.items()):
        key = "JEV_SWITCH_" + name.upper().replace(".", "_")
        if enabled:
            steps.append(
                {
                    "step": len(steps) + 1,
                    "action": f"unset {key}",
                    "detail": f"{name} is on by declaration or environment",
                    "executes": False,
                }
            )
    coverage = None
    if profile_root:
        try:
            coverage = sb.read_coverage(profile_root, hooks_path)
        except sb.BoundaryError as exc:
            coverage = {
                "hooks_path": str(hooks_path or Path(profile_root) / "hooks.json"),
                "error": exc.code,
            }
    if coverage is not None:
        for stage, entry in sorted((coverage.get("paths") or {}).items()):
            if not entry.get("wired"):
                continue
            steps.append(
                {
                    "step": len(steps) + 1,
                    "action": f"remove the {stage} hook entries from {coverage['hooks_path']}",
                    "detail": f"{entry['native_event']} wires {entry['commands']} command(s)",
                    "executes": False,
                }
            )
    if coverage is not None and coverage.get("disable_all_hooks"):
        steps.append(
            {
                "step": len(steps) + 1,
                "action": "no hook removal is needed",
                "detail": "disableAllHooks is already true",
                "executes": False,
            }
        )
    steps.append(
        {
            "step": len(steps) + 1,
            "action": "set the component policy back to shadow",
            "detail": "python3 -I <component>/launch.py policy --policy <policy> --mode shadow",
            "executes": False,
        }
    )
    steps.append(
        {
            "step": len(steps) + 1,
            "action": "re-run the coverage probe to confirm nothing is activated",
            "detail": "python3 jev/scripts/sentinel_boundary.py coverage --probe",
            "executes": False,
        }
    )
    return {
        "schema": OPS_SCHEMA,
        "kind": "disable_plan",
        "steps": steps,
        "coverage": coverage,
        "dispatch": {"network": "none", "executes": False, "writes": False},
        "reachable": False,
    }


# ---------------------------------------------------------------- command line


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Bounded, metadata-only incident operations"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    journal = Path("codex-jev-incidents.jsonl")

    list_node = sub.add_parser("list")
    list_node.add_argument("--incidents", default=str(journal))
    list_node.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    list_node.add_argument("--kind")
    list_node.add_argument("--session-id")
    list_node.add_argument("--decision")
    list_node.add_argument("--outcome")
    list_node.add_argument("--since-ms", type=int)

    show_node = sub.add_parser("show")
    show_node.add_argument("--incidents", default=str(journal))
    show_node.add_argument("--event-id", required=True)

    summary_node = sub.add_parser("summary")
    summary_node.add_argument("--incidents", default=str(journal))
    summary_node.add_argument("--limit", type=int, default=MAX_LIMIT)

    correlate_node = sub.add_parser("correlate")
    correlate_node.add_argument("--incidents", default=str(journal))
    correlate_node.add_argument("--env", required=True)
    correlate_node.add_argument("--limit", type=int, default=MAX_LIMIT)

    policy_node = sub.add_parser("policy")
    policy_node.add_argument("--profile-root", required=True)
    policy_node.add_argument("--policy", default="")

    disable_node = sub.add_parser("disable")
    disable_node.add_argument("--profile-root", default="")
    disable_node.add_argument("--hooks", default="")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "list":
            result = list_incidents(
                args.incidents,
                limit=args.limit,
                kind=args.kind,
                session_id=args.session_id,
                decision=args.decision,
                outcome=args.outcome,
                since_ms=args.since_ms,
            )
        elif args.command == "show":
            result = show_incident(args.incidents, args.event_id)
        elif args.command == "summary":
            result = summary(args.incidents, limit=args.limit)
        elif args.command == "correlate":
            result = correlate(args.incidents, args.env, limit=args.limit)
        elif args.command == "policy":
            result = policy_view(args.profile_root, policy_path=args.policy or None)
        else:
            result = disable_plan(
                profile_root=args.profile_root or None,
                hooks_path=args.hooks or None,
            )
    except (IncidentOpsError, sb.BoundaryError, jev_manifest.ManifestError) as exc:
        print(_canonical({"error": str(exc)}))
        return 1
    print(_canonical(result))
    if args.command == "list" and result["refused_count"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
