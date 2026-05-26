from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.github_comment_poster import render_verification_comment
from src.lark_adapter import (
    GetLarkInvokeResult,
    GetLarkLiveCheckAdapter,
    GetLarkWorkflowListResult,
    GetLarkWorkflowRef,
    pick_workflow_id,
    resolve_execution_status,
)
from src.run_health import compute_run_health, format_health_summary_line, run_comment_headline
from src.service import VerificationService
from src.models import (
    ArtifactKind,
    BriefClassification,
    BriefConfidence,
    BriefSignals,
    ConfidenceLevel,
    ExecutionArtifact,
    IssueSummary,
    PlanMode,
    PlanTargetType,
    ResultStatus,
    RunStatus,
    VerificationBrief,
    VerificationMode,
    VerificationPlan,
    VerificationResult,
    VerifyResponse,
)


def _issue() -> IssueSummary:
    return IssueSummary(
        number=2,
        title="Save profile returns 500 on settings page",
        body="issue body",
        state="open",
        labels=[],
        author="pjm",
        url="https://github.com/example/repo/issues/2",
    )


def _brief() -> VerificationBrief:
    return VerificationBrief(
        summary="Save profile returns 500 on settings page",
        classification=BriefClassification.REPRODUCIBLE_CANDIDATE,
        reproduction_steps=[
            "Log in as a test user",
            "Open Settings -> Profile",
            "Change display name and click Save",
        ],
        expected_behavior="Profile saves",
        actual_behavior="HTTP 500",
        missing_information=[],
        signals=BriefSignals(
            has_numbered_steps=True,
            has_expected_behavior=True,
            has_actual_behavior=True,
            has_error_message=True,
            has_environment_details=True,
        ),
        confidence=BriefConfidence(
            level=ConfidenceLevel.HIGH,
            reason="Structured reproduction steps with a clear observed failure.",
        ),
        recommended_verification_mode=VerificationMode.LARK_WORKFLOW_CANDIDATE,
    )


def _plan() -> VerificationPlan:
    return VerificationPlan(
        mode=PlanMode.LARK_WORKFLOW_CANDIDATE,
        target_type=PlanTargetType.UI_FLOW,
        workflow_name="github_issue_verification",
        goal="Save profile returns 500 on settings page",
        proposed_steps=[
            "Log in as a test user",
            "Open Settings -> Profile",
            "Change display name and click Save",
        ],
        assumptions=[],
        blockers=[],
    )


def test_resolve_execution_status_reproducible_is_simulated_by_default() -> None:
    status = resolve_execution_status(_plan(), _brief())
    assert status == ResultStatus.SIMULATED


def test_live_check_marks_reproduced_when_invoke_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("src.lark_adapter.GETLARK_ENABLE_WORKFLOW_INVOKE", True)
    adapter = GetLarkLiveCheckAdapter(api_key="key", api_url="https://api.getlark.ai")
    adapter._client.list_workflows = lambda limit=5: GetLarkWorkflowListResult(  # type: ignore[method-assign]
        endpoint="https://api.getlark.ai/workflows",
        status_code=200,
        workflow_count=1,
        summary="1 workflow(s); examples: larkguard-smoke",
        response_snippet='{"workflows":[{"id":"wflw_123","name":"larkguard-smoke"}]}',
        workflow_ids=["wflw_123"],
        workflow_refs=[GetLarkWorkflowRef(workflow_id="wflw_123", name="larkguard-smoke")],
    )
    adapter._client.invoke_workflow_best_effort = (  # type: ignore[method-assign]
        lambda **kwargs: GetLarkInvokeResult(
            attempted=True,
            endpoint="https://api.getlark.ai/workflows/wflw_123/invoke",
            status_code=200,
            success=True,
            summary=(
                "Lark workflow invoked successfully via POST "
                "https://api.getlark.ai/workflows/wflw_123/invoke "
                "(execution_id=wflw_exec_123)"
            ),
            response_snippet='{"id":"wflw_exec_123","workflow_id":"wflw_123"}',
            workflow_id="wflw_123",
            execution_id="wflw_exec_123",
        )
    )

    result = adapter.execute(_plan(), _brief(), _issue())

    assert result.status == ResultStatus.REPRODUCED
    assert any(item.label == "execution_id" for item in result.evidence)
    assert any("Lark workflow invoked successfully." in note for note in result.execution_notes)


def test_comment_includes_sponsor_lines_for_live_parser_and_adapter() -> None:
    result = VerificationResult(
        status=ResultStatus.REPRODUCED,
        outcome_summary="Run finished.",
        evidence=[
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="execution_id",
                content="wflw_exec_123 (workflow wflw_123)",
            )
        ],
        execution_notes=["Lark workflow invoked successfully."],
        resilience_notes=["No fallback path executed in this run"],
        confidence=BriefConfidence(level=ConfidenceLevel.HIGH, reason="Live invoke succeeded"),
    )
    response = VerifyResponse(
        run_id="abc123def456",
        status=RunStatus.COMPLETED,
        issue=_issue(),
        comments=[],
        evidence_packet={
            "problem_statement": "x",
            "raw_report_text": "x",
            "report_quality_hints": {
                "has_body": True,
                "has_repro_signals": True,
                "comment_count": 0,
                "label_count": 0,
            },
            "combined_text": "x",
        },
        verification_brief=_brief(),
        verification_plan=_plan(),
        verification_result=result,
        adapter_used="getlark_live_check",
        primary_adapter_requested="getlark_live_check",
        parser_used="truefoundry_gateway",
    )

    comment = render_verification_comment(response)
    assert "Powered by [getlark.ai]" in comment
    assert "Powered by [TrueFoundry AI Gateway]" in comment
    assert "live getlark workflow execution proof" in comment
    assert "**Primary requested:** `getlark_live_check`" in comment
    assert "**Live sponsor run**" in comment


def test_live_check_without_execution_id_gets_rest_sponsor_banner() -> None:
    result = VerificationResult(
        status=ResultStatus.SIMULATED,
        outcome_summary="Live list only.",
        evidence=[
            ExecutionArtifact(
                kind=ArtifactKind.LOG,
                label="live_api",
                content="Real getlark API call succeeded: GET /workflows → HTTP 200",
            )
        ],
        execution_notes=[],
        resilience_notes=[],
        confidence=BriefConfidence(level=ConfidenceLevel.MEDIUM, reason="list ok"),
    )
    response = VerifyResponse(
        run_id="abc123def456",
        status=RunStatus.COMPLETED,
        issue=_issue(),
        comments=[],
        evidence_packet={
            "problem_statement": "x",
            "raw_report_text": "x",
            "report_quality_hints": {
                "has_body": True,
                "has_repro_signals": True,
                "comment_count": 0,
                "label_count": 0,
            },
            "combined_text": "x",
        },
        verification_brief=_brief(),
        verification_plan=_plan(),
        verification_result=result,
        adapter_used="getlark_live_check",
        primary_adapter_requested="getlark_live_check",
    )
    assert run_comment_headline(response) == (
        "> **Live sponsor run** — real getlark REST live check succeeded."
    )


def test_cli_live_fallback_uses_adapter_specific_wording() -> None:
    result, adapter_used = VerificationService._fallback_after_live_adapter_failure(
        _plan(),
        _brief(),
        _issue(),
        "getlark CLI not found",
        failed_adapter="getlark_cli_live",
    )
    assert adapter_used == "fake"
    assert result.execution_notes[0] == "getlark_cli_live failed: getlark CLI not found"
    assert any("getlark_cli_live failed; fell back to fake adapter" in n for n in result.resilience_notes)


def test_pick_workflow_id_prefers_env_name() -> None:
    refs = [
        GetLarkWorkflowRef(workflow_id="wflw_a", name="other"),
        GetLarkWorkflowRef(workflow_id="wflw_b", name="larkguard-smoke"),
    ]
    picked = pick_workflow_id(
        refs,
        workflow_id=None,
        workflow_name="larkguard-smoke",
    )
    assert picked == "wflw_b"


def test_pick_workflow_id_prefers_env_id() -> None:
    refs = [
        GetLarkWorkflowRef(workflow_id="wflw_a", name="alpha"),
        GetLarkWorkflowRef(workflow_id="wflw_b", name="beta"),
    ]
    picked = pick_workflow_id(refs, workflow_id="wflw_b", workflow_name=None)
    assert picked == "wflw_b"


def test_getlark_cli_live_adapter_captures_cli_stdout(monkeypatch) -> None:
    from src.lark_adapter import GetLarkCliListResult, GetLarkCliLiveAdapter

    monkeypatch.setattr(
        "src.lark_adapter.run_getlark_cli_list",
        lambda **kwargs: GetLarkCliListResult(
            attempted=True,
            command="getlark workflows list",
            exit_code=0,
            success=True,
            summary="getlark CLI workflows list succeeded (exit 0): getlark workflows list",
            stdout_snippet='{"workflows":[{"id":"wflw_123","name":"larkguard-smoke"}]}',
            stderr_snippet="",
        ),
    )
    adapter = GetLarkCliLiveAdapter(api_key="key", api_url="https://api.getlark.ai")
    result = adapter.execute(_plan(), _brief(), _issue())
    assert result.status == ResultStatus.SIMULATED
    assert any(item.label == "cli_stdout" for item in result.evidence)


def test_health_summary_line_is_compact() -> None:
    response = VerifyResponse(
        run_id="abc123def456",
        status=RunStatus.COMPLETED,
        issue=_issue(),
        comments=[],
        evidence_packet={
            "problem_statement": "x",
            "raw_report_text": "x",
            "report_quality_hints": {
                "has_body": True,
                "has_repro_signals": True,
                "comment_count": 0,
                "label_count": 0,
            },
            "combined_text": "x",
        },
        verification_brief=_brief(),
        verification_plan=_plan(),
        verification_result=VerificationResult(
            status=ResultStatus.REPRODUCED,
            outcome_summary="ok",
            evidence=[],
            execution_notes=[],
            resilience_notes=[],
            confidence=BriefConfidence(level=ConfidenceLevel.HIGH, reason="ok"),
        ),
        adapter_used="getlark_live_check",
        parser_used="truefoundry_gateway",
    )
    assert compute_run_health(response) == "healthy"
    assert "Health=healthy" in format_health_summary_line(response)
