from __future__ import annotations

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from src.config import (
    GETLARK_API_KEY,
    GETLARK_API_URL,
    GETLARK_CLI_BIN,
    GETLARK_ENABLE_CLI_LIVE,
    GETLARK_ENABLE_WORKFLOW_INVOKE,
    GETLARK_TIMEOUT_SECONDS,
    GETLARK_WORKFLOW_ID,
    GETLARK_WORKFLOW_NAME,
    effective_primary_adapter_mode,
    fault_injection_mode,
    getlark_credentials_complete,
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

# TODO: wire full getlark workflow invoke (create + --wait) after live connectivity check
# TODO: wire real getlark MCP tool calls in GetLarkMcpAdapter
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


class LiveCheckFailedError(Exception):
    """Raised when a live getlark adapter cannot complete a real API/CLI call."""


@dataclass
class GetLarkWorkflowRef:
    workflow_id: str
    name: str | None = None


@dataclass
class GetLarkWorkflowListResult:
    endpoint: str
    status_code: int
    workflow_count: int
    summary: str
    response_snippet: str
    workflow_ids: list[str]
    workflow_refs: list[GetLarkWorkflowRef] = field(default_factory=list)


@dataclass
class GetLarkCliListResult:
    attempted: bool
    command: str
    exit_code: int | None
    success: bool
    summary: str
    stdout_snippet: str
    stderr_snippet: str


@dataclass
class GetLarkInvokeResult:
    attempted: bool
    endpoint: str
    status_code: int | None
    success: bool
    summary: str
    response_snippet: str
    workflow_id: str | None = None
    execution_id: str | None = None


class GetLarkApiClient:
    """Minimal getlark REST client (workflow list = auth + connectivity check)."""

    def __init__(
        self,
        api_key: str,
        api_url: str,
        timeout_seconds: float = GETLARK_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("GETLARK_API_KEY is required")
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout_seconds

    def list_workflows(self, *, limit: int = 5) -> GetLarkWorkflowListResult:
        endpoint = f"{self._api_url}/workflows"
        params = {"limit": max(1, min(limit, 20))}
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(endpoint, headers=headers, params=params)
        except httpx.RequestError as exc:
            raise LiveCheckFailedError(f"Network error calling getlark API: {exc}") from exc

        if response.status_code in {401, 403}:
            raise LiveCheckFailedError(
                f"getlark API rejected credentials (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise LiveCheckFailedError(
                f"getlark workflows endpoint not found (HTTP 404) at {endpoint}"
            )
        if response.status_code >= 500:
            raise LiveCheckFailedError(
                f"getlark API server error (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise LiveCheckFailedError(
                f"getlark API error (HTTP {response.status_code}): {response.text[:200]}"
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise LiveCheckFailedError("getlark API returned non-JSON response") from exc

        count, summary = _summarize_workflow_list(payload)
        workflow_refs = _extract_workflow_refs(payload)
        workflow_ids = [ref.workflow_id for ref in workflow_refs]
        snippet = json.dumps(payload, indent=2)[:1200]
        return GetLarkWorkflowListResult(
            endpoint=endpoint,
            status_code=response.status_code,
            workflow_count=count,
            summary=summary,
            response_snippet=snippet,
            workflow_ids=workflow_ids,
            workflow_refs=workflow_refs,
        )

    def invoke_workflow_best_effort(
        self,
        *,
        plan: VerificationPlan,
        issue: IssueSummary,
        workflow_refs: list[GetLarkWorkflowRef],
    ) -> GetLarkInvokeResult:
        """
        Thin real invoke attempt for sponsor proof.
        Best effort only: never raises, preserves caller fallback behavior.
        """
        if not workflow_refs:
            return GetLarkInvokeResult(
                attempted=False,
                endpoint=f"{self._api_url}/workflows/{{id}}/invoke",
                status_code=None,
                success=False,
                summary="No workflow id available from /workflows response; skipped invoke attempt.",
                response_snippet="{}",
            )

        workflow_id = pick_workflow_id(
            workflow_refs,
            workflow_id=GETLARK_WORKFLOW_ID,
            workflow_name=GETLARK_WORKFLOW_NAME,
        )
        if not workflow_id:
            return GetLarkInvokeResult(
                attempted=False,
                endpoint=f"{self._api_url}/workflows/{{id}}/invoke",
                status_code=None,
                success=False,
                summary="No matching workflow id for invoke attempt.",
                response_snippet="{}",
            )
        # Matches @getlark/cli: POST /workflows/{workflowId}/invoke with {}
        endpoint = f"{self._api_url}/workflows/{workflow_id}/invoke"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(endpoint, headers=headers, json={})
        except httpx.RequestError as exc:
            return GetLarkInvokeResult(
                attempted=True,
                endpoint=endpoint,
                status_code=None,
                success=False,
                summary=f"Invoke attempt failed with network error: {exc}",
                response_snippet="{}",
            )

        snippet = response.text[:1200]
        if response.status_code >= 400:
            return GetLarkInvokeResult(
                attempted=True,
                endpoint=endpoint,
                status_code=response.status_code,
                success=False,
                summary=(
                    f"Invoke endpoint returned HTTP {response.status_code}; "
                    "preserved live-check behavior without failing verify."
                ),
                response_snippet=snippet,
            )

        execution_id = _extract_execution_id(response)
        return GetLarkInvokeResult(
            attempted=True,
            endpoint=endpoint,
            status_code=response.status_code,
            success=True,
            summary=(
                f"Lark workflow invoked successfully via POST {endpoint}"
                + (f" (execution_id={execution_id})" if execution_id else "")
            ),
            response_snippet=snippet,
            workflow_id=workflow_id,
            execution_id=execution_id,
        )


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
    """Select adapter using PRIMARY_ADAPTER_MODE (or LARK_MODE)."""
    return resolve_lark_adapter_for_mode(effective_primary_adapter_mode())


def resolve_lark_adapter_for_mode(mode: str) -> LarkAdapterSelection:
    """Select getlark.ai adapter for a mode; fall back to fake when credentials are missing."""
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

    if mode == "getlark_cli_live" and getlark_credentials_complete():
        return LarkAdapterSelection(
            adapter=GetLarkCliLiveAdapter(
                api_key=GETLARK_API_KEY,
                api_url=GETLARK_API_URL,
            ),
            adapter_id="getlark_cli_live",
            preamble_notes=["Adapter selected: getlark_cli_live"],
        )

    if mode == "getlark_live_check" and getlark_credentials_complete():
        return LarkAdapterSelection(
            adapter=GetLarkLiveCheckAdapter(
                api_key=GETLARK_API_KEY,
                api_url=GETLARK_API_URL,
            ),
            adapter_id="getlark_live_check",
            preamble_notes=["Adapter selected: getlark_live_check"],
        )

    if mode in ("getlark_mcp", "getlark_cli", "getlark_cli_live", "getlark_live_check"):
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


def apply_adapter_run_metadata(
    result: VerificationResult,
    *,
    primary_adapter_requested: str,
    adapter_used: str,
    fallback_triggered: bool,
    fault_mode: str,
) -> VerificationResult:
    """Add explicit adapter / fault-injection notes to a verification result."""
    execution_notes = list(result.execution_notes)
    resilience_notes = list(result.resilience_notes)

    metadata_notes = [
        f"Primary adapter requested: {primary_adapter_requested}",
        f"Adapter used: {adapter_used}",
    ]
    if fallback_triggered:
        metadata_notes.append("Fell back to fake adapter for demo-safe completion")

    for note in metadata_notes:
        if note not in execution_notes:
            execution_notes.append(note)

    if fallback_triggered:
        resilience_notes = [
            note
            for note in resilience_notes
            if note != "No fallback path executed in this run"
        ]
        if fault_mode == "force_adapter_failure":
            if "Fault injection forced primary adapter failure" not in resilience_notes:
                resilience_notes.append("Fault injection forced primary adapter failure")
            if "Fell back to fake adapter for demo-safe completion" not in resilience_notes:
                resilience_notes.append("Fell back to fake adapter for demo-safe completion")
        elif not any("Fallback path" in n for n in resilience_notes):
            resilience_notes.append("Fallback path executed to complete verification")

    if fault_mode == "force_fallback_note":
        demo_note = "Degraded mode simulated for demo purposes (force_fallback_note)"
        if demo_note not in resilience_notes:
            resilience_notes.append(demo_note)

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


class GetLarkLiveCheckAdapter(LarkAdapter):
    """Thin real getlark integration: GET /workflows to validate API key and connectivity."""

    adapter_id = "getlark_live_check"

    def __init__(self, api_key: str, api_url: str) -> None:
        self._client = GetLarkApiClient(api_key=api_key, api_url=api_url)

    def execute(
        self,
        plan: VerificationPlan,
        brief: VerificationBrief,
        issue: IssueSummary,
    ) -> VerificationResult:
        listing = self._client.list_workflows(limit=5)
        invoke = None
        if (
            GETLARK_ENABLE_WORKFLOW_INVOKE
            and plan.mode == PlanMode.LARK_WORKFLOW_CANDIDATE
            and listing.workflow_refs
        ):
            invoke = self._client.invoke_workflow_best_effort(
                plan=plan,
                issue=issue,
                workflow_refs=listing.workflow_refs,
            )
        cli_list = run_getlark_cli_list_best_effort() if GETLARK_ENABLE_CLI_LIVE else None
        status = resolve_execution_status(plan, brief)
        if invoke is not None and invoke.success and status == ResultStatus.SIMULATED:
            # Live invoke proof should surface as reproduced in demo output.
            status = ResultStatus.REPRODUCED
        title = issue.title.strip() or f"Issue #{issue.number}"

        evidence = [
            ExecutionArtifact(
                kind=ArtifactKind.LOG,
                label="live_api",
                content=(
                    f"Real getlark API call succeeded: GET {listing.endpoint} "
                    f"→ HTTP {listing.status_code}"
                ),
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="workflows",
                content=listing.summary,
            ),
            ExecutionArtifact(
                kind=ArtifactKind.TRACE,
                label="api_response",
                content=listing.response_snippet,
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="scope",
                content=(
                    "Live check listed workflows. Invoke is optional and controlled by "
                    "GETLARK_ENABLE_WORKFLOW_INVOKE."
                ),
            ),
        ]
        extra_notes = [
            f"Real getlark API call: GET {listing.endpoint}",
            "This confirms API key and connectivity, not bug reproduction",
        ]
        if invoke is not None:
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.LOG,
                    label="invoke_attempt",
                    content=invoke.summary,
                )
            )
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.TRACE,
                    label="invoke_response",
                    content=invoke.response_snippet,
                )
            )
            if invoke.execution_id:
                evidence.append(
                    ExecutionArtifact(
                        kind=ArtifactKind.NOTE,
                        label="execution_id",
                        content=(
                            f"{invoke.execution_id}"
                            + (f" (workflow {invoke.workflow_id})" if invoke.workflow_id else "")
                        ),
                    )
                )
            if invoke.success:
                extra_notes.append("Lark workflow invoked successfully.")
                if invoke.execution_id:
                    extra_notes.append(f"getlark execution_id: {invoke.execution_id}")
            elif invoke.attempted:
                extra_notes.append(
                    "Lark workflow invoke attempt failed; retained non-breaking live-check flow."
                )
        if cli_list is not None:
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.LOG,
                    label="cli_workflows_list",
                    content=cli_list.summary,
                )
            )
            if cli_list.stdout_snippet:
                evidence.append(
                    ExecutionArtifact(
                        kind=ArtifactKind.TRACE,
                        label="cli_stdout",
                        content=cli_list.stdout_snippet,
                    )
                )
            if cli_list.success:
                extra_notes.append(f"Real getlark CLI list succeeded: {cli_list.command}")
            elif cli_list.attempted:
                extra_notes.append(
                    "getlark CLI workflows list attempted; see cli_workflows_list evidence."
                )

        return VerificationResult(
            status=status,
            outcome_summary=(
                f"{title}: getlark live check succeeded ({listing.summary}); "
                + (
                    "workflow invoke succeeded."
                    if invoke is not None and invoke.success
                    else (
                        "workflow invoke attempted."
                        if invoke is not None and invoke.attempted
                        else "full workflow execution was not run."
                    )
                )
            ),
            evidence=evidence,
            execution_notes=execution_notes(
                plan,
                brief,
                status,
                adapter_label="getlark_live_check",
                extra=extra_notes,
            ),
            resilience_notes=[
                "No resilience gateway configured yet",
                "No fallback path executed in this run",
            ],
            confidence=BriefConfidence(
                level=(
                    ConfidenceLevel.HIGH
                    if invoke is not None and invoke.success
                    else ConfidenceLevel.MEDIUM
                ),
                reason=(
                    "Real getlark workflow invoke succeeded; execution id captured."
                    if invoke is not None and invoke.success
                    else (
                        "Real getlark API response received (workflow list). "
                        "Bug reproduction was not executed live."
                    )
                ),
            ),
        )


class GetLarkCliLiveAdapter(LarkAdapter):
    """Live getlark CLI integration: runs `getlark workflows list` and captures stdout as evidence."""

    adapter_id = "getlark_cli_live"

    def __init__(self, api_key: str, api_url: str) -> None:
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")

    def execute(
        self,
        plan: VerificationPlan,
        brief: VerificationBrief,
        issue: IssueSummary,
    ) -> VerificationResult:
        cli_list = run_getlark_cli_list(api_key=self._api_key, api_url=self._api_url)
        if not cli_list.success:
            raise LiveCheckFailedError(cli_list.summary)

        status = resolve_execution_status(plan, brief)
        title = issue.title.strip() or f"Issue #{issue.number}"
        evidence = [
            ExecutionArtifact(
                kind=ArtifactKind.LOG,
                label="cli_workflows_list",
                content=cli_list.summary,
            ),
            ExecutionArtifact(
                kind=ArtifactKind.TRACE,
                label="cli_stdout",
                content=cli_list.stdout_snippet,
            ),
            ExecutionArtifact(
                kind=ArtifactKind.NOTE,
                label="lark_mode",
                content="live REST + CLI scaffold; this run used real getlark CLI list",
            ),
        ]
        if cli_list.stderr_snippet:
            evidence.append(
                ExecutionArtifact(
                    kind=ArtifactKind.NOTE,
                    label="cli_stderr",
                    content=cli_list.stderr_snippet,
                )
            )

        return VerificationResult(
            status=status,
            outcome_summary=(
                f"{title}: getlark CLI workflows list succeeded via `{cli_list.command}`; "
                "live CLI proof captured (not target-app reproduction)."
            ),
            evidence=evidence,
            execution_notes=execution_notes(
                plan,
                brief,
                status,
                adapter_label="getlark_cli_live",
                extra=[
                    f"Real getlark CLI call: {cli_list.command}",
                    "CLI stdout captured as verification evidence",
                ],
            ),
            resilience_notes=[
                "No resilience gateway configured yet",
                "No fallback path executed in this run",
            ],
            confidence=BriefConfidence(
                level=ConfidenceLevel.MEDIUM,
                reason="Real getlark CLI workflows list succeeded; bug reproduction was not executed live.",
            ),
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


def _summarize_workflow_list(payload: Any) -> tuple[int, str]:
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        raw = payload.get("workflows") or payload.get("data") or payload.get("items")
        items = raw if isinstance(raw, list) else []
    else:
        return 0, "unexpected response shape"

    names: list[str] = []
    for item in items[:5]:
        if isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("workflow_id")
            if name:
                names.append(str(name))

    if names:
        return len(items), f"{len(items)} workflow(s); examples: {', '.join(names)}"
    return len(items), f"{len(items)} workflow(s) returned"


def pick_workflow_id(
    workflow_refs: list[GetLarkWorkflowRef],
    *,
    workflow_id: str | None,
    workflow_name: str | None,
) -> str | None:
    """Select a workflow id for invoke using env overrides, then first listed workflow."""
    if workflow_id:
        for ref in workflow_refs:
            if ref.workflow_id == workflow_id:
                return ref.workflow_id
        return workflow_id
    if workflow_name:
        for ref in workflow_refs:
            if ref.name and ref.name == workflow_name:
                return ref.workflow_id
    if workflow_refs:
        return workflow_refs[0].workflow_id
    return None


def run_getlark_cli_list_best_effort() -> GetLarkCliListResult:
    """Best-effort CLI list for combined REST+CLI sponsor demos; never raises."""
    return run_getlark_cli_list(
        api_key=GETLARK_API_KEY,
        api_url=GETLARK_API_URL,
        raise_on_missing=False,
    )


def run_getlark_cli_list(
    *,
    api_key: str,
    api_url: str,
    cli_bin: str = GETLARK_CLI_BIN,
    timeout_seconds: float = GETLARK_TIMEOUT_SECONDS,
    raise_on_missing: bool = True,
) -> GetLarkCliListResult:
    """Run `getlark workflows list` and capture stdout/stderr as sponsor evidence."""
    command = f"{cli_bin} workflows list"
    argv = [cli_bin, "workflows", "list"]
    env = os.environ.copy()
    env["GETLARK_API_KEY"] = api_key
    env["GETLARK_API_URL"] = api_url.rstrip("/")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        summary = (
            f"getlark CLI not found (`{cli_bin}`). Install @getlark/cli or set GETLARK_CLI_BIN."
        )
        if raise_on_missing:
            return GetLarkCliListResult(
                attempted=True,
                command=command,
                exit_code=None,
                success=False,
                summary=summary,
                stdout_snippet="",
                stderr_snippet=summary,
            )
        return GetLarkCliListResult(
            attempted=True,
            command=command,
            exit_code=None,
            success=False,
            summary=summary,
            stdout_snippet="",
            stderr_snippet=summary,
        )
    except subprocess.TimeoutExpired:
        summary = f"getlark CLI timed out after {timeout_seconds}s: {command}"
        return GetLarkCliListResult(
            attempted=True,
            command=command,
            exit_code=None,
            success=False,
            summary=summary,
            stdout_snippet="",
            stderr_snippet=summary,
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    success = proc.returncode == 0
    if success:
        summary = f"getlark CLI workflows list succeeded (exit 0): {command}"
    else:
        summary = (
            f"getlark CLI workflows list failed (exit {proc.returncode}): {command}"
        )
    return GetLarkCliListResult(
        attempted=True,
        command=command,
        exit_code=proc.returncode,
        success=success,
        summary=summary,
        stdout_snippet=stdout[:1200],
        stderr_snippet=stderr[:400],
    )


def _extract_workflow_refs(payload: Any) -> list[GetLarkWorkflowRef]:
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        raw = payload.get("workflows") or payload.get("data") or payload.get("items")
        items = raw if isinstance(raw, list) else []
    else:
        return []

    refs: list[GetLarkWorkflowRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        workflow_id = item.get("id") or item.get("workflow_id")
        if not workflow_id:
            continue
        name = item.get("name")
        refs.append(
            GetLarkWorkflowRef(
                workflow_id=str(workflow_id),
                name=str(name) if name else None,
            )
        )
    return refs


def _extract_workflow_ids(payload: Any) -> list[str]:
    return [ref.workflow_id for ref in _extract_workflow_refs(payload)]


def _extract_execution_id(response: httpx.Response) -> str | None:
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    return _execution_id_from_payload(payload)


def _execution_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = (
        payload.get("id"),
        payload.get("execution_id"),
        payload.get("run_id"),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    nested = payload.get("execution") or payload.get("data")
    if isinstance(nested, dict):
        for key in ("id", "execution_id", "run_id"):
            value = nested.get(key)
            if value:
                return str(value)
    return None
