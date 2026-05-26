from __future__ import annotations

import re

from src.config import GITHUB_OWNER, GITHUB_REPO, require_github_token
from src.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from src.models import (
    CommentSummary,
    EvidencePacket,
    IssueSummary,
    ReportQualityHints,
    RunInputParams,
    RunStage,
    RunStatus,
    RunSummary,
    StoredRun,
    TriggerType,
    VerifyRequest,
    VerifyResponse,
)
from src.run_store import RunNotFoundError, RunStore

# TODO: parser module — extract structured repro steps from evidence_packet.combined_text
# TODO: lark_adapter module — invoke Lark CLI/MCP for agent workflows
# TODO: resilience_gateway module — route LLM/MCP calls with graceful fallback
# TODO: fault_injection module — simulate MCP/LLM outages for demo/resilience testing
# TODO: github_comment_poster module — post verification evidence back to the issue

REPRO_SIGNAL_PATTERN = re.compile(
    r"\b(repro(?:duce| steps?)?|steps to reproduce|expected|actual|stack trace|error message)\b",
    re.IGNORECASE,
)


class ServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        status_code: int = 400,
        context: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.context = context or {}


class VerificationService:
    def __init__(
        self,
        run_store: RunStore | None = None,
        github_client: GitHubClient | None = None,
    ) -> None:
        self.run_store = run_store or RunStore()
        self._github_client = github_client

    def _resolve_github_client(self) -> GitHubClient:
        if self._github_client is not None:
            return self._github_client
        token = require_github_token()
        return GitHubClient(token=token)

    def _resolve_owner_repo(self, owner: str | None, repo: str | None) -> tuple[str, str]:
        resolved_owner = owner or GITHUB_OWNER
        resolved_repo = repo or GITHUB_REPO
        if not resolved_owner or not resolved_repo:
            raise ServiceError(
                "owner and repo are required (provide in request or set GITHUB_OWNER/GITHUB_REPO)",
                error_type="missing_repo_context",
                status_code=400,
            )
        return resolved_owner, resolved_repo

    async def verify(self, request: VerifyRequest) -> VerifyResponse:
        owner, repo = self._resolve_owner_repo(request.owner, request.repo)
        run = StoredRun(
            run_id=StoredRun.new_id(),
            created_at=StoredRun.now_utc(),
            input_params=RunInputParams(
                owner=owner,
                repo=repo,
                issue_number=request.issue_number,
                trigger=request.trigger,
            ),
            stage=RunStage.QUEUED,
            status=RunStatus.QUEUED,
        )
        self.run_store.save(run)

        run.stage = RunStage.FETCHING
        self.run_store.save(run)

        try:
            client = self._resolve_github_client()
            issue = await client.fetch_issue(owner, repo, request.issue_number)
            comments = await client.fetch_issue_comments(owner, repo, request.issue_number)
            response = self._build_verify_response(run.run_id, issue, comments)

            run.stage = RunStage.COMPLETED
            run.status = RunStatus.COMPLETED
            run.normalized_payload = response
            self.run_store.save(run)
            return response

        except GitHubNotFoundError as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(
                str(exc),
                error_type="not_found",
                status_code=404,
                context=exc.context,
            ) from exc
        except GitHubAuthError as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(str(exc), error_type="auth_error", status_code=401) from exc
        except GitHubRateLimitError as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(str(exc), error_type="rate_limit", status_code=429) from exc
        except GitHubClientError as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(
                str(exc),
                error_type=exc.error_type or "github_error",
                status_code=exc.status_code or 502,
                context=exc.context,
            ) from exc
        except ValueError as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(str(exc), error_type="configuration_error", status_code=400) from exc
        except Exception as exc:
            self._mark_failed(run, str(exc))
            raise ServiceError(
                f"Unexpected verification failure: {exc}",
                error_type="internal_error",
                status_code=500,
            ) from exc

    async def replay(self, run_id: str) -> VerifyResponse:
        try:
            run = self.run_store.load(run_id)
        except RunNotFoundError as exc:
            raise ServiceError(str(exc), error_type="run_not_found", status_code=404) from exc

        if run.normalized_payload is not None:
            return run.normalized_payload

        if run.status == RunStatus.FAILED and run.error:
            raise ServiceError(
                f"Run {run_id} failed previously: {run.error}",
                error_type="run_failed",
                status_code=409,
            )

        request = VerifyRequest(
            owner=run.input_params.owner,
            repo=run.input_params.repo,
            issue_number=run.input_params.issue_number,
            trigger=run.input_params.trigger,
        )
        return await self.verify(request)

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        return self.run_store.list_recent(limit=limit)

    def _mark_failed(self, run: StoredRun, error: str) -> None:
        run.stage = RunStage.FAILED
        run.status = RunStatus.FAILED
        run.error = error
        self.run_store.save(run)

    def _build_verify_response(
        self,
        run_id: str,
        issue: IssueSummary,
        comments: list[CommentSummary],
    ) -> VerifyResponse:
        raw_report_text = self._build_raw_report_text(issue, comments)
        combined_text = self._build_combined_text(issue, comments)
        hints = ReportQualityHints(
            has_body=bool(issue.body.strip()),
            has_repro_signals=bool(REPRO_SIGNAL_PATTERN.search(combined_text)),
            comment_count=len(comments),
            label_count=len(issue.labels),
        )
        evidence = EvidencePacket(
            problem_statement=issue.title.strip() or "Untitled issue",
            raw_report_text=raw_report_text,
            report_quality_hints=hints,
            combined_text=combined_text,
            recommended_next_step="parse_with_llm",
        )
        return VerifyResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            issue=issue,
            comments=comments,
            evidence_packet=evidence,
        )

    @staticmethod
    def _build_raw_report_text(issue: IssueSummary, comments: list[CommentSummary]) -> str:
        parts = [issue.body.strip()] if issue.body.strip() else []
        parts.extend(comment.body.strip() for comment in comments if comment.body.strip())
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _build_combined_text(issue: IssueSummary, comments: list[CommentSummary]) -> str:
        sections = [
            f"Issue #{issue.number}: {issue.title}",
            f"State: {issue.state}",
            f"Labels: {', '.join(issue.labels) if issue.labels else '(none)'}",
            f"Author: {issue.author}",
            "",
            issue.body.strip() or "(no issue body)",
        ]
        for index, comment in enumerate(comments, start=1):
            sections.extend(
                [
                    "",
                    f"Comment {index} by {comment.author} ({comment.created_at}):",
                    comment.body.strip() or "(empty comment)",
                ]
            )
        return "\n".join(sections).strip()
