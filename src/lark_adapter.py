from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from src.config import (
    GETLARK_API_KEY,
    GETLARK_API_URL,
    getlark_credentials_complete,
    requested_lark_mode,
)
from src.models import (
    ArtifactKind,
    BriefClassification,
    BriefConfidence,
    ConfidenceLevel,
    ExecutionArtifact,
    IssueSummary,
    PlanMode,
    PlanTargetType,
    ResultStatus,
    VerificationBrief,
    VerificationMode,
    VerificationPlan,
    VerificationResult,
)

# TODO: wire real getlark MCP HTTP client (https://api.getlark.ai/mcp) in GetLarkMcpAdapter
# TODO: wire real getlark CLI subprocess (@getlark/cli workflows create/invoke) in GetLarkCliAdapter
# TODO: artifact persistence — save screenshots/logs from getlark executions per run
# TODO: resilience_gateway wrapping adapter execution — fallback on MCP/CLI outage

GetLarkTransport = Literal["mcp", "cli"]

UI_VERB = re.compile(
    r"\b(click|open|navigate|select|press|submit|login|sign\s*in|type|enter|go\s+to|tap|scroll)\b",
    re.IGNORECASE,
)


@dataclass
class LarkAdapterSelection:
    adapter: LarkAdapter
    adapter_id: str
    preamble_notes: list[str] = field(default_factory=list)


class LarkAdapter(ABC):
    adapter_id: str = "unknown"

    @abstractmethod
    def execute(
        self,
        plan: VerificationPlan,
        brief: VerificationBrief,
        issue: IssueSummary,
    ) -> VerificationResult:
        """Run verification for the given plan and return a structured result."""


def resolve_lark_adapter() -> LarkAdapterSelection:
    """Select getlark.ai adapter from config; fall back to fake when credentials are missing."""
    mode = requested_lark_mode()

    if mode == "getlark_mcp" and getlark_credentials_complete():
        return LarkAdapterSelection(
            adapter=GetLarkScaffoldAdapter(
                api_key=GETLARK_API_KEY,
                api_url=GETLARK_API_URL,
                transport="mcp",
            ),
            adapter_id="getlark_mcp_scaffold",
            preamble_notes=["Adapter selected: getlark_mcp_scaffold"],
        )

    if mode == "getlark_cli" and getlark_credentials_complete():
        return LarkAdapterSelection(
            adapter=GetLarkScaffoldAdapter(
                api_key=GETLARK_API_KEY,
                api_url=GETLARK_API_URL,
                transport="cli",
            ),
            adapter_id="getlark_cli_scaffold",
            preamble_notes=["Adapter selected: getlark_cli_scaffold"],
        )

    if mode in ("getlark_mcp", "getlark_cli"):
        return LarkAdapterSelection(
            adapter=FakeLarkAdapter(),
            adapter_id="fake",
            preamble_notes=[
                f"Requested {mode} but missing GETLARK_API_KEY; fell back to fake adapter",
                "Adapter selected: fake",
            ],
        )

    return LarkAdapterSelection(
        adapter=FakeLarkAdapter(),
        adapter_id="fake",
        preamble_notes=["Adapter selected: fake"],
    )


def default_lark_adapter() -> LarkAdapter:
    return resolve_lark_adapter().adapter


def annotate_result(
    result: VerificationResult, selection: LarkAdapterSelection
) -> VerificationResult:
    """Attach adapter selection / fallback notes to a verification result."""
    preamble = list(selection.preamble_notes)
    if not any(note.startswith("Adapter selected:") for note in preamble):
        preamble.insert(0, f"Adapter selected: {selection.adapter_id}")

    execution_notes = preamble + list(result.execution_notes)
    resilience_notes = list(result.resilience_notes)

    if any("fell back" in note.lower() for note in preamble):
        resilience_notes = [
            note
            for note in resilience_notes
            if note != "No fallback path executed in this run"
        ]
        resilience_notes.append(
            "Fallback path executed: getlark mode requested but GETLARK_API_KEY was missing"
        )

    return result.model_copy(
        update={
            "execution_notes": execution_notes,
            "resilience_notes": resilience_notes,
        }
    )


def plan_verification(brief: VerificationBrief) -> VerificationPlan:
    """Convert a verification brief into an execution plan."""
    if brief.recommended_verification_mode == VerificationMode.MANUAL_REVIEW:
        return VerificationPlan(
            mode=PlanMode.MANUAL_REVIEW,
            target_type=PlanTargetType.UNKNOWN,
            workflow_name="manual_issue_review",
            goal=brief.summary,
            proposed_steps=brief.reproduction_steps,
            assumptions=[],
            blockers=list(brief.missing_information) or ["Manual review required before automation"],
        )

    target_type = (
        PlanTargetType.UI_FLOW
        if _steps_have_ui_verbs(brief.reproduction_steps)
        else PlanTargetType.UNKNOWN
    )
    assumptions = _infer_assumptions(brief)

    return VerificationPlan(
        mode=PlanMode.LARK_WORKFLOW_CANDIDATE,
        target_type=target_type,
        workflow_name="github_issue_verification",
        goal=_concise_goal(brief),
        proposed_steps=list(brief.reproduction_steps),
        assumptions=assumptions,
        blockers=[],
    )


class FakeLarkAdapter(LarkAdapter):
    """Simulates plan → execute → result without getlark.ai connectivity."""

    adapter_id = "fake"

    def execute(
        self,
        plan: VerificationPlan,
        brief: VerificationBrief,
        issue: IssueSummary,
    ) -> VerificationResult:
        status = resolve_execution_status(plan, brief)
        step_count = len(plan.proposed_steps)
        evidence = [
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="plan",
                content="Prepared workflow plan",
            ),
            ExecutionArtifact(
                kind=ArtifactKind.LOG,
                label="execution",
                content="Simulated execution only; getlark.ai adapter not connected",
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="steps",
                content=f"Would execute {step_count} proposed step(s)",
            ),
        ]
        if plan.proposed_steps:
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.TRACE,
                    label="workflow",
                    content=" → ".join(plan.proposed_steps[:5]),
                )
            )

        return VerificationResult(
            status=status,
            outcome_summary=outcome_summary(status, plan, brief, issue),
            evidence=evidence,
            execution_notes=execution_notes(plan, brief, status, adapter_label="fake"),
            resilience_notes=[
                "No resilience gateway configured yet",
                "No fallback path executed in this run",
            ],
            confidence=result_confidence(status, brief),
        )


class GetLarkScaffoldAdapter(LarkAdapter):
    """getlark.ai scaffold — describes intended MCP or CLI workflow; no network/subprocess calls."""

    def __init__(
        self,
        api_key: str,
        api_url: str,
        transport: GetLarkTransport,
    ) -> None:
        if not api_key:
            raise ValueError("GETLARK_API_KEY is required for getlark scaffold modes")
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._transport = transport

    @property
    def adapter_id(self) -> str:
        return f"getlark_{self._transport}_scaffold"

    def execute(
        self,
        plan: VerificationPlan,
        brief: VerificationBrief,
        issue: IssueSummary,
    ) -> VerificationResult:
        status = resolve_execution_status(plan, brief)
        workflow_description = build_getlark_workflow_description(plan, brief, issue)
        integration_mode = f"getlark_{self._transport}_scaffold"
        mcp_url = f"{self._api_url}/mcp"

        evidence = [
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="integration",
                content=f"integration mode = {integration_mode}",
            ),
            ExecutionArtifact(
                kind=ArtifactKind.LOG,
                label="vendor",
                content="getlark.ai (testing platform) — not Lark Suite / larksuite.com",
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="workflow",
                content=(
                    f"Would create/run workflow '{plan.workflow_name}' "
                    f"(target_type={plan.target_type.value})"
                ),
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="workflow_description",
                content=workflow_description[:2000],
            ),
        ]

        if self._transport == "mcp":
            evidence.extend(
                [
                    ExecutionArtifact(
                        kind=ArtifactKind.LOG,
                        label="mcp",
                        content=(
                            f"Would call MCP at {mcp_url} (X-API-Key header); "
                            "real HTTP not wired yet"
                        ),
                    ),
                    ExecutionArtifact(
                        kind=ArtifactKind.TRACE,
                        label="mcp_tools",
                        content="list workflows, create workflow, invoke workflow, fetch execution logs",
                    ),
                ]
            )
        else:
            cli_name = _cli_workflow_name(issue)
            evidence.extend(
                [
                    ExecutionArtifact(
                        kind=ArtifactKind.LOG,
                        label="cli",
                        content="Would run @getlark/cli via subprocess; not spawned in this step",
                    ),
                    ExecutionArtifact(
                        kind=ArtifactKind.TRACE,
                        label="cli_create",
                        content=(
                            f'getlark workflows create --name "{cli_name}" '
                            f'--description "{_shell_escape(workflow_description[:500])}"'
                        ),
                    ),
                    ExecutionArtifact(
                        kind=ArtifactKind.TRACE,
                        label="cli_invoke",
                        content="getlark workflows invoke --workflow-ids <new_id> --wait --timeout 300",
                    ),
                ]
            )

        if plan.proposed_steps:
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.TRACE,
                    label="intended_steps",
                    content=" → ".join(plan.proposed_steps[:8]),
                )
            )

        assumptions_text = "; ".join(plan.assumptions) if plan.assumptions else "(none)"
        blockers_text = "; ".join(plan.blockers) if plan.blockers else "(none)"
        evidence.extend(
            [
                ExecutionArtifact(
                    kind=ArtifactKind.NOTE,
                    label="assumptions",
                    content=f"Plan assumptions: {assumptions_text}",
                ),
                ExecutionArtifact(
                    kind=ArtifactKind.NOTE,
                    label="blockers",
                    content=f"Plan blockers: {blockers_text}",
                ),
            ]
        )

        transport_label = "MCP" if self._transport == "mcp" else "CLI"
        return VerificationResult(
            status=status,
            outcome_summary=(
                f"{issue.title.strip() or f'Issue #{issue.number}'}: "
                f"{integration_mode} prepared {transport_label} workflow "
                f"'{plan.workflow_name}' ({len(plan.proposed_steps)} step(s)); "
                "live getlark execution not wired yet."
            ),
            evidence=evidence,
            execution_notes=execution_notes(
                plan,
                brief,
                status,
                adapter_label=integration_mode,
                extra=[
                    f"getlark API base: {self._api_url}",
                    f"Transport: {transport_label}",
                    "Docs: https://docs.getlark.ai/mcp-quickstart and https://docs.getlark.ai/cli",
                    "Real getlark calls are not implemented in this step",
                ],
            ),
            resilience_notes=[
                "No resilience gateway configured yet",
                "No fallback path executed in this run",
            ],
            confidence=BriefConfidence(
                level=ConfidenceLevel.MEDIUM,
                reason="Scaffold mode: plan is getlark-ready but execution was not performed.",
            ),
        )


def build_getlark_workflow_description(
    plan: VerificationPlan,
    brief: VerificationBrief,
    issue: IssueSummary,
) -> str:
    """Natural-language workflow body for getlark workflows create --description."""
    lines = [
        f"Verify GitHub issue #{issue.number}: {issue.title}",
        f"Goal: {plan.goal}",
    ]
    if issue.url:
        lines.append(f"Issue URL: {issue.url}")
    if brief.expected_behavior:
        lines.append(f"Expected: {brief.expected_behavior}")
    if brief.actual_behavior:
        lines.append(f"Actual: {brief.actual_behavior}")
    if plan.proposed_steps:
        lines.append("Reproduction steps:")
        lines.extend(f"- {step}" for step in plan.proposed_steps)
    else:
        lines.append(f"Report summary: {brief.summary}")
    return "\n".join(lines)


def _cli_workflow_name(issue: IssueSummary) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", issue.title.lower()).strip("-")[:40]
    return f"issue-{issue.number}-{slug or 'verify'}"


def _shell_escape(text: str) -> str:
    return text.replace('"', '\\"')


def resolve_execution_status(
    plan: VerificationPlan, brief: VerificationBrief
) -> ResultStatus:
    if plan.mode == PlanMode.MANUAL_REVIEW:
        return ResultStatus.BLOCKED
    if brief.classification == BriefClassification.BLOCKED_MISSING_INFO:
        return ResultStatus.BLOCKED
    if brief.classification == BriefClassification.REPRODUCIBLE_CANDIDATE:
        return ResultStatus.SIMULATED
    if brief.classification == BriefClassification.UNCLEAR:
        if plan.proposed_steps:
            return ResultStatus.NOT_REPRODUCED
        return ResultStatus.BLOCKED
    return ResultStatus.BLOCKED


def outcome_summary(
    status: ResultStatus,
    plan: VerificationPlan,
    brief: VerificationBrief,
    issue: IssueSummary,
) -> str:
    title = issue.title.strip() or f"Issue #{issue.number}"
    if status == ResultStatus.BLOCKED:
        return f"{title}: verification blocked ({plan.workflow_name})."
    if status == ResultStatus.SIMULATED:
        return (
            f"{title}: simulated successful workflow run "
            f"({len(plan.proposed_steps)} step(s), not a live repro)."
        )
    if status == ResultStatus.NOT_REPRODUCED:
        return f"{title}: could not confirm reproduction from available report details."
    return f"{title}: verification marked as {status.value}."


def execution_notes(
    plan: VerificationPlan,
    brief: VerificationBrief,
    status: ResultStatus,
    *,
    adapter_label: str,
    extra: list[str] | None = None,
) -> list[str]:
    notes = [
        f"Planner selected workflow '{plan.workflow_name}' with target_type={plan.target_type.value}.",
        f"Brief classification: {brief.classification.value}.",
    ]
    if plan.assumptions:
        notes.append(f"Assumptions: {'; '.join(plan.assumptions)}")
    if plan.blockers:
        notes.append(f"Blockers: {'; '.join(plan.blockers)}")
    if extra:
        notes.extend(extra)
    notes.append(f"{adapter_label} adapter resolved status to '{status.value}'.")
    return notes


def result_confidence(status: ResultStatus, brief: VerificationBrief) -> BriefConfidence:
    if status == ResultStatus.SIMULATED:
        return BriefConfidence(
            level=ConfidenceLevel.MEDIUM,
            reason="Simulated/scaffold run only; real getlark execution would be needed to confirm reproduction.",
        )
    if status == ResultStatus.BLOCKED:
        return BriefConfidence(
            level=ConfidenceLevel.LOW,
            reason="Verification could not proceed automatically due to missing info or manual-review mode.",
        )
    return BriefConfidence(
        level=brief.confidence.level,
        reason=brief.confidence.reason,
    )


def _steps_have_ui_verbs(steps: list[str]) -> bool:
    return any(UI_VERB.search(step) for step in steps)


def _infer_assumptions(brief: VerificationBrief) -> list[str]:
    assumptions: list[str] = []
    if not brief.signals.has_environment_details:
        assumptions.append("Environment details were not provided; using default test environment")
    if not brief.expected_behavior:
        assumptions.append("Expected behavior inferred from issue title and summary")
    if brief.reproduction_steps and not brief.signals.has_numbered_steps:
        assumptions.append("Reproduction steps may need refinement before automation")
    return assumptions


def _concise_goal(brief: VerificationBrief) -> str:
    summary = brief.summary.strip()
    if " — classified as " in summary:
        return summary.split(" — classified as ")[0]
    return summary or "Verify reported bug behavior"
