use super::*;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::fs;
use std::path::PathBuf;

/// A deterministic adapter stand-in that speaks the documented CLI over stdio.
///
/// It records every invocation, then behaves according to a `mode` file beside
/// it, so the host-side fallback paths can be exercised without a component.
const STUB: &str = r#"import json, pathlib, sys, time

here = pathlib.Path(__file__).resolve().parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "mutate"
with (here / "calls").open("a", encoding="utf-8") as calls:
    calls.write("1\n")

payload = json.load(sys.stdin)
items = payload["input"]

if mode == "sleep":
    time.sleep(5)
    mode = "mutate"
if mode == "fail":
    sys.stderr.write("refused\n")
    sys.exit(1)
if mode == "garbage":
    sys.stdout.write("not json")
    sys.exit(0)
if mode == "drop":
    items = items[1:]
if mode.startswith("remove"):
    # `remove:<index>` drops one item and reports it as a view removal;
    # `remove-mismatch:<index>` reports a different position than it dropped;
    # `remove-overclaim:<index>` reports one more removal than the array lost.
    _, _, spec = mode.partition(":")
    index = int(spec)
    report_removed = [{"index": index}]
    if mode.startswith("remove-overclaim"):
        report_removed.append({"index": index + 1})
    elif mode.startswith("remove-mismatch"):
        report_removed = [{"index": 0}]
        index = index if index != 0 else 1
    items = items[:index] + items[index + 1 :]
    json.dump(
        {
            "request": {"input": items},
            "report": {"host": "codex", "view": {"removed": report_removed}},
        },
        sys.stdout,
    )
    sys.exit(0)
if mode == "mutate":
    for item in items:
        if item.get("type") == "function_call_output":
            item["output"] = "REDACTED"
            break

json.dump({"request": {"input": items}, "report": {"host": "codex"}}, sys.stdout)
"#;

struct Fixture {
    _dir: tempfile::TempDir,
    config: JevBusConfig,
    calls: PathBuf,
}

impl Fixture {
    fn new(mode: &str, timeout: Duration) -> Self {
        let dir = tempfile::tempdir().expect("temp dir");
        fs::write(dir.path().join("stub.py"), STUB).expect("write the adapter stand-in");
        fs::write(dir.path().join("mode"), mode).expect("write the mode");
        let calls = dir.path().join("calls");
        let config = JevBusConfig {
            enabled: true,
            adapter: dir.path().join("stub.py"),
            python: "python3".to_string(),
            timeout,
            stages: vec![("dedup".to_string(), "stub-stage".to_string())],
            view: None,
        };
        Self {
            _dir: dir,
            config,
            calls,
        }
    }

    /// The same boundary, with an approved view configured for the host path.
    fn with_view(mode: &str, timeout: Duration) -> Self {
        let mut fixture = Self::new(mode, timeout);
        let view = fixture._dir.path().join("view.json");
        fs::write(
            &view,
            r#"{"kind":"approved-view","version":"jev-view.v1","keys":[]}"#,
        )
        .expect("write the approved view");
        fixture.config.view = Some(view);
        fixture
    }

    fn invocations(&self) -> usize {
        fs::read_to_string(&self.calls)
            .map(|calls| calls.lines().count())
            .unwrap_or(0)
    }
}

fn sample_input() -> Vec<ResponseItem> {
    serde_json::from_value(json!([
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "name": "read_file",
            "arguments": "{\"path\":\"a\"}",
            "call_id": "call_1",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "FILE A BODY"},
        {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "think"}],
            "encrypted_content": null,
        },
    ]))
    .expect("sample is a valid wire request")
}

/// A request whose first item is standalone assistant prose, so it is the one
/// item a removal is allowed to take.
fn prose_input() -> Vec<ResponseItem> {
    serde_json::from_value(json!([
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "chatter"}],
        },
        {
            "type": "function_call",
            "name": "read_file",
            "arguments": "{\"path\":\"a\"}",
            "call_id": "call_1",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "FILE A BODY"},
        {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "think"}],
            "encrypted_content": null,
        },
    ]))
    .expect("prose sample is a valid wire request")
}

fn output_body(items: &[ResponseItem]) -> Option<String> {
    items.iter().find_map(|item| match item {
        ResponseItem::FunctionCallOutput { output, .. } => output.body.to_text(),
        _ => None,
    })
}

fn item_value(item: &ResponseItem) -> Value {
    serde_json::to_value(item).expect("an item serializes")
}

#[test]
fn disabled_boundary_never_invokes_the_adapter() {
    let fixture = Fixture::new("mutate", Duration::from_secs(5));
    let config = JevBusConfig {
        enabled: false,
        ..fixture.config.clone()
    };
    let input = sample_input();

    let outgoing = project_with(&config, input.clone(), &ProjectionContext::default());

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 0);
}

#[test]
fn projection_replaces_only_the_outgoing_view() {
    let fixture = Fixture::new("mutate", Duration::from_secs(5));
    let input = sample_input();
    let canonical = input.clone();

    let outgoing = project_with(&fixture.config, input, &ProjectionContext::default());

    assert_eq!(output_body(&outgoing).as_deref(), Some("REDACTED"));
    assert_eq!(fixture.invocations(), 1, "the adapter runs exactly once");
    assert_eq!(outgoing.len(), canonical.len());
    // The canonical transcript the caller handed over is untouched, and the
    // opaque shapes the boundary must not touch are byte-identical.
    assert_eq!(output_body(&canonical).as_deref(), Some("FILE A BODY"));
    assert_eq!(item_value(&outgoing[3]), item_value(&canonical[3]));
    assert_eq!(item_value(&outgoing[0]), item_value(&canonical[0]));
}

#[test]
fn adapter_failure_falls_back_to_the_stage_input() {
    let fixture = Fixture::new("fail", Duration::from_secs(5));
    let input = sample_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn adapter_timeout_falls_back_to_the_stage_input() {
    // The deadline must outlast interpreter startup, or the child can be killed
    // before it records the invocation and the assertion below measures the
    // machine's load instead of the host's fallback. It stays far below the
    // stub's own sleep, so this still exercises the deadline path.
    let fixture = Fixture::new("sleep", Duration::from_secs(2));
    let input = sample_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn malformed_output_falls_back_to_the_stage_input() {
    let fixture = Fixture::new("garbage", Duration::from_secs(5));
    let input = sample_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_changed_item_count_is_refused() {
    let fixture = Fixture::new("drop", Duration::from_secs(5));
    let input = sample_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn an_approved_removal_is_applied_when_a_view_is_configured() {
    // The carrier removed the prose item it was approved to remove; the host
    // accepts the shorter array because the report accounts for exactly that
    // position and the item that left is assistant prose.
    let fixture = Fixture::with_view("remove:0", Duration::from_secs(5));
    let input = prose_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input[1..].to_vec());
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_removal_with_no_configured_view_is_refused() {
    // The same adapter answer, with no view configured for the host path: the
    // projection stays byte-only and the incoming payload is used verbatim.
    let fixture = Fixture::new("remove:0", Duration::from_secs(5));
    let input = prose_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_removal_the_report_does_not_account_for_is_refused() {
    // The array lost one position while the report names another, so the host
    // cannot tie the loss to the view and refuses it.
    let fixture = Fixture::with_view("remove-mismatch:2", Duration::from_secs(5));
    let input = prose_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_report_naming_more_removals_than_the_array_lost_is_refused() {
    let fixture = Fixture::with_view("remove-overclaim:0", Duration::from_secs(5));
    let input = prose_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_removal_of_an_item_that_is_not_assistant_prose_is_refused() {
    // Reasoning is not prose however well the report accounts for it: the view
    // may reduce assistant prose, and native compaction keeps working.
    let fixture = Fixture::with_view("remove:3", Duration::from_secs(5));
    let input = prose_input();

    let outgoing = project_with(
        &fixture.config,
        input.clone(),
        &ProjectionContext::default(),
    );

    assert_eq!(outgoing, input);
    assert_eq!(fixture.invocations(), 1);
}

#[test]
fn a_missing_adapter_falls_back_to_the_stage_input() {
    let config = JevBusConfig {
        enabled: true,
        adapter: PathBuf::from("/nonexistent/jev-bus-adapter.py"),
        python: "python3".to_string(),
        timeout: Duration::from_secs(5),
        stages: vec![("dedup".to_string(), "stub-stage".to_string())],
        view: None,
    };
    let input = sample_input();

    let outgoing = project_with(&config, input.clone(), &ProjectionContext::default());

    assert_eq!(outgoing, input);
}

#[test]
fn switches_are_on_only_for_the_contract_value() {
    assert!(switch_enabled(Some("1")));
    assert!(!switch_enabled(Some("0")));
    assert!(!switch_enabled(Some("true")));
    assert!(!switch_enabled(Some("")));
    assert!(!switch_enabled(None));
}

#[test]
fn the_deadline_is_always_bounded() {
    assert_eq!(resolve_timeout_ms(None), DEFAULT_TIMEOUT_MS);
    assert_eq!(resolve_timeout_ms(Some("2500")), 2_500);
    assert_eq!(resolve_timeout_ms(Some(" 40 ")), 40);
    // Unreadable and out-of-range values fall back instead of running unbounded.
    assert_eq!(resolve_timeout_ms(Some("soon")), DEFAULT_TIMEOUT_MS);
    assert_eq!(resolve_timeout_ms(Some("0")), 1);
    assert_eq!(resolve_timeout_ms(Some("600000")), MAX_TIMEOUT_MS);
}
