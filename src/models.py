from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class TriggerType(str, Enum):
    MANUAL = "manual"


class RunStage(str, Enum):
    QUEUED = "queued"
    FETCHING = "fetching"
    NORMALIZED = "normalized"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(str, Enum):
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"


class VerifyRequest(BaseModel):
    owner: str | None = None
    repo: str | None = None
    issue_number: int
    trigger: TriggerType = TriggerType.MANUAL

    @field_validator("issue_number")
    @classmethod
    def issue_number_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("issue_number must be greater than 0")
        return value


class ReplayRequest(BaseModel):
    run_id: str


class IssueSummary(BaseModel):
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    author: str
    url: str


class CommentSummary(BaseModel):
    author: str
    body: str
    created_at: str
    url: str


class ReportQualityHints(BaseModel):
    has_body: bool
    has_repro_signals: bool
    comment_count: int
    label_count: int


class EvidencePacket(BaseModel):
    problem_statement: str
    raw_report_text: str
    report_quality_hints: ReportQualityHints
    combined_text: str
    recommended_next_step: str = "parse_with_llm"


class BriefClassification(str, Enum):
    REPRODUCIBLE_CANDIDATE = "reproducible_candidate"
    BLOCKED_MISSING_INFO = "blocked_missing_info"
    UNCLEAR = "unclear"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationMode(str, Enum):
    MANUAL_REVIEW = "manual_review"
    LARK_WORKFLOW_CANDIDATE = "lark_workflow_candidate"


class BriefSignals(BaseModel):
    has_numbered_steps: bool
    has_expected_behavior: bool
    has_actual_behavior: bool
    has_error_message: bool
    has_environment_details: bool


class BriefConfidence(BaseModel):
    level: ConfidenceLevel
    reason: str


class VerificationBrief(BaseModel):
    summary: str
    classification: BriefClassification
    reproduction_steps: list[str]
    expected_behavior: str
    actual_behavior: str
    missing_information: list[str]
    signals: BriefSignals
    confidence: BriefConfidence
    recommended_verification_mode: VerificationMode


class PlanMode(str, Enum):
    MANUAL_REVIEW = "manual_review"
    LARK_WORKFLOW_CANDIDATE = "lark_workflow_candidate"


class PlanTargetType(str, Enum):
    UI_FLOW = "ui_flow"
    UNKNOWN = "unknown"


class VerificationPlan(BaseModel):
    mode: PlanMode
    target_type: PlanTargetType
    workflow_name: str
    goal: str
    proposed_steps: list[str]
    assumptions: list[str]
    blockers: list[str]


class ArtifactKind(str, Enum):
    LOG = "log"
    NOTE = "note"
    SCREENSHOT = "screenshot"
    TRACE = "trace"


class ExecutionArtifact(BaseModel):
    kind: ArtifactKind
    label: str
    content: str
    path: str | None = None


class ResultStatus(str, Enum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    BLOCKED = "blocked"
    SIMULATED = "simulated"


class VerificationResult(BaseModel):
    status: ResultStatus
    outcome_summary: str
    evidence: list[ExecutionArtifact]
    execution_notes: list[str]
    resilience_notes: list[str]
    confidence: BriefConfidence


class VerifyResponse(BaseModel):
    run_id: str
    status: RunStatus
    issue: IssueSummary
    comments: list[CommentSummary]
    evidence_packet: EvidencePacket
    verification_brief: VerificationBrief | None = None
    verification_plan: VerificationPlan | None = None
    verification_result: VerificationResult | None = None
    github_comment_url: str | None = None
    adapter_used: str | None = None
    primary_adapter_requested: str | None = None
    fallback_triggered: bool = False


class RunInputParams(BaseModel):
    owner: str
    repo: str
    issue_number: int
    trigger: TriggerType = TriggerType.MANUAL


class StoredRun(BaseModel):
    run_id: str
    created_at: datetime
    input_params: RunInputParams
    stage: RunStage
    status: RunStatus
    normalized_payload: VerifyResponse | None = None
    error: str | None = None

    @staticmethod
    def new_id() -> str:
        return uuid4().hex[:12]

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(timezone.utc)


class RunSummary(BaseModel):
    run_id: str
    created_at: datetime
    stage: RunStage
    status: RunStatus
    owner: str
    repo: str
    issue_number: int
    error: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    error_type: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
