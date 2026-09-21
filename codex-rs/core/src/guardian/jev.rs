//! Opt-in JEV preflight inside the existing synchronous Guardian attempt.
//! Native source adapter for openai/codex c45ea25ffb72d5f7324489d824d0c677283aa0b4,
//! ported into this repository's pinned host base. The guarded `guardian/mod.rs` and
//! `guardian/review_request.rs` blobs are identical at both revisions, so the port
//! required no source reconciliation beyond module placement.
//! No contributor, sandbox, permission, hook-precedence or approval-cache changes.

use std::path::PathBuf;
use std::process::Stdio;

use codex_guardian_reviewer::GuardianAssessment;
use codex_guardian_reviewer::GuardianReviewOutcome;
use codex_protocol::config_types::ApprovalsReviewer;
use codex_protocol::protocol::GuardianAssessmentOutcome;
use codex_protocol::protocol::GuardianRiskLevel;
use codex_protocol::protocol::GuardianUserAuthorization;
use serde_json::Value;
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

fn assessment(response: &Value, request_id: &str) -> Option<GuardianAssessment> {
    if response.get("schema_version")?.as_u64()? != 1
        || response.get("request_id")?.as_str()? != request_id
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
                "policy": {"guardian_instructions": policy},
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
        let result = assessment(&response, review_id)?;
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

    fn response() -> serde_json::Value {
        json!({"schema_version": 1, "request_id": "review-1", "decision": "allow",
        "vector": {"model": "jev-1.13.0", "choices": {
            "risk": {"choice": "low"}, "authorization": {"choice": "within_task"}
        }}})
    }
    #[test]
    fn jev_accepts_bound_low_risk_answer() {
        assert_eq!(
            assessment(&response(), "review-1").unwrap().outcome,
            GuardianAssessmentOutcome::Allow
        );
    }
    #[test]
    fn jev_rejects_wrong_request() {
        assert!(assessment(&response(), "another").is_none());
    }
    #[test]
    fn jev_rejects_elevated_allow() {
        let mut value = response();
        value["vector"]["choices"]["risk"]["choice"] = json!("high");
        assert!(assessment(&value, "review-1").is_none());
    }
    #[test]
    fn jev_abstention_keeps_guardian() {
        let mut value = response();
        value["decision"] = json!("defer");
        assert!(assessment(&value, "review-1").is_none());
    }
    #[test]
    fn jev_missing_vector_keeps_guardian() {
        assert!(
            assessment(
                &json!({"schema_version":1,"request_id":"review-1","decision":"allow"}),
                "review-1"
            )
            .is_none()
        );
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
