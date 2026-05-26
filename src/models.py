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


class VerifyResponse(BaseModel):
    run_id: str
    status: RunStatus
    issue: IssueSummary
    comments: list[CommentSummary]
    evidence_packet: EvidencePacket


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
