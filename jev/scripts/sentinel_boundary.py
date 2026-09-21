#!/usr/bin/env python3
"""Codex host wiring for the pinned ``jev-sentinel`` boundary checks.

Phase 4 wires the Sentinel evaluator onto the three Codex hook boundaries -
``UserPromptSubmit`` (ingress), ``PreToolUse`` (tool_before), and ``PostToolUse``
(tool_after) - and reports *effective* coverage. The host owns the envelope, the
correlation identity, and the coverage claim; the pinned component
(``jev-sentinel``) owns detection, thresholds, the audit store, and the policy.
Every verdict in this module comes from running the installed/fetched runtime, so
nothing about detection is duplicated here.

Three facts are load-bearing:

* **Bounded payloads.** The host refuses to forward a payload it cannot bound -
  larger than the component's own input limit (``jev_sentinel_adapter.MAX_INPUT``)
  or carrying more content than the policy's ``max_content_bytes`` (the same
  threshold where the component's own ``content_limit`` rule answers ``REVIEW``) -
  and records a fail-closed ``REVIEW`` incident instead of truncating. Content
  over the bound never leaves the host and is never assessed as if complete.
* **Shadow is observational.** ``sentinel.shadow`` defaults off and the policy
  defaults to ``mode=shadow``; a shadow finding is recorded and never vetoes, so
  the host response is ``{}``. ``sentinel.enforcement`` requires
  ``sentinel.shadow`` (the manifest declares the ordering).
* **Installation is not activation.** Coverage reports ``activated: true`` only
  when a deterministic canary actually runs through the wired hook commands and
  lands a correlated audit row. Present files, a resolvable launcher, and a
  matching revision are necessary, never sufficient.

Subcommands:

``coverage``  report the wired event paths, covered tools, and bypass surfaces;
              ``--probe`` runs the canaries through the wired commands;
``canary``    run the deterministic canaries through this boundary and emit
              correlated ``sentinel_incident`` envelopes;
``observe``   normalize, bound, evaluate, and record one native host event.

Exit codes: 0 ok, 1 refused or incomplete, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_bus  # noqa: E402 - vendored jev-bus.v1, only for MAX_WIRE
import jev_manifest  # noqa: E402
import jev_sentinel_adapter as adapter  # noqa: E402

HOST = adapter.HARNESS
ADAPTER = "codex-jev"
COMPONENT_ID = "jev-sentinel"
INCIDENT_KIND = "sentinel_incident"
COVERAGE_SCHEMA = "jev-sentinel.coverage.v1"
REDACTION = "content_sha256_only"

# The host forwards nothing larger than the component's own input limit.
FORWARD_LIMIT = adapter.MAX_INPUT
HARD_RAW_BOUND = jev_bus.MAX_WIRE
COMPONENT_STDOUT_BOUND = 65536
# Matches the ``timeout`` the installer writes into ``hooks.json``.
HOOK_TIMEOUT_S = 12
# The component's ``outbox`` caps ``--limit`` at 100; the probe reports a
# saturated scan as inconclusive rather than as an inactive integration.
OUTBOX_SCAN_LIMIT = 100

# A literal tool name may use these characters; anything else is regex syntax.
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]+")

# Stable refusal codes. A boundary that cannot prove its invariants refuses
# rather than approximating a decision.
E_HOOKS_MISSING = "E_HOOKS_MISSING"
E_HOOKS_SHAPE = "E_HOOKS_SHAPE"
E_EVENT_NAME = "E_EVENT_NAME"
E_PAYLOAD_BOUND = "E_PAYLOAD_BOUND"
E_PAYLOAD_SHAPE = "E_PAYLOAD_SHAPE"
E_COMPONENT_MISSING = "E_COMPONENT_MISSING"
E_COMPONENT_FAILED = "E_COMPONENT_FAILED"
E_VERDICT_SHAPE = "E_VERDICT_SHAPE"
E_POLICY_SHAPE = "E_POLICY_SHAPE"

# The switch pair the manifest declares for this phase, in dependency order.
SHADOW_SWITCH = "sentinel.shadow"
ENFORCE_SWITCH = "sentinel.enforcement"

# The boundaries that carry a native tool matcher, in evaluation order.
TOOL_STAGES = ("tool_before", "tool_after")
EVENT_PATH_ORDER = ("ingress", "tool_before", "tool_after")


class BoundaryError(ValueError):
    """A bounded, non-sensitive refusal. Its message never contains context text."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def load_manifest():
    return jev_manifest.load_json(jev_manifest.default_manifest_path(), "manifest")


def _canonical(value) -> str:
    return adapter.dumps(value)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def component_index(manifest) -> dict:
    for component in manifest.get("components", []):
        if component.get("id") == COMPONENT_ID:
            return component
    raise BoundaryError(E_COMPONENT_MISSING, f"manifest declares no {COMPONENT_ID}")


# ---------------------------------------------------------------- switches


def feature_switches(manifest, env=None) -> dict:
    """Resolve the two Sentinel switches from the ``JEV_SWITCH_*`` environment."""
    return feature_switches_for(manifest, (SHADOW_SWITCH, ENFORCE_SWITCH), env)


def feature_switches_for(manifest, features, env=None) -> dict:
    """Resolve named manifest switches from their ``JEV_SWITCH_*`` environment.

    A feature with no environment override takes its declared default, so an
    unset variable means "whatever the manifest says", never "off". Later phases
    declare their own switch pairs and resolve them through this one rule.
    """
    env = os.environ if env is None else env
    declared = manifest.get("features", {})
    state = {}
    for feature in features:
        spec = declared.get(feature)
        if spec is None:
            raise BoundaryError(E_POLICY_SHAPE, f"manifest declares no feature {feature}")
        key = "JEV_SWITCH_" + feature.upper().replace(".", "_")
        raw = env.get(key)
        state[feature] = bool(spec["default"]) if raw is None else raw == "1"
    return state


# ---------------------------------------------------------------- component


def resolve_component(explicit: str = "", env=None, manifest=None) -> Path:
    """Locate the pinned component checkout that owns the ``launch.py`` runtime."""
    env = os.environ if env is None else env
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if env.get("JEV_SENTINEL_ROOT"):
        candidates.append(Path(env["JEV_SENTINEL_ROOT"]))
    if env.get("JEV_COMPONENTS_ROOT"):
        candidates.append(Path(env["JEV_COMPONENTS_ROOT"]) / COMPONENT_ID)
    manifest = manifest or load_manifest()
    repository = component_index(manifest)["repository"].rsplit("/", 1)[-1]
    if repository != COMPONENT_ID:
        raise BoundaryError(E_COMPONENT_MISSING, "manifest repository name mismatch")
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (root / "launch.py").is_file():
            return root
    raise BoundaryError(
        E_COMPONENT_MISSING,
        "pass --component or set JEV_SENTINEL_ROOT to the pinned jev-sentinel checkout",
    )


def component_revision(root: Path) -> str:
    """Read the checkout revision without importing the component."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# ---------------------------------------------------------------- policy


def load_policy(path: Path) -> dict:
    """Read the fields this boundary consumes; the component validates the rest."""
    try:
        configured = adapter.strict_json(Path(path).read_bytes())
    except OSError as exc:
        raise BoundaryError(E_POLICY_SHAPE, f"policy unreadable: {Path(path).name}") from exc
    except adapter.AdapterError as exc:
        raise BoundaryError(E_POLICY_SHAPE, str(exc)) from exc
    if not isinstance(configured, dict):
        raise BoundaryError(E_POLICY_SHAPE, "policy must be an object")
    mode = configured.get("mode", "shadow")
    backend = configured.get("backend", "local")
    if mode not in ("shadow", "enforce"):
        raise BoundaryError(E_POLICY_SHAPE, "policy mode must be shadow or enforce")
    if backend not in ("local", "jev"):
        raise BoundaryError(E_POLICY_SHAPE, "policy backend must be local or jev")
    limit = configured.get("max_content_bytes", 65536)
    if type(limit) is not int or not 256 <= limit <= 100000:
        raise BoundaryError(E_POLICY_SHAPE, "policy max_content_bytes is out of range")
    return {"mode": mode, "backend": backend, "max_content_bytes": limit}


# ---------------------------------------------------------------- bound


def digest_content(event: dict) -> str:
    """The component's own audit digest: sha256 of the normalized content."""
    return hashlib.sha256(event["content"].encode("utf-8")).hexdigest()


def digest_action(event: dict) -> str:
    """The component's own audit digest: sha256 of the canonical tool input."""
    return hashlib.sha256(_canonical(event["tool_input"]).encode("utf-8")).hexdigest()


def session_ref_for(harness: str, profile: str, session_id: str) -> str:
    """The component's session key: sha256 of [harness, profile, session_id].

    An empty ``session_id`` has no key, exactly as in the component's own
    ``assess``: no key means nothing can be correlated and nothing can latch.
    """
    if not session_id:
        return ""
    return hashlib.sha256(
        _canonical([harness, profile, session_id]).encode("utf-8")
    ).hexdigest()


def session_ref(event: dict) -> str:
    """The session key of a normalized event."""
    return session_ref_for(event["harness"], event["profile"], event["session_id"])


def bound_event(event: dict, policy: dict) -> dict:
    """Refuse an event the host must not forward, or return it unchanged.

    The forwarded payload is checked against the component's own limits, so the
    host never sends something the component would reject for size, and never
    truncates content (a shortened prompt would be assessed as if complete).
    """
    size = len(_canonical(event).encode("utf-8"))
    if size > FORWARD_LIMIT:
        raise BoundaryError(E_PAYLOAD_BOUND, f"normalized event {size}B exceeds {FORWARD_LIMIT}B")
    content_bytes = len(event["content"].encode("utf-8"))
    if content_bytes > policy["max_content_bytes"]:
        raise BoundaryError(
            E_PAYLOAD_BOUND,
            f"content {content_bytes}B exceeds policy {policy['max_content_bytes']}B",
        )
    return event


# ---------------------------------------------------------------- component calls


def _run_component(root: Path, argv: list, payload, timeout: int):
    command = [sys.executable, "-I", str(root / "launch.py"), *argv]
    try:
        result = subprocess.run(
            command, input=payload, capture_output=True, timeout=timeout, cwd=str(root)
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoundaryError(E_COMPONENT_FAILED, type(exc).__name__) from exc
    if len(result.stdout) > COMPONENT_STDOUT_BOUND:
        raise BoundaryError(E_COMPONENT_FAILED, "component stdout exceeded the bound")
    if result.returncode != 0:
        raise BoundaryError(E_COMPONENT_FAILED, f"component exit {result.returncode}")
    try:
        return adapter.strict_json(result.stdout)
    except adapter.AdapterError as exc:
        raise BoundaryError(E_COMPONENT_FAILED, f"component output invalid: {exc}") from exc


def verdict_for(root: Path, policy: Path, event: dict) -> dict:
    """Evaluate the normalized event through the component's documented entry."""
    verdict = _run_component(
        root,
        ["check", "--policy", str(policy)],
        _canonical(event).encode("utf-8"),
        HOOK_TIMEOUT_S,
    )
    if not isinstance(verdict, dict) or verdict.get("decision") not in adapter.DECISIONS:
        raise BoundaryError(E_VERDICT_SHAPE, "component returned no usable verdict")
    if type(verdict.get("enforced")) is not bool or not isinstance(verdict.get("id"), str):
        raise BoundaryError(E_VERDICT_SHAPE, "component verdict is missing identity fields")
    return verdict


def outbox(root: Path, policy: Path, limit: int = OUTBOX_SCAN_LIMIT) -> list:
    """The component's pending audit records: the activation evidence source."""
    rows = _run_component(
        root, ["outbox", "--policy", str(policy), "--limit", str(limit)], None, HOOK_TIMEOUT_S
    )
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------- incident


def incident(
    event: dict,
    *,
    native_event: str,
    identity: dict,
    outcome: str,
    verdict,
    enforced: bool,
    decision: str,
    reason_codes: list,
    route: str,
    backend: str,
    capture_id: str = "",
    occurred_at_ms=None,
) -> dict:
    """Build one correlated ``sentinel_incident`` envelope (no raw content).

    The envelope is the manifest's: every field in ``events.envelope_fields`` is
    present, and the correlation keys (``session_id``, ``turn_id``,
    ``tool_call_id``) tie a finding back to the exact host activity. The
    component's verdict identity (``sentinel.event_id``) and its ``session_ref``
    are copied through, so an incident can be joined to the component's own audit
    row.
    """
    body = {
        "kind": INCIDENT_KIND,
        "stage": event["stage"],
        "native_event": native_event,
        "session_id": identity["session_id"],
        "turn_id": identity["turn_id"],
        "tool_call_id": identity["tool_call_id"],
        "outcome": outcome,
        "decision": decision,
    }
    envelope = {
        "event_id": _digest([INCIDENT_KIND, body]),
        "parent_event_id": identity.get("parent_event_id") or None,
        "session_id": identity["session_id"],
        "turn_id": identity["turn_id"],
        "tool_call_id": identity["tool_call_id"],
        "component": COMPONENT_ID,
        "stage": event["stage"],
        "kind": INCIDENT_KIND,
        "origin_workspace": identity.get("workspace", ""),
        "capture_id": capture_id or digest_content(event),
        "occurred_at_ms": _now_ms() if occurred_at_ms is None else int(occurred_at_ms),
        "redaction": REDACTION,
    }
    envelope["native_event"] = native_event
    envelope["outcome"] = outcome
    envelope["sentinel"] = {
        "event_id": (verdict or {}).get("id", ""),
        "decision": decision,
        "enforced": bool(enforced),
        "reason_codes": sorted(reason_codes),
        "route": route,
        "backend": backend,
        "session_ref": session_ref(event) if event["session_id"] else "",
        "content_sha256": digest_content(event),
        "action_sha256": digest_action(event),
        "content_bytes": len(event["content"].encode("utf-8")),
        "tool_name": event["tool_name"],
        "harness": event["harness"],
        "profile": event["profile"],
    }
    envelope["identity"] = {
        "harness": HOST,
        "profile": event["profile"],
        "session_id": identity["session_id"],
        "turn_id": identity["turn_id"],
        "tool_call_id": identity["tool_call_id"],
    }
    return envelope


def incident_path(state_dir) -> Path:
    return Path(state_dir) / "codex-jev-incidents.jsonl"


def append_incident(path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(record) + "\n")


def read_incidents(path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(adapter.strict_json(line))
    return records


# ---------------------------------------------------------------- observe


def observe(
    raw,
    *,
    event_name: str,
    profile: str,
    identity: dict,
    component: Path,
    policy_path: Path,
    policy: dict,
    enforced: bool,
    state_dir,
) -> dict:
    """Normalize, bound, evaluate, and record one native host event."""
    if not isinstance(raw, dict):
        raise BoundaryError(E_PAYLOAD_SHAPE, "native payload must be an object")
    try:
        event = adapter.normalize(event_name, raw, profile)
    except adapter.AdapterError as exc:
        raise BoundaryError(E_PAYLOAD_SHAPE, str(exc)) from exc
    if identity.get("session_id"):
        event["session_id"] = identity["session_id"]
    try:
        bound_event(event, policy)
    except BoundaryError as exc:
        # Fail closed: the host must not forward a payload it cannot bound, so it
        # records the refusal as a REVIEW incident. It vetoes nothing on its own.
        record = incident(
            event,
            native_event=event_name,
            identity=identity,
            outcome="boundary_refusal",
            verdict=None,
            enforced=enforced,
            decision="REVIEW",
            reason_codes=["host_" + exc.code.removeprefix("E_").lower()],
            route="security_review",
            backend="host_boundary",
        )
        append_incident(incident_path(state_dir), record)
        return {
            "event": event,
            "verdict": None,
            "response": {},
            "incident": record,
            "refused": exc.code,
        }

    verdict = verdict_for(component, policy_path, event)
    # The host honors the wired command's rendering; that command runs the same
    # evaluator over the same normalized event, and the translation is pinned to
    # the component by the goldens. Evaluating once keeps the remote budget honest.
    response = adapter.render(event_name, verdict, raw)
    adapter.assert_no_replacement(response)
    record = incident(
        event,
        native_event=event_name,
        identity=identity,
        outcome="observed",
        verdict=verdict,
        enforced=bool(verdict.get("enforced")),
        decision=verdict["decision"],
        reason_codes=list(verdict.get("reason_codes", [])),
        route=verdict.get("route", ""),
        backend=verdict.get("backend", ""),
    )
    append_incident(incident_path(state_dir), record)
    return {
        "event": event,
        "verdict": verdict,
        "response": response,
        "incident": record,
        "refused": "",
    }


# ---------------------------------------------------------------- coverage


def _entries(hooks: dict, event_name: str) -> list:
    value = hooks.get(event_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise BoundaryError(E_HOOKS_SHAPE, f"hooks.{event_name} must be an array")
    return value


def _commands(entry) -> list:
    """Extract the command strings from one hooks.json entry."""
    if not isinstance(entry, dict):
        raise BoundaryError(E_HOOKS_SHAPE, "hook entry must be an object")
    if isinstance(entry.get("command"), str):
        return [entry["command"]]
    nested = entry.get("hooks", [])
    if not isinstance(nested, list):
        raise BoundaryError(E_HOOKS_SHAPE, "hook entry hooks must be an array")
    commands = []
    for item in nested:
        if not isinstance(item, dict):
            raise BoundaryError(E_HOOKS_SHAPE, "nested hook must be an object")
        if isinstance(item.get("command"), str):
            commands.append(item["command"])
    return commands


def _launcher_of(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    for token in argv:
        if token.endswith("launch.py"):
            return token
    return ""


def read_coverage(profile_root, hooks_path=None) -> dict:
    """Read the profile's real hook wiring and derive the effective paths."""
    path = Path(hooks_path) if hooks_path else Path(profile_root) / "hooks.json"
    if not path.is_file():
        raise BoundaryError(E_HOOKS_MISSING, f"no hook wiring at {path}")
    try:
        document = adapter.strict_json(path.read_bytes())
    except OSError as exc:
        raise BoundaryError(E_HOOKS_MISSING, f"hook wiring unreadable: {path.name}") from exc
    except adapter.AdapterError as exc:
        raise BoundaryError(E_HOOKS_SHAPE, f"hook wiring invalid: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise BoundaryError(E_HOOKS_SHAPE, "hook wiring must hold a hooks object")
    hooks = document["hooks"]
    if document.get("disableAllHooks") is True:
        # Native policy disables every hook; nothing is evaluated regardless of
        # what is written below.
        return {"hooks_path": str(path), "disable_all_hooks": True, "paths": {}}
    paths = {}
    for event_name, stage in adapter.EVENTS.items():
        entries = _entries(hooks, event_name)
        commands = []
        matchers = []
        launchers = []
        for entry in entries:
            found = _commands(entry)
            commands.extend(found)
            if isinstance(entry, dict):
                matcher = entry.get("matcher")
                if isinstance(matcher, str) and matcher:
                    matchers.append(matcher)
            launchers.extend(filter(None, (_launcher_of(command) for command in found)))
        reachable = sorted({item for item in launchers if Path(item).is_file()})
        paths[stage] = {
            "stage": stage,
            "native_event": event_name,
            "wired": bool(commands),
            "entries": len(entries),
            "commands": len(commands),
            "matchers": sorted(set(matchers)),
            "launchers": sorted(set(launchers)),
            "reachable_launchers": reachable,
            "reachable": bool(commands) and len(reachable) == len(set(launchers)),
            "covered_tools": covered_tools(matchers) if commands else [],
        }
    return {"hooks_path": str(path), "disable_all_hooks": False, "paths": paths}


def covered_tools(matchers: list) -> list:
    """Derive which tools a matcher set covers: ``*`` means every tool.

    A Codex matcher is a regular expression over tool names. A plain alternation
    of literal names can be enumerated exactly; anything using regex syntax is
    reported as opaque rather than guessed at.
    """
    if not matchers:
        # No matcher key: Codex applies the hook to the event's normal scope.
        return ["*"]
    names = set()
    for matcher in matchers:
        if matcher in (".*", "*", ""):
            return ["*"]
        parts = matcher.split("|")
        if all(TOOL_NAME_RE.fullmatch(part) for part in parts):
            names.update(parts)
        else:
            return [f"<opaque:{matcher}>"]
    return sorted(names)


def bypass_surfaces(coverage: dict, policy: dict, switches: dict, revision_match: bool) -> list:
    """Every observed way a finding can be skipped or an action left ungated."""
    surfaces = []
    if coverage["disable_all_hooks"]:
        surfaces.append(
            {
                "id": "native_disable_all_hooks",
                "kind": "bypass",
                "observable": True,
                "detail": "hooks.disableAllHooks=true in the profile; no event is evaluated.",
            }
        )
    for stage, spec in sorted(coverage["paths"].items()):
        if not spec["wired"]:
            surfaces.append(
                {
                    "id": f"hook_not_wired.{stage}",
                    "kind": "bypass",
                    "observable": True,
                    "detail": f"{spec['native_event']} has no command hook; {stage} is uncovered.",
                }
            )
        elif not spec["reachable"]:
            surfaces.append(
                {
                    "id": f"launcher_unreachable.{stage}",
                    "kind": "bypass",
                    "observable": True,
                    "detail": f"{spec['native_event']} names a launcher that is not a file.",
                }
            )
        if spec["wired"] and spec["covered_tools"] != ["*"]:
            opaque = any(str(tool).startswith("<opaque:") for tool in spec["covered_tools"])
            surfaces.append(
                {
                    "id": (f"matcher_opaque.{stage}" if opaque else f"tools_outside_matcher.{stage}"),
                    "kind": "bypass",
                    "observable": True,
                    "detail": (
                        f"{spec['native_event']} uses matcher {spec['matchers']}; the covered "
                        "tool set cannot be enumerated without guessing"
                        if opaque
                        else f"{spec['native_event']} matches {spec['covered_tools']} only."
                    ),
                }
            )
    surfaces.append(
        {
            "id": "post_tool_replacement_unsupported",
            "kind": "unsupported_replacement",
            "observable": True,
            "detail": (
                "Codex exposes no output-replacement field; the post-tool path returns "
                "additionalContext and the component's subsequent-action latch only."
            ),
        }
    )
    surfaces.append(
        {
            "id": "ingress_scope_is_prompt_only",
            "kind": "bypass",
            "observable": False,
            "detail": (
                "Only the submitted prompt reaches ingress; model-authored and tool text "
                "arrive at the tool_after boundary instead."
            ),
        }
    )
    if policy["backend"] == "local":
        surfaces.append(
            {
                "id": "local_rules_only",
                "kind": "bypass",
                "observable": True,
                "detail": "policy backend=local; the pinned local rules are illustrative, not Jev.",
            }
        )
    surfaces.append(
        {
            "id": "host_trust_unverified",
            "kind": "bypass",
            "observable": True,
            "detail": (
                "Codex requires per-hook trust approval that no file on disk records, so "
                "only a canary through the wired command can show activation."
            ),
        }
    )
    if not revision_match:
        surfaces.append(
            {
                "id": "component_revision_mismatch",
                "kind": "bypass",
                "observable": True,
                "detail": "the resolved component checkout is not the manifest's pinned revision.",
            }
        )
    if not (switches[SHADOW_SWITCH] or switches[ENFORCE_SWITCH]):
        surfaces.append(
            {
                "id": "integration_switch_off",
                "kind": "bypass",
                "observable": True,
                "detail": "sentinel.shadow is off; the host does not call the boundary at all.",
            }
        )
    return surfaces


def render_probe() -> dict:
    """Prove the Codex response shape: no replacement field, ever."""
    synthetic = {
        "id": "0" * 32,
        "decision": "BLOCK",
        "enforced": True,
        "message": "JEV Sentinel: content or action requires security review.",
    }
    emitted = {}
    for event_name in adapter.EVENTS:
        response = adapter.render(event_name, synthetic, {})
        adapter.assert_no_replacement(response)
        emitted[event_name] = sorted(response)
    return {"fields": emitted, "replacement_fields": []}


def canary_payloads() -> list:
    """Three deterministic canaries, one per wired boundary."""
    return [
        ("UserPromptSubmit", {"prompt": f"plumbing canary {adapter.CANARY}"}, "shell"),
        (
            "PreToolUse",
            {"tool_name": "shell", "tool_input": {"command": f"echo {adapter.CANARY}"}},
            "shell",
        ),
        (
            "PostToolUse",
            {"tool_name": "shell", "tool_input": {}, "tool_response": {"stdout": adapter.CANARY}},
            "shell",
        ),
    ]


def canary(
    *,
    component: Path,
    policy_path: Path,
    policy: dict,
    identity: dict,
    state_dir,
    enabled: bool,
) -> dict:
    """Run the three canaries through this boundary and emit correlated incidents."""
    results = []
    envelope_ids = []
    for event_name, raw, _tool in canary_payloads():
        stage = adapter.stage_of(event_name)
        if not enabled:
            results.append({"stage": stage, "native_event": event_name, "skipped": "switch_off"})
            continue
        payload = dict(raw)
        if stage != "ingress":
            payload["session_id"] = identity["session_id"]
        # Each tool boundary gets its own correlated call identity.
        stage_identity = dict(identity)
        if stage != "ingress" and not stage_identity["tool_call_id"]:
            stage_identity["tool_call_id"] = f"{identity['session_id']}-{stage}"
        if stage != "ingress":
            payload["tool_call_id"] = stage_identity["tool_call_id"]
        observed = observe(
            payload,
            event_name=event_name,
            profile=identity["profile"],
            identity=stage_identity,
            component=component,
            policy_path=policy_path,
            policy=policy,
            enforced=policy["mode"] == "enforce",
            state_dir=state_dir,
        )
        verdict = observed["verdict"] or {}
        envelope_ids.append(observed["incident"]["event_id"])
        results.append(
            {
                "stage": stage,
                "native_event": event_name,
                "decision": observed["incident"]["sentinel"]["decision"],
                "reason_codes": observed["incident"]["sentinel"]["reason_codes"],
                "enforced": observed["incident"]["sentinel"]["enforced"],
                "host_response_keys": sorted(observed["response"]),
                "host_vetoed": bool(observed["response"]),
                "sentinel_event_id": verdict.get("id", ""),
                "incident_event_id": observed["incident"]["event_id"],
                "correlated": True,
            }
        )
    complete = enabled and len(envelope_ids) == len(canary_payloads())
    return {
        "schema": COVERAGE_SCHEMA,
        "canaries": results,
        "incident_event_ids": envelope_ids,
        "session_id": identity["session_id"],
        "turn_id": identity["turn_id"],
        "complete": complete,
    }


def _wired_commands(coverage: dict, stage: str) -> list:
    """Re-read the wired commands for one stage from the coverage document."""
    document = adapter.strict_json(Path(coverage["hooks_path"]).read_bytes())
    event_name = coverage["paths"][stage]["native_event"]
    commands = []
    for entry in _entries(document["hooks"], event_name):
        commands.extend(_commands(entry))
    return commands


def _wired_profile(coverage: dict, stage: str) -> str:
    """The ``--profile`` the installer wired, which the audit row key is built on."""
    for command in _wired_commands(coverage, stage):
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        for index, token in enumerate(argv):
            if token == "--profile" and index + 1 < len(argv):
                return argv[index + 1]
    return ""


def probe_activation(
    coverage: dict, *, component: Path, policy_path: Path, policy: dict, nonce: str
) -> dict:
    """Prove effective coverage: run each wired command and find its audit row.

    The wired command is what Codex runs, so this is the only honest activation
    evidence. A shadow-mode response is ``{}``, which is indistinguishable from
    "the hook never ran"; the audit row the component writes is not, so the probe
    matches a row written by this probe and nothing earlier.

    Each probe run carries a unique session identity, so its rows are unique in
    the component's outbox. Without that, a launcher that only echoes ``{}``
    would look active whenever an earlier run had left a matching canary row.
    """
    evidence = {}
    session_id = f"canary-probe-{nonce}"
    for event_name, raw, tool_name in canary_payloads():
        stage = adapter.stage_of(event_name)
        spec = coverage["paths"].get(stage, {})
        if not spec.get("wired") or not spec.get("reachable"):
            evidence[stage] = {"probed": False, "reason": "not_wired_or_unreachable"}
            continue
        payload = dict(raw)
        payload["session_id"] = session_id
        profile = _wired_profile(coverage, stage)
        try:
            event = adapter.normalize(event_name, payload, profile)
        except adapter.AdapterError as exc:
            evidence[stage] = {"probed": False, "reason": str(exc)}
            continue
        expected = digest_content(event)
        expected_ref = session_ref(event)
        responses = []
        for command in _wired_commands(coverage, stage):
            try:
                result = subprocess.run(
                    shlex.split(command),
                    input=_canonical(payload).encode("utf-8"),
                    capture_output=True,
                    timeout=HOOK_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                responses.append({"ok": False, "reason": type(exc).__name__})
                continue
            try:
                parsed = adapter.strict_json(result.stdout)
            except adapter.AdapterError:
                parsed = None
            replaced = []
            if isinstance(parsed, dict):
                try:
                    adapter.assert_no_replacement(parsed)
                except adapter.AdapterError:
                    replaced = list(adapter.REPLACEMENT_FIELDS)
            responses.append(
                {
                    "ok": result.returncode == 0 and isinstance(parsed, dict) and not replaced,
                    "response_keys": sorted(parsed) if isinstance(parsed, dict) else [],
                    "replacement_fields": replaced,
                }
            )
        scanned = outbox(component, policy_path)
        rows = [
            row
            for row in scanned
            if isinstance(row, dict)
            and row.get("content_sha256") == expected
            and row.get("stage") == stage
            and row.get("session_ref") == expected_ref
        ]
        saturated = len(scanned) >= OUTBOX_SCAN_LIMIT
        evidence[stage] = {
            "probed": True,
            "commands": len(responses),
            "responses": responses,
            "audit_rows": len(rows),
            "reason_codes": sorted({code for row in rows for code in row.get("reason_codes", [])}),
            "content_sha256": expected,
            "session_ref": expected_ref,
            "tool_name": tool_name,
            "outbox_scanned": len(scanned),
            "inconclusive": bool(saturated and not rows),
        }
    if not evidence:
        return {"activated": False, "basis": "none", "policy_mode": policy["mode"], "evidence": {}}
    inconclusive = any(item.get("inconclusive") for item in evidence.values())
    activated = not inconclusive and all(
        item.get("audit_rows", 0) >= 1 for item in evidence.values()
    )
    return {
        "activated": activated,
        "basis": "probe_canary" if activated else ("inconclusive" if inconclusive else "none"),
        "policy_mode": policy["mode"],
        "evidence": evidence,
    }


def coverage_report(
    *,
    manifest,
    component: Path,
    profile_root,
    policy_path: Path,
    hooks_path=None,
    switches: dict,
    probe: bool,
    nonce: str = "",
) -> dict:
    policy = load_policy(policy_path)
    pinned = component_index(manifest)["revision"]
    revision = component_revision(component)
    coverage = read_coverage(profile_root, hooks_path)
    paths = coverage["paths"]
    wired = sorted(stage for stage, spec in paths.items() if spec.get("wired"))
    reachable = sorted(stage for stage, spec in paths.items() if spec.get("reachable"))
    # Only the tool boundaries carry a matcher; ingress covers the prompt.
    covered = sorted(
        {
            tool
            for stage, spec in paths.items()
            if stage in TOOL_STAGES and spec.get("wired")
            for tool in spec.get("covered_tools", [])
        }
    )
    report = {
        "schema": COVERAGE_SCHEMA,
        "host": HOST,
        "adapter": ADAPTER,
        "component": {
            "id": COMPONENT_ID,
            "root": str(component),
            "pinned_revision": pinned,
            "resolved_revision": revision,
            "revision_match": revision == pinned,
            "version": component_index(manifest).get("version", ""),
        },
        "policy": {
            "path": str(policy_path),
            "mode": policy["mode"],
            "backend": policy["backend"],
            "max_content_bytes": policy["max_content_bytes"],
            "forward_limit_bytes": FORWARD_LIMIT,
        },
        "switches": switches,
        "hooks_path": coverage["hooks_path"],
        "disable_all_hooks": coverage["disable_all_hooks"],
        "event_paths": paths,
        "wired_stages": wired,
        "reachable_stages": reachable,
        "covered_tools": ["*"] if "*" in covered else covered,
        "shadow": {
            "effective": bool(paths) and len(reachable) == len(paths) and policy["mode"] == "shadow",
            "observational": policy["mode"] == "shadow",
            "detail": (
                "shadow records every finding and returns an empty host response, so a "
                "shadow finding never vetoes an action"
            ),
        },
        "response_platform": render_probe(),
        "activation": {
            "activated": False,
            "basis": "none",
            "detail": (
                "a resolvable launcher and a matching revision are necessary but not "
                "sufficient; run --probe to show a canary reaching the wired command"
            ),
        },
    }
    report["bypass_surfaces"] = bypass_surfaces(
        coverage, policy, switches, report["component"]["revision_match"]
    )
    if probe:
        report["activation"] = probe_activation(
            coverage, component=component, policy_path=policy_path, policy=policy, nonce=nonce
        )
    return report


# ---------------------------------------------------------------- CLI


def _identity(args, profile: str) -> dict:
    return {
        "profile": profile,
        "session_id": args.session,
        "turn_id": args.turn,
        "tool_call_id": getattr(args, "tool_call_id", ""),
        "workspace": getattr(args, "workspace", ""),
        "parent_event_id": "",
    }


def _read_request(path):
    raw = Path(path).read_text(encoding="utf-8") if path and path != "-" else sys.stdin.read()
    return adapter.strict_json(raw)


def _resolve_event_name(value: str) -> str:
    if value in adapter.EVENTS:
        return value
    for event_name, stage in adapter.EVENTS.items():
        if stage == value:
            return event_name
    raise BoundaryError(E_EVENT_NAME, f"unknown event or stage {value!r}")


def _default_policy() -> Path:
    env = os.environ.get("JEV_SENTINEL_POLICY")
    return Path(env) if env else Path.home() / ".jev-sentinel" / "policy.json"


def _default_profile_root() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _add_component_args(node):
    node.add_argument("--component", default="", help="pinned jev-sentinel checkout")
    node.add_argument("--policy", default="", help="sentinel policy.json")
    node.add_argument("--json", action="store_true")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cov = sub.add_parser("coverage", help="report effective hook coverage")
    _add_component_args(cov)
    cov.add_argument("--profile-root", default="", help="Codex profile root holding hooks.json")
    cov.add_argument("--hooks", default="", help="explicit hooks.json path")
    cov.add_argument("--probe", action="store_true", help="run the canaries through the wired commands")
    cov.add_argument("--nonce", default="", help="probe run identity (default: random)")

    can = sub.add_parser("canary", help="run the deterministic canaries")
    _add_component_args(can)
    can.add_argument("--profile", default="codex-jev-canary")
    can.add_argument("--session", default="canary-session")
    can.add_argument("--turn", default="canary-turn-1")
    can.add_argument("--workspace", default="")
    can.add_argument("--state-dir", default="")

    obs = sub.add_parser("observe", help="normalize, bound, evaluate and record one event")
    _add_component_args(obs)
    obs.add_argument("--event", required=True, help="native event name or boundary stage")
    obs.add_argument("--profile", default="default")
    obs.add_argument("--session", default="")
    obs.add_argument("--turn", default="")
    obs.add_argument("--tool-call-id", default="")
    obs.add_argument("--workspace", default="")
    obs.add_argument("--state-dir", default="")
    obs.add_argument("--request", default="-", help="native payload JSON file, or - for stdin")

    args = parser.parse_args(argv)
    manifest = load_manifest()
    switches = feature_switches(manifest)
    policy_path = Path(args.policy) if args.policy else _default_policy()
    try:
        component = resolve_component(args.component, manifest=manifest)

        if args.command == "coverage":
            profile_root = Path(args.profile_root) if args.profile_root else _default_profile_root()
            report = coverage_report(
                manifest=manifest,
                component=component,
                profile_root=profile_root,
                policy_path=policy_path,
                hooks_path=Path(args.hooks) if args.hooks else None,
                switches=switches,
                probe=args.probe,
                nonce=args.nonce or secrets.token_hex(8),
            )
            print(json.dumps(report, indent=2 if args.json else None))
            return 0 if (not args.probe or report["activation"]["activated"]) else 1

        policy = load_policy(policy_path)
        state_dir = Path(args.state_dir) if args.state_dir else policy_path.parent

        if args.command == "canary":
            identity = _identity(args, args.profile)
            result = canary(
                component=component,
                policy_path=policy_path,
                policy=policy,
                identity=identity,
                state_dir=state_dir,
                enabled=switches[SHADOW_SWITCH] or switches[ENFORCE_SWITCH],
            )
            print(json.dumps(result, indent=2 if args.json else None))
            return 0 if result["complete"] else 1

        event_name = _resolve_event_name(args.event)
        raw = _read_request(args.request)
        identity = _identity(args, args.profile)
        result = observe(
            raw,
            event_name=event_name,
            profile=args.profile,
            identity=identity,
            component=component,
            policy_path=policy_path,
            policy=policy,
            enforced=policy["mode"] == "enforce",
            state_dir=state_dir,
        )
        print(json.dumps(result, indent=2 if args.json else None))
        return 1 if result["refused"] else 0
    except BoundaryError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
