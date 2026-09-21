//! Opt-in JEV preflight inside the existing synchronous Guardian attempt.
//! Native source adapter for openai/codex c45ea25ffb72d5f7324489d824d0c677283aa0b4,
//! ported into this repository's pinned host base. The guarded `guardian/mod.rs` and
//! `guardian/review_request.rs` blobs are identical at both revisions, so the port
//! required no source reconciliation beyond module placement.
//! No contributor, sandbox, permission, hook-precedence or approval-cache changes.

use std::fmt::Write as _;
use std::path::PathBuf;
use std::process::Stdio;

use codex_guardian_reviewer::GuardianAssessment;
use codex_guardian_reviewer::GuardianReviewOutcome;
use codex_protocol::config_types::ApprovalsReviewer;
use codex_protocol::protocol::GuardianAssessmentOutcome;
use codex_protocol::protocol::GuardianRiskLevel;
use codex_protocol::protocol::GuardianUserAuthorization;
use serde_json::Value;
use sha2::Digest;
use sha2::Sha256;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWriteExt;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use super::ApprovalRequestReasons;
use super::GuardianApprovalRequest;
use super::GuardianReviewContext;
use super::GuardianReviewOptions;
use crate::context::GuardianReviewEvidence;
use crate::session::session::Session;

const MAX_WIRE_BYTES: u64 = 262_144;
const PREFLIGHT_BUDGET: std::time::Duration = std::time::Duration::from_millis(2_000);

/// The declared switch that gates this phase (manifest feature `approval.preflight`).
const PREFLIGHT_SWITCH: &str = "approval.preflight";

/// The question set the approved policy was calibrated against, as the pinned component
/// computes it (`jev_approval.questions.QUESTION_HASH`, revision `0b931ee2`). Recorded in
/// the manifest's adapter record and cross-checked by the required CI lane, because the
/// host cannot recompute a hash over question text it does not carry.
const APPROVED_QUESTION_HASH: &str =
    "cf231ce0f334b53ddbeea0c25bc1e83e68c168b9ee641386d8720349f4f8c6ed";

/// `1` is the only value that turns a declared switch on (CONTRACTS.md C10).
fn switch_value_on(value: Option<&str>) -> bool {
    value == Some("1")
}

/// The declared switch key for a feature: `approval.preflight` -> `JEV_SWITCH_APPROVAL_PREFLIGHT`.
fn switch_key(feature: &str) -> String {
    format!("JEV_SWITCH_{}", feature.to_uppercase().replace('.', "_"))
}

/// Phases 2-6 read the declared switch contract and nothing else, so an absent,
/// empty, or non-`1` value leaves the integration inert rather than guessing.
fn switch_on(feature: &str) -> bool {
    switch_value_on(std::env::var(switch_key(feature)).ok().as_deref())
}

fn absolute_file_env(key: &str) -> Option<PathBuf> {
    let path = PathBuf::from(std::env::var_os(key)?);
    (path.is_absolute() && path.is_file()).then_some(path)
}

/// Why a review attempt is not eligible for JEV preflight.
///
/// Every variant is a deliberate hand-back to the host's existing route rather
/// than an error: [`ineligibility`] returning `Some` means Guardian performs the
/// inference exactly as it did before this module existed. Nothing here grants,
/// widens, or records permission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Ineligible {
    /// `JEV_SWITCH_APPROVAL_PREFLIGHT` is not `1`.
    SwitchOff,
    /// The trusted runtime and bounded transport are not fully configured.
    RuntimeIncomplete,
    /// The attempt was cancelled before it started.
    Cancelled,
    /// The turn requires Guardian, so no preflight judgement may replace it.
    MandatoryReview,
    /// The extension asked to escalate to the synchronous reviewer.
    SynchronousReviewRequired,
    /// A retry must return to the reviewer that produced the original judgement.
    Retry,
    /// This action class, or the permissions it asks for, stays Guardian-owned.
    UnsupportedAction,
}

/// The inputs that decide eligibility, kept separate from the live session so
/// every disabled and ineligible path can be asserted directly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Gate {
    /// `JEV_SWITCH_APPROVAL_PREFLIGHT` resolved from the environment.
    preflight_enabled: bool,
    /// `CODEX_JEV_PYTHON`, `CODEX_JEV_LAUNCHER`, and `CODEX_JEV_CONFIG` all resolve.
    runtime_complete: bool,
    /// The caller cancelled before the attempt started.
    cancelled: bool,
}

/// Decide eligibility without touching the live session.
fn ineligibility(
    gate: Gate,
    options: &GuardianReviewOptions,
    reasons: &ApprovalRequestReasons,
    request: &GuardianApprovalRequest,
) -> Option<Ineligible> {
    if !gate.preflight_enabled {
        return Some(Ineligible::SwitchOff);
    }
    if !gate.runtime_complete {
        return Some(Ineligible::RuntimeIncomplete);
    }
    if gate.cancelled {
        return Some(Ineligible::Cancelled);
    }
    if options.require_guardian {
        return Some(Ineligible::MandatoryReview);
    }
    if options.require_synchronous_review {
        return Some(Ineligible::SynchronousReviewRequired);
    }
    if reasons.retry.is_some() {
        return Some(Ineligible::Retry);
    }
    if action_is_guardian_owned(request) {
        return Some(Ineligible::UnsupportedAction);
    }
    None
}

/// The action classes a preflight may replace. Network, permissions, stdin,
/// intercepted execs, computer use and MCP remain Guardian-owned, and an
/// escalated command keeps its own reviewer because escalation is a permission
/// decision rather than an action classification.
fn action_is_guardian_owned(request: &GuardianApprovalRequest) -> bool {
    match request {
        GuardianApprovalRequest::ExecCommand {
            sandbox_permissions,
            ..
        } => sandbox_permissions.requires_escalated_permissions(),
        GuardianApprovalRequest::ApplyPatch { .. } => false,
        GuardianApprovalRequest::WriteStdin { .. }
        | GuardianApprovalRequest::McpToolCall { .. }
        | GuardianApprovalRequest::NetworkAccess { .. }
        | GuardianApprovalRequest::RequestPermissions { .. } => true,
        #[cfg(unix)]
        GuardianApprovalRequest::Execve { .. } => true,
    }
}

/// Append `value` in the component's canonical form: object keys sorted, no
/// insignificant whitespace, strings left as UTF-8. `jev_approval.schema.canonical`
/// is the definition, and the engine hashes exactly these bytes; any divergence
/// makes the binding check below fail closed rather than accept an answer.
fn canonical_json(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&number.to_string()),
        Value::String(text) => push_json_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                canonical_json(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_unstable();
            out.push('{');
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                push_json_string(key, out);
                out.push(':');
                if let Some(item) = map.get(key) {
                    canonical_json(item, out);
                }
            }
            out.push('}');
        }
    }
}

/// Escape a string the way `json.dumps(..., ensure_ascii=False)` does: only `"`, `\`,
/// the five short control escapes and the remaining C0 controls, with non-ASCII kept
/// as UTF-8.
fn push_json_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            control if (control as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", control as u32);
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

fn digest_of(value: &Value) -> String {
    let mut canonical = String::new();
    canonical_json(value, &mut canonical);
    let digest = Sha256::digest(canonical.as_bytes());
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

/// `Envelope.policy_hash`: the digest of the policy object the host sent, which pins the
/// reviewed instructions independently of the transport.
fn policy_hash(policy: &Value) -> String {
    digest_of(policy)
}

/// `Envelope.snapshot_hash`: the digest of the whole envelope the host sent, request id
/// included, so no answer can be reused for another request.
fn envelope_snapshot_hash(envelope: &Value) -> String {
    digest_of(envelope)
}

/// The component's own transport refuses an answer whose `request_id`, `snapshot_hash`,
/// `policy_hash` or `question_hash` does not match what it sent
/// (`daemon_response_binding_mismatch`). The port must not accept a weaker binding, so
/// an answer that does not name this exact action and policy, under this exact approved
/// question set, is treated as absent and Guardian runs unchanged.
fn assessment(
    response: &Value,
    request_id: &str,
    expected_policy_hash: &str,
    expected_snapshot_hash: &str,
) -> Option<GuardianAssessment> {
    if response.get("schema_version")?.as_u64()? != 1
        || response.get("request_id")?.as_str()? != request_id
        || response.get("policy_hash")?.as_str()? != expected_policy_hash
        || response.get("snapshot_hash")?.as_str()? != expected_snapshot_hash
        || response.get("question_hash")?.as_str()? != APPROVED_QUESTION_HASH
    {
        return None;
    }
    let outcome = match response.get("decision")?.as_str()? {
        "allow" => GuardianAssessmentOutcome::Allow,
        "deny" => GuardianAssessmentOutcome::Deny,
        _ => return None,
    };
    let risk_level = match response.pointer("/vector/choices/risk/choice")?.as_str()? {
        "low" => GuardianRiskLevel::Low,
        "medium" => GuardianRiskLevel::Medium,
        "high" => GuardianRiskLevel::High,
        "critical" => GuardianRiskLevel::Critical,
        _ => return None,
    };
    // Enforce the low-risk boundary again at the host, independently of Python policy.
    if outcome == GuardianAssessmentOutcome::Allow && risk_level != GuardianRiskLevel::Low {
        return None;
    }
    let user_authorization = match response
        .pointer("/vector/choices/authorization/choice")?
        .as_str()?
    {
        "explicit" => GuardianUserAuthorization::High,
        "within_task" => GuardianUserAuthorization::Medium,
        "absent" => GuardianUserAuthorization::Low,
        "unknown" => GuardianUserAuthorization::Unknown,
        _ => return None,
    };
    if outcome == GuardianAssessmentOutcome::Allow
        && !matches!(
            user_authorization,
            GuardianUserAuthorization::High | GuardianUserAuthorization::Medium
        )
    {
        return None;
    }
    let model = response.pointer("/vector/model")?.as_str()?;
    if model.len() > 64
        || !model.starts_with("jev-")
        || !model
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'.')
    {
        return None;
    }
    Some(GuardianAssessment {
        risk_level,
        user_authorization,
        outcome,
        // This is a deterministic summary, not a claimed model-generated explanation.
        rationale: format!(
            "JEV preflight ({model}); typed policy gate. See the correlated JEV audit record {request_id}."
        ),
    })
}

/// None preserves the existing Guardian inference and its failure/user routing.
/// The budget includes evidence preparation, subprocess startup and inference.
#[allow(clippy::too_many_arguments)]
pub(super) async fn review(
    session: &Session,
    context: &GuardianReviewContext,
    request: &GuardianApprovalRequest,
    reasons: &ApprovalRequestReasons,
    options: &GuardianReviewOptions,
    review_id: &str,
    guardian_deadline: Instant,
    cancellation: &CancellationToken,
) -> Option<GuardianReviewOutcome> {
    let python = absolute_file_env("CODEX_JEV_PYTHON");
    let launcher = absolute_file_env("CODEX_JEV_LAUNCHER");
    let config = absolute_file_env("CODEX_JEV_CONFIG");
    let gate = Gate {
        preflight_enabled: switch_on(PREFLIGHT_SWITCH),
        runtime_complete: python.is_some() && launcher.is_some() && config.is_some(),
        cancelled: cancellation.is_cancelled(),
    };
    if ineligibility(gate, options, reasons, request).is_some() {
        return None;
    }
    let (python, launcher, config) = (python?, launcher?, config?);
    let deadline = std::cmp::min(guardian_deadline, Instant::now() + PREFLIGHT_BUDGET);
    let attempt = async {
        let live = session.get_config().await;
        let requirements = live.config_layer_stack.requirements();
        if requirements.auto_review_required_for_model(&context.model_info.slug)
            || requirements
                .approvals_reviewer
                .can_set(&ApprovalsReviewer::User)
                .is_err()
        {
            return None;
        }
        let history = session.conversation_history_snapshot().await;
        let evidence = session
            .services
            .thread_extension_data
            .get_or_init(GuardianReviewEvidence::default);
        let local_version = evidence.authorization_version(history.as_ref());
        let root_version = session
            .services
            .agent_control
            .root_user_authorization(session.thread_id)
            .await
            .map(|snapshot| snapshot.authorization_version);
        if !local_version.retained_context_complete
            || root_version.is_some_and(|version| !version.retained_context_complete)
        {
            return None;
        }
        // Reuse Codex's selected, managed-policy-aware Guardian prompt, not a copied policy.
        let reviewer = super::review::guardian_review_session_config(session, context)
            .await
            .ok()?;
        let policy = reviewer.spawn_config.base_instructions.clone()?;
        let policy_context = serde_json::json!({"guardian_instructions": policy});
        let items = super::prompt::build_guardian_prompt_items_with_parent_turn(
            session,
            history.as_ref(),
            Some(context),
            reasons.clone(),
            request.clone(),
            super::prompt::GuardianPromptMode::Full,
            /*reviewed_node_repl_evidence_sequence*/ 0,
        )
        .await
        .ok()?;
        if !items.context.truncations.is_empty() {
            return None;
        }
        let messages = serde_json::to_value(items.context.into_messages()).ok()?;
        let action = super::approval_request::guardian_approval_request_to_json(request).ok()?;
        let payload = serde_json::json!({
            "schema_version": 1,
            "request_id": review_id,
            "source": "codex-native",
            "action": action,
            "context": {
                "policy": policy_context,
                "messages": messages,
                "authorization_revision": format!("{local_version:?}|{root_version:?}"),
            },
            "guards": {
                "context_complete": true,
                "mandatory_review": false,
                "fresh_review": false,
                "retry": false,
                "escalated": false,
                "cancelled": false,
                "authorization_current": true,
            }
        });
        let bytes = serde_json::to_vec(&payload).ok()?;
        if bytes.len() > MAX_WIRE_BYTES as usize {
            return None;
        }
        // Bind the answer to the exact envelope and policy this attempt sent. The engine
        // echoes both digests, and `assessment` refuses any answer that disagrees.
        let expected_policy_hash = policy_hash(&payload["context"]["policy"]);
        let expected_snapshot_hash = envelope_snapshot_hash(&payload);
        drop(history);
        let mut child = tokio::process::Command::new(python)
            .arg("-I")
            .arg(&launcher)
            .arg("native")
            .arg("--config")
            .arg(config)
            .current_dir(launcher.parent()?)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .ok()?;
        let mut stdin = child.stdin.take()?;
        stdin.write_all(&bytes).await.ok()?;
        drop(stdin);
        let stdout = child.stdout.take()?;
        let mut output = Vec::new();
        stdout
            .take(MAX_WIRE_BYTES + 1)
            .read_to_end(&mut output)
            .await
            .ok()?;
        if output.len() > MAX_WIRE_BYTES as usize || !child.wait().await.ok()?.success() {
            return None;
        }
        let response: Value = serde_json::from_slice(&output).ok()?;
        let result = assessment(
            &response,
            review_id,
            &expected_policy_hash,
            &expected_snapshot_hash,
        )?;
        // Additional freshness check for every context mode; the original host check remains too.
        let now_history = session.conversation_history_snapshot().await;
        let now_root = session
            .services
            .agent_control
            .root_user_authorization(session.thread_id)
            .await
            .map(|snapshot| snapshot.authorization_version);
        let now_config = super::review::guardian_review_session_config(session, context)
            .await
            .ok()?;
        let now_live = session.get_config().await;
        let now_requirements = now_live.config_layer_stack.requirements();
        if cancellation.is_cancelled()
            || local_version != evidence.authorization_version(now_history.as_ref())
            || root_version != now_root
            || now_config.spawn_config.base_instructions.as_deref() != Some(policy.as_str())
            || now_requirements.auto_review_required_for_model(&context.model_info.slug)
            || now_requirements
                .approvals_reviewer
                .can_set(&ApprovalsReviewer::User)
                .is_err()
        {
            return None;
        }
        Some(GuardianReviewOutcome::Completed(result))
    };
    tokio::select! {
        biased;
        _ = cancellation.cancelled() => None,
        result = tokio::time::timeout_at(deadline, attempt) => result.ok().flatten(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use codex_analytics::GuardianApprovalRequestSource;
    use codex_protocol::approvals::GuardianCommandSource;
    use codex_protocol::approvals::NetworkApprovalProtocol;
    use codex_protocol::models::SandboxPermissions;
    use codex_protocol::protocol::GuardianAssessmentOutcome;
    use codex_protocol::request_permissions::RequestPermissionProfile;
    #[cfg(unix)]
    use codex_utils_absolute_path::AbsolutePathBuf;
    use codex_utils_path_uri::LegacyAppPathString;
    use codex_utils_path_uri::PathUri;
    use serde_json::json;

    /// The envelope these fixtures bind against, kept as text so the parity vectors and
    /// the binding fixtures cannot drift apart.
    const FIXTURE_ENVELOPE: &str = r#"{"schema_version":1,"request_id":"review-1","source":"codex-native","action":{"tool":"exec_command"},"context":{"policy":{"guardian_instructions":"guardian policy"},"messages":[],"authorization_revision":"local|root"},"guards":{"context_complete":true,"mandatory_review":false,"fresh_review":false,"retry":false,"escalated":false,"cancelled":false,"authorization_current":true}}"#;

    fn response() -> serde_json::Value {
        answer_for(&fixture_envelope())
    }

    /// The envelope the host sends for these fixtures. The digests below are what the
    /// engine echoes for it, and every binding test moves one field at a time.
    fn fixture_envelope() -> serde_json::Value {
        serde_json::from_str(FIXTURE_ENVELOPE).expect("fixture envelope is valid JSON")
    }

    fn binding(envelope: &serde_json::Value) -> (String, String) {
        (
            policy_hash(&envelope["context"]["policy"]),
            envelope_snapshot_hash(envelope),
        )
    }

    fn answer_for(envelope: &serde_json::Value) -> serde_json::Value {
        let (policy, snapshot) = binding(envelope);
        json!({"schema_version": 1, "request_id": envelope["request_id"],
        "policy_hash": policy, "snapshot_hash": snapshot,
        "question_hash": APPROVED_QUESTION_HASH, "decision": "allow",
        "vector": {"model": "jev-1.13.0", "choices": {
            "risk": {"choice": "low"}, "authorization": {"choice": "within_task"}
        }}})
    }

    /// Assess `response` against the fixture envelope's binding, the way `review` does.
    fn assess(response: &serde_json::Value) -> Option<GuardianAssessment> {
        let (policy, snapshot) = binding(&fixture_envelope());
        assessment(response, "review-1", &policy, &snapshot)
    }
    #[test]
    fn jev_accepts_bound_low_risk_answer() {
        assert_eq!(
            assess(&response()).unwrap().outcome,
            GuardianAssessmentOutcome::Allow
        );
    }
    #[test]
    fn jev_rejects_wrong_request() {
        let (policy, snapshot) = binding(&fixture_envelope());
        assert!(assessment(&response(), "another", &policy, &snapshot).is_none());
    }
    #[test]
    fn jev_rejects_elevated_allow() {
        let mut value = response();
        value["vector"]["choices"]["risk"]["choice"] = json!("high");
        assert!(assess(&value).is_none());
    }
    #[test]
    fn jev_abstention_keeps_guardian() {
        let mut value = response();
        value["decision"] = json!("defer");
        assert!(assess(&value).is_none());
    }
    #[test]
    fn jev_missing_vector_keeps_guardian() {
        let mut value = response();
        value.as_object_mut().expect("object").remove("vector");
        assert!(assess(&value).is_none());
    }

    // ---- the component's answer binding (issue #22) ----

    /// The canonical form must be byte-identical to `jev_approval.schema.canonical`,
    /// because the engine hashes exactly those bytes. Each digest is the value the
    /// pinned component computes for the same JSON text.
    #[test]
    fn canonical_json_matches_the_pinned_component() {
        let vectors = [
            (
                FIXTURE_ENVELOPE,
                "bb7062d3d98daff5017829bf90cb2a6f1f8ed1eb7bef289e6843b235685a42ee",
            ),
            (
                r#"{"z":[3,1,{"b":"\u00b7 \u00e9 \u4e2d","a":-2}],"a":{"m":"quote\" backslash\\ newline\n tab\t ctrl\u0001","n":12345678901234567890}}"#,
                "c28af59e1b633f099c9b225f79b91bd20e0d846dfc7ad32c98b1527a1fa0c3a3",
            ),
            (
                r#"{"guardian_instructions":"Line one\nLine two \"quoted\" \u00b7" , "x":1}"#,
                "0c2dd1910b8f33dfa907e84433f011c4a05bedc8d656c6f98f8de7916eb651d0",
            ),
        ];
        for (text, expected) in vectors {
            let value: Value = serde_json::from_str(text).expect("test vector is valid JSON");
            assert_eq!(
                digest_of(&value),
                expected,
                "canonical form drifted for {text}"
            );
        }
        assert_eq!(digest_of(&json!({"a": 1})), digest_of(&json!({ "a" : 1 })));
    }

    /// A stale answer, whose snapshot digest names a different envelope, must not
    /// reach the host, which is what stops a replayed or out-of-order result from
    /// granting permission under a reused review id.
    #[test]
    fn stale_answer_keeps_guardian() {
        let mut other = fixture_envelope();
        other["context"]["authorization_revision"] = json!("local|root-earlier");
        let stale = answer_for(&other);
        assert_eq!(stale["request_id"], json!("review-1"));
        assert!(assess(&stale).is_none());
    }

    /// An answer computed against different policy text must not be accepted even if it
    /// holds the right risk and authorization choices.
    #[test]
    fn answer_for_another_policy_keeps_guardian() {
        let mut other = fixture_envelope();
        other["context"]["policy"]["guardian_instructions"] = json!("relaxed policy");
        assert!(assess(&answer_for(&other)).is_none());
    }

    /// The approved question set is a declared pin; a response from a different one is
    /// not a decision this policy was calibrated for.
    #[test]
    fn answer_from_another_question_set_keeps_guardian() {
        let mut value = response();
        value["question_hash"] =
            json!("0000000000000000000000000000000000000000000000000000000000000000");
        assert!(assess(&value).is_none());
    }

    /// The abstention the component's CLI prints when anything fails carries no binding,
    /// so it can never be read as an allow.
    #[test]
    fn adapter_failure_answer_keeps_guardian() {
        assert!(
            assess(&json!({"schema_version": 1, "request_id": "review-1",
            "decision": "defer", "reason": "adapter_failure"}))
            .is_none()
        );
    }

    /// Malformed and unbound material is refused rather than partially trusted.
    #[test]
    fn unbound_or_malformed_answers_keep_guardian() {
        for value in [
            json!({"request_id": "review-1"}),
            json!({"schema_version": 2, "request_id": "review-1", "policy_hash": "x",
                   "snapshot_hash": "y", "question_hash": APPROVED_QUESTION_HASH,
                   "decision": "allow"}),
            json!({"schema_version": 1, "request_id": "review-1", "policy_hash": "x",
                   "snapshot_hash": "y", "question_hash": APPROVED_QUESTION_HASH,
                   "decision": "allow"}),
            json!({"schema_version": 1, "request_id": "review-1", "decision": "allow",
            "vector": {"model": "jev-1.13.0", "choices": {
                "risk": {"choice": "low"}, "authorization": {"choice": "within_task"}
            }}}),
            json!([]),
            json!("allow"),
        ] {
            assert!(assess(&value).is_none(), "{value} must not be accepted");
        }
    }

    // ---- the declared switch contract (CONTRACTS.md C10) ----

    /// Switches reach the runtime as `JEV_SWITCH_<FEATURE>` with `1`/`0`, and `1`
    /// is the only value that turns one on.
    #[test]
    fn switch_key_and_value_follow_the_declared_contract() {
        assert_eq!(
            switch_key("approval.preflight"),
            "JEV_SWITCH_APPROVAL_PREFLIGHT"
        );
        assert_eq!(
            switch_key("projection.dedup_receipts"),
            "JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS"
        );
        assert!(switch_value_on(Some("1")));
        for value in [Some(""), Some("0"), Some("true"), Some("yes"), None] {
            assert!(
                !switch_value_on(value),
                "{value:?} must not enable a switch"
            );
        }
    }

    // ---- disabled and ineligible attempts keep the host's existing route ----

    /// The gate for an otherwise eligible attempt: declared switch on, trusted
    /// runtime resolved, not cancelled.
    fn gate() -> Gate {
        Gate {
            preflight_enabled: true,
            runtime_complete: true,
            cancelled: false,
        }
    }

    fn options() -> GuardianReviewOptions {
        GuardianReviewOptions {
            require_guardian: false,
            plugin_attribution_override: None,
            approval_request_source: GuardianApprovalRequestSource::MainTurn,
            external_cancel: None,
            require_synchronous_review: false,
        }
    }

    fn reasons() -> ApprovalRequestReasons {
        ApprovalRequestReasons {
            approval: Some("review".to_string()),
            retry: None,
        }
    }

    fn uri() -> PathUri {
        PathUri::parse("file:///repo").expect("path uri")
    }

    fn apply_patch() -> GuardianApprovalRequest {
        GuardianApprovalRequest::ApplyPatch {
            id: "patch-1".to_string(),
            cwd: uri(),
            files: vec![uri()],
            patch: String::new(),
        }
    }

    fn exec_command(sandbox_permissions: SandboxPermissions) -> GuardianApprovalRequest {
        GuardianApprovalRequest::ExecCommand {
            id: "exec-1".to_string(),
            environment_id: "local".to_string(),
            command: vec!["git".to_string(), "status".to_string()],
            cwd: uri(),
            guardian_cwd: LegacyAppPathString::from_string("/repo"),
            sandbox_permissions,
            additional_permissions: None,
            justification: None,
            tty: false,
        }
    }

    fn write_stdin() -> GuardianApprovalRequest {
        GuardianApprovalRequest::WriteStdin {
            id: "stdin-1".to_string(),
            approval_id: "approval-1".to_string(),
            environment_id: "local".to_string(),
            process_id: 1,
            input: String::new(),
            cwd: uri(),
            tty: false,
            sandbox_permissions: SandboxPermissions::UseDefault,
            additional_permissions: None,
        }
    }

    fn network_access() -> GuardianApprovalRequest {
        GuardianApprovalRequest::NetworkAccess {
            id: "net-1".to_string(),
            turn_id: "turn-1".to_string(),
            environment_id: "local".to_string(),
            target: "example.test".to_string(),
            host: "example.test".to_string(),
            protocol: NetworkApprovalProtocol::Https,
            port: 443,
            trigger: None,
        }
    }

    fn mcp_tool_call() -> GuardianApprovalRequest {
        GuardianApprovalRequest::McpToolCall {
            id: "mcp-1".to_string(),
            server: "server".to_string(),
            tool_name: "tool".to_string(),
            arguments: None,
            connector_id: None,
            connector_name: None,
            connector_description: None,
            connected_account_email: None,
            tool_title: None,
            tool_description: None,
            annotations: None,
        }
    }

    fn request_permissions() -> GuardianApprovalRequest {
        GuardianApprovalRequest::RequestPermissions {
            id: "perm-1".to_string(),
            turn_id: "turn-1".to_string(),
            reason: None,
            permissions: RequestPermissionProfile::default(),
        }
    }

    #[cfg(unix)]
    fn execve() -> GuardianApprovalRequest {
        GuardianApprovalRequest::Execve {
            id: "execve-1".to_string(),
            environment_id: "local".to_string(),
            source: GuardianCommandSource::Shell,
            program: "/bin/true".to_string(),
            argv: vec!["/bin/true".to_string()],
            cwd: AbsolutePathBuf::from_absolute_path("/repo").expect("absolute path"),
            additional_permissions: None,
        }
    }

    /// With the declared switch off, the attempt is handed back before the
    /// runtime, the session, or a subprocess is touched - even for an action that
    /// would otherwise be eligible. This is the isolated profile's guarantee.
    #[test]
    fn disabled_switch_preserves_existing_routing() {
        let gate = Gate {
            preflight_enabled: false,
            ..gate()
        };
        assert_eq!(
            ineligibility(gate, &options(), &reasons(), &apply_patch()),
            Some(Ineligible::SwitchOff)
        );
    }

    /// Every ineligible input returns `Some`, which is what hands the attempt back
    /// to the host's original routing instead of granting anything.
    #[test]
    fn ineligible_attempts_preserve_existing_routing() {
        for (gate, expected) in [
            (
                Gate {
                    runtime_complete: false,
                    ..gate()
                },
                Ineligible::RuntimeIncomplete,
            ),
            (
                Gate {
                    cancelled: true,
                    ..gate()
                },
                Ineligible::Cancelled,
            ),
        ] {
            assert_eq!(
                ineligibility(gate, &options(), &reasons(), &apply_patch()),
                Some(expected)
            );
        }

        let mandatory = GuardianReviewOptions {
            require_guardian: true,
            ..options()
        };
        assert_eq!(
            ineligibility(gate(), &mandatory, &reasons(), &apply_patch()),
            Some(Ineligible::MandatoryReview)
        );

        let synchronous = GuardianReviewOptions {
            require_synchronous_review: true,
            ..options()
        };
        assert_eq!(
            ineligibility(gate(), &synchronous, &reasons(), &apply_patch()),
            Some(Ineligible::SynchronousReviewRequired)
        );

        let retry = ApprovalRequestReasons {
            retry: Some("authorization changed".to_string()),
            ..reasons()
        };
        assert_eq!(
            ineligibility(gate(), &options(), &retry, &apply_patch()),
            Some(Ineligible::Retry)
        );
    }

    /// Network, permissions, stdin, intercepted execs, MCP, and escalated commands
    /// keep their own reviewer rather than becoming preflight candidates.
    #[test]
    fn guardian_owned_actions_preserve_existing_routing() {
        let mut owned = vec![
            mcp_tool_call(),
            write_stdin(),
            network_access(),
            request_permissions(),
            exec_command(SandboxPermissions::RequireEscalated),
        ];
        #[cfg(unix)]
        owned.push(execve());

        for request in owned {
            assert_eq!(
                ineligibility(gate(), &options(), &reasons(), &request),
                Some(Ineligible::UnsupportedAction)
            );
        }
    }

    /// The eligible control: a sandboxed command and a patch application are the
    /// only inputs that let preflight reach the carrier at all.
    #[test]
    fn eligible_actions_are_the_only_ones_that_reach_the_carrier() {
        for request in [
            apply_patch(),
            exec_command(SandboxPermissions::UseDefault),
            exec_command(SandboxPermissions::WithAdditionalPermissions),
        ] {
            assert_eq!(
                ineligibility(gate(), &options(), &reasons(), &request),
                None
            );
        }
    }
}
