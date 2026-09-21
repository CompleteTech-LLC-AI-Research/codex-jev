//! The native JEV projection boundary for the outgoing Responses payload.
//!
//! JEV prunes the *outgoing view* and never the canonical transcript. This
//! module owns the host half of that boundary (the manifest's host-owned
//! `jev_bus_call_site`): when a projection switch is enabled, the request copy
//! is handed to the repository's bus adapter (`jev/scripts/bus_boundary.py`)
//! exactly once and the returned view replaces the payload. Nothing else about
//! the request is host-visible, and the persisted history is never touched.
//!
//! Failure policy: every failure - a disabled switch, an unconfigured chain, a
//! missing adapter, a timeout, a non-zero exit, output that is not the expected
//! object, or a changed item count - leaves the caller's input exactly as it
//! was, which is the stage-input fallback the bus contract requires. A refusal
//! is never silently substituted with an approximation.
//!
//! One shrink is not a failure: an approved prose view may remove whole items,
//! which is how the model-visible context is reduced by items and not only by
//! bytes. The host accepts that only for a view the operator configured for this
//! path (`JEV_BUS_VIEW`), when the adapter's own report names exactly the
//! positions the array lost, the resulting array is the incoming array minus
//! those positions in order, and every removed item is assistant prose the host
//! itself recognizes as removable. An addition, a reorder, a removal the report
//! does not account for, and a removal with no configured view are all refused.
//! The host never re-derives *which* prose is approvable: that policy stays with
//! the carrier (`fabric_views.py`), which owns the view.
//!
//! The boundary is resolved from the environment so the isolated profile owns
//! the wiring:
//!
//! * `JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS` / `JEV_SWITCH_PROJECTION_FABRIC_VIEWS`
//!   enable the stages; anything other than `1` is off.
//! * `JEV_BUS_ADAPTER` is the adapter path and `JEV_BUS_PYTHON` its interpreter.
//! * `JEV_BUS_STAGE_DEDUP` / `JEV_BUS_STAGE_FABRIC_VIEW` carry one stage command
//!   each, because a stage with no command must not be registered.
//! * `JEV_BUS_VIEW` is the approved view the carrier filters with; with none
//!   configured an item removal is refused and the projection stays byte-only.
//! * `JEV_BUS_TIMEOUT_MS` bounds one invocation; `JEV_BUS_WORKSPACE` labels it.

use std::collections::BTreeSet;
use std::io::Read;
use std::io::Write;
use std::path::PathBuf;
use std::process::Child;
use std::process::Command;
use std::process::ExitStatus;
use std::process::Stdio;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;
use std::time::Instant;

use codex_protocol::models::ContentItem;
use codex_protocol::models::ResponseItem;
use serde_json::Value;
use serde_json::json;

/// Duplicate-read dedup, the jev-bus stage at order 100.
const DEDUP_SWITCH: &str = "JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS";
/// Approved Fabric prose view, the jev-bus stage at order 200.
const FABRIC_VIEWS_SWITCH: &str = "JEV_SWITCH_PROJECTION_FABRIC_VIEWS";

const ADAPTER_ENV: &str = "JEV_BUS_ADAPTER";
const PYTHON_ENV: &str = "JEV_BUS_PYTHON";
const TIMEOUT_ENV: &str = "JEV_BUS_TIMEOUT_MS";
const WORKSPACE_ENV: &str = "JEV_BUS_WORKSPACE";
const VIEW_ENV: &str = "JEV_BUS_VIEW";
const STAGE_ENV_PREFIX: &str = "JEV_BUS_STAGE_";

const DEFAULT_PYTHON: &str = "python3";
const DEFAULT_TIMEOUT_MS: u64 = 15_000;
const MAX_TIMEOUT_MS: u64 = 120_000;
/// The adapter refuses a payload above its own wire bound, so a request that
/// could never be projected is not handed to a subprocess at all.
const MAX_INPUT_BYTES: usize = 8 * 1024 * 1024;
/// Grace period for draining the child's pipes once it has exited.
const OUTPUT_GRACE: Duration = Duration::from_secs(2);
const POLL_INTERVAL: Duration = Duration::from_millis(2);
const STDERR_LIMIT: usize = 512;

/// The bus stages the host may register, in the order the bus must run them.
const STAGES: [(&str, &str); 2] = [
    ("dedup", DEDUP_SWITCH),
    ("fabric_view", FABRIC_VIEWS_SWITCH),
];

/// One resolved boundary: what to run, for how long, and whether to run at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct JevBusConfig {
    enabled: bool,
    adapter: PathBuf,
    python: String,
    timeout: Duration,
    stages: Vec<(String, String)>,
    /// The approved view the carrier filters with, when the profile supplies one.
    view: Option<PathBuf>,
}

impl JevBusConfig {
    /// Resolve the boundary contract from the environment.
    ///
    /// A switch that is unset or not exactly `1` is off, and an enabled stage
    /// with no command is not registered. Projection therefore stays disabled
    /// until the profile supplies both a switch and a chain, so a half
    /// configured host passes the request through instead of invoking a chain
    /// it cannot describe.
    pub(crate) fn from_env() -> Self {
        let mut stages = Vec::new();
        for (id, switch) in STAGES {
            if !switch_enabled(std::env::var(switch).ok().as_deref()) {
                continue;
            }
            let key = format!("{STAGE_ENV_PREFIX}{}", id.to_uppercase());
            if let Some(command) = non_empty(std::env::var(key).ok()) {
                stages.push((id.to_string(), command));
            }
        }
        let adapter = non_empty(std::env::var(ADAPTER_ENV).ok()).unwrap_or_default();
        let enabled = !stages.is_empty() && !adapter.is_empty();
        Self {
            enabled,
            adapter: PathBuf::from(adapter),
            python: non_empty(std::env::var(PYTHON_ENV).ok())
                .unwrap_or_else(|| DEFAULT_PYTHON.to_string()),
            timeout: Duration::from_millis(resolve_timeout_ms(
                std::env::var(TIMEOUT_ENV).ok().as_deref(),
            )),
            stages,
            view: non_empty(std::env::var(VIEW_ENV).ok()).map(PathBuf::from),
        }
    }
}

/// Where the projection happened, as the bus contract labels it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct ProjectionContext {
    pub(crate) session_id: String,
    pub(crate) turn_id: String,
    pub(crate) workspace: String,
}

impl ProjectionContext {
    /// Build the context for one request from the turn metadata.
    ///
    /// The host has no workspace path at this boundary, so the label comes from
    /// the profile that launched the host.
    pub(crate) fn from_responses_metadata(metadata: &crate::CodexResponsesMetadata) -> Self {
        Self {
            session_id: metadata.session_id.clone(),
            turn_id: metadata.turn_id.clone().unwrap_or_default(),
            workspace: non_empty(std::env::var(WORKSPACE_ENV).ok()).unwrap_or_default(),
        }
    }
}

/// Project one outgoing request copy, or return it unchanged.
///
/// The caller passes the request copy, not the transcript, and receives the
/// array to send. This is the only entry point: exactly one invocation happens
/// per outgoing payload.
pub(crate) fn project_input(
    input: Vec<ResponseItem>,
    context: &ProjectionContext,
) -> Vec<ResponseItem> {
    project_with(&JevBusConfig::from_env(), input, context)
}

fn project_with(
    config: &JevBusConfig,
    input: Vec<ResponseItem>,
    context: &ProjectionContext,
) -> Vec<ResponseItem> {
    if !config.enabled {
        return input;
    }
    match request_projection(config, &input, context) {
        Ok(projected) => projected,
        Err(reason) => {
            tracing::debug!(
                target: "codex_core::jev_bus",
                reason,
                "jev-bus projection skipped; the outgoing request is unchanged"
            );
            input
        }
    }
}

/// Run the adapter once and return the projected view.
///
/// Every rejection below is a contract violation, so the caller falls back to
/// the input it already has rather than sending something unverified.
fn request_projection(
    config: &JevBusConfig,
    input: &[ResponseItem],
    context: &ProjectionContext,
) -> Result<Vec<ResponseItem>, &'static str> {
    let payload = serde_json::to_vec(&json!({ "input": input })).map_err(|_| "serialize-failed")?;
    if payload.len() > MAX_INPUT_BYTES {
        return Err("wire-bound");
    }
    let output = run_adapter(config, context, &payload)?;
    let parsed: Value = serde_json::from_slice(&output).map_err(|_| "malformed-output")?;
    let projected = parsed
        .get("request")
        .and_then(|request| request.get("input"))
        .ok_or("missing-request-input")?;
    let projected: Vec<ResponseItem> =
        serde_json::from_value(projected.clone()).map_err(|_| "input-shape")?;
    // The adapter proves order and membership; the host re-checks the property
    // it depends on, so a projection can never silently add or drop an item.
    if projected.len() != input.len() {
        // A shorter array is an approved removal, and only a view the operator
        // configured can authorize one; a longer array is an addition and is
        // refused outright.
        if config.view.is_none() || projected.len() > input.len() {
            return Err("item-count");
        }
        accept_removals(input, &projected, &parsed)?;
    }
    Ok(projected)
}

/// Accept a shorter array only when the report accounts for exactly the loss.
///
/// Every refusal below returns the caller's own array through the fallback in
/// [`project_with`], so an adapter that misreports a removal cannot make an item
/// disappear on its own word.
fn accept_removals(
    input: &[ResponseItem],
    projected: &[ResponseItem],
    parsed: &Value,
) -> Result<(), &'static str> {
    let removed = view_removed_indices(parsed, input.len())?;
    if removed.is_empty() || input.len() - removed.len() != projected.len() {
        return Err("item-count");
    }
    // Only assistant prose may leave, checked against the *incoming* array: the
    // carrier owns which prose is approvable, the host owns what may be lost.
    for index in &removed {
        if !is_removable_prose(&input[*index]) {
            return Err("item-count");
        }
    }
    if *projected != without_indices(input, &removed) {
        return Err("item-count");
    }
    Ok(())
}

/// The positions the adapter's report marks as removed by the approved view.
///
/// An index that is out of range or named twice is refused rather than ignored:
/// the report is the only place a removal is accounted for.
fn view_removed_indices(parsed: &Value, items: usize) -> Result<BTreeSet<usize>, &'static str> {
    let entries = parsed
        .get("report")
        .and_then(|report| report.get("view"))
        .and_then(|view| view.get("removed"))
        .and_then(Value::as_array)
        .ok_or("item-count")?;
    let mut removed = BTreeSet::new();
    for entry in entries {
        let index = entry
            .get("index")
            .and_then(Value::as_u64)
            .and_then(|index| usize::try_from(index).ok())
            .ok_or("item-count")?;
        if index >= items || !removed.insert(index) {
            return Err("item-count");
        }
    }
    Ok(removed)
}

/// The incoming array with the given positions removed, in order.
fn without_indices(input: &[ResponseItem], removed: &BTreeSet<usize>) -> Vec<ResponseItem> {
    input
        .iter()
        .enumerate()
        .filter(|(index, _)| !removed.contains(index))
        .map(|(_, item)| item.clone())
        .collect()
}

/// Whether the host itself recognizes an item as standalone assistant prose.
///
/// This is deliberately narrower than the carrier's eligibility policy - it does
/// not model the protected recent tail or the constraint guard, which the host
/// cannot re-derive - so it bounds what may leave rather than deciding what is
/// approvable. Anything carrying a tool call, reasoning, an image, or audio is
/// never removable here.
fn is_removable_prose(item: &ResponseItem) -> bool {
    let ResponseItem::Message { role, content, .. } = item else {
        return false;
    };
    if role != "assistant" {
        return false;
    }
    let mut text = String::new();
    for part in content {
        let part = match part {
            ContentItem::InputText { text } | ContentItem::OutputText { text } => text,
            ContentItem::InputImage { .. } | ContentItem::InputAudio { .. } => return false,
        };
        if !text.is_empty() {
            text.push('\n');
        }
        text.push_str(part);
    }
    !text.trim().is_empty()
}

/// Invoke the adapter over its documented CLI and return its stdout.
fn run_adapter(
    config: &JevBusConfig,
    context: &ProjectionContext,
    payload: &[u8],
) -> Result<Vec<u8>, &'static str> {
    let mut command = Command::new(&config.python);
    command
        .arg(&config.adapter)
        .arg("apply")
        .arg("--request")
        .arg("-")
        .arg("--json")
        .arg("--session")
        .arg(&context.session_id)
        .arg("--turn")
        .arg(&context.turn_id)
        .arg("--workspace")
        .arg(&context.workspace);
    for (id, argv) in &config.stages {
        command.arg("--stage").arg(format!("{id}={argv}"));
    }
    if let Some(view) = &config.view {
        command.arg("--view").arg(view);
    }
    // The adapter reads the same `JEV_SWITCH_*` contract, so the environment is
    // inherited deliberately.
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| "adapter-spawn")?;

    let mut stdin = child.stdin.take().ok_or("adapter-stdin")?;
    let payload = payload.to_vec();
    let writer = thread::spawn(move || {
        // A refusal can close the pipe early; that is the adapter's answer, not
        // an error of its own.
        let _ = stdin.write_all(&payload);
        let _ = stdin.flush();
    });
    let mut stdout = child.stdout.take().ok_or("adapter-stdout")?;
    let (stdout_tx, stdout_rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout.read_to_end(&mut buffer);
        let _ = stdout_tx.send(buffer);
    });
    let stderr = child.stderr.take();
    let stderr_reader = stderr.map(spawn_stderr_reader);

    let status = wait_for_exit(&mut child, config.timeout);
    // Reap before surrendering the pipes: a killed child closes them, which
    // releases both readers.
    let _ = writer.join();
    let status = match status {
        Ok(status) if status.success() => status,
        Ok(_status) => {
            log_stderr(stderr_reader, "adapter-failed");
            return Err("adapter-failed");
        }
        Err(reason) => {
            log_stderr(stderr_reader, reason);
            return Err(reason);
        }
    };
    let _ = status;
    let output = stdout_rx
        .recv_timeout(OUTPUT_GRACE)
        .map_err(|_| "adapter-output-timeout")?;
    drop(reader);
    Ok(output)
}

fn spawn_stderr_reader(mut stderr: std::process::ChildStderr) -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut buffer = String::new();
        let _ = stderr.read_to_string(&mut buffer);
        let _ = tx.send(buffer);
    });
    rx
}

fn log_stderr(reader: Option<mpsc::Receiver<String>>, reason: &str) {
    let Some(reader) = reader else {
        return;
    };
    if let Ok(stderr) = reader.recv_timeout(OUTPUT_GRACE) {
        let stderr = stderr.trim();
        if !stderr.is_empty() {
            let detail: String = stderr.chars().take(STDERR_LIMIT).collect();
            tracing::debug!(
                target: "codex_core::jev_bus",
                reason,
                detail,
                "jev-bus adapter reported a refusal"
            );
        }
    }
}

/// Wait for the adapter, killing it if it exceeds the deadline.
///
/// The whole chain is bounded here, so a wedged component can never hold a turn
/// open; a timeout is the stage-input fallback, not a retry.
fn wait_for_exit(child: &mut Child, timeout: Duration) -> Result<ExitStatus, &'static str> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("adapter-timeout");
                }
                thread::sleep(POLL_INTERVAL);
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("adapter-wait");
            }
        }
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|value| !value.is_empty())
}

/// A switch is on only for the exact contract value.
fn switch_enabled(raw: Option<&str>) -> bool {
    raw == Some("1")
}

/// Resolve the invocation deadline, bounded and never unbounded.
fn resolve_timeout_ms(raw: Option<&str>) -> u64 {
    raw.and_then(|value| value.trim().parse::<u64>().ok())
        .map(|value| value.clamp(1, MAX_TIMEOUT_MS))
        .unwrap_or(DEFAULT_TIMEOUT_MS)
}

#[cfg(test)]
#[path = "jev_bus_tests.rs"]
mod tests;
