from __future__ import annotations

import re
from dataclasses import dataclass

from src.config import (
    GITHUB_OWNER,
    GITHUB_REPO,
    effective_primary_adapter_mode,
    fault_injection_mode,
    require_github_token,
)
from src.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from src.github_comment_poster import (
    GitHubCommentPoster,
    GitHubCommentPosterError,
    filter_larkguard_comments,
    render_verification_comment,
    should_post_github_comment,
)
from src.lark_adapter import (
    FakeLarkAdapter,
    LarkAdapter,
    LarkAdapterSelection,
    annotate_result,
    apply_adapter_run_metadata,
    plan_verification,
    resolve_lark_adapter_for_mode,
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
    VerificationPlan,
    VerificationResult,
    VerifyRequest,
    VerifyResponse,
)
from src.parser import Parser, default_parser
from src.run_store import RunNotFoundError, RunStore

# TODO: resilience_gateway module — wrap adapter execution with richer fallback policies

REPRO_SIGNAL_PATTERN = re.compile(
    r"\b(repro(?:duce| steps?)?|steps to reproduce|expected|actual|stack trace|error message)\b",
    re.IGNORECASE,
)


@dataclass
class AdapterExecutionMeta:
    primary_adapter_requested: str
    adapter_used: str
    fallback_triggered: bool
    result: VerificationResult


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
        parser: Parser | None = None,
        lark_adapter: LarkAdapter | None = None,
        comment_poster: GitHubCommentPoster | None = None,
    ) -> None:
        self.run_store = run_store or RunStore()
        self._github_client = github_client
        self._parser = parser or default_parser()
        self._lark_adapter_override = lark_adapter
        self._comment_poster = comment_poster

    def _resolve_github_client(self) -> GitHubClient:
        if self._github_client is not None:
            return self._github_client
        token = require_github_token()
        return GitHubClient(token=token)

    def _resolve_comment_poster(self) -> GitHubCommentPoster:
        if self._comment_poster is not None:
            return self._comment_poster
        return GitHubCommentPoster(token=require_github_token())

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
            comments = filter_larkguard_comments(comments)
            response = self._build_verify_response(run.run_id, issue, comments)
            response = await self._maybe_post_github_comment(
                response, owner, repo, request.issue_number
            )

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
            return self._ensure_full_payload(run)

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

    def _primary_selection(self) -> LarkAdapterSelection:
        if self._lark_adapter_override is not None:
            adapter = self._lark_adapter_override
            adapter_id = getattr(adapter, "adapter_id", "custom")
            return LarkAdapterSelection(
                adapter=adapter,
                adapter_id=adapter_id,
                preamble_notes=[f"Adapter selected: {adapter_id}"],
            )
        return resolve_lark_adapter_for_mode(effective_primary_adapter_mode())

    def _execute_with_adapter(
        self,
        plan: VerificationPlan,
        brief,
        issue: IssueSummary,
    ) -> AdapterExecutionMeta:
        primary_requested = effective_primary_adapter_mode()
        fault_mode = fault_injection_mode()
        selection = self._primary_selection()

        if (
            fault_mode == "force_adapter_failure"
            and primary_requested != "fake"
            and selection.adapter_id != "fake"
        ):
            fake = FakeLarkAdapter()
            result = fake.execute(plan, brief, issue)
            result = annotate_result(
                result,
                LarkAdapterSelection(
                    adapter=fake,
                    adapter_id="fake",
                    preamble_notes=[
                        f"Fault injection simulated failure of primary adapter ({selection.adapter_id})",
                        "Adapter selected: fake",
                    ],
                ),
            )
            result = apply_adapter_run_metadata(
                result,
                primary_adapter_requested=primary_requested,
                adapter_used="fake",
                fallback_triggered=True,
                fault_mode=fault_mode,
            )
            result = self._inject_fault_execution_note(result, selection.adapter_id)
            return AdapterExecutionMeta(
                primary_adapter_requested=primary_requested,
                adapter_used="fake",
                fallback_triggered=True,
                result=result,
            )

        result = selection.adapter.execute(plan, brief, issue)
        result = annotate_result(result, selection)
        fallback_triggered = (
            selection.adapter_id == "fake"
            and primary_requested in ("getlark_mcp", "getlark_cli")
        )
        result = apply_adapter_run_metadata(
            result,
            primary_adapter_requested=primary_requested,
            adapter_used=selection.adapter_id,
            fallback_triggered=fallback_triggered,
            fault_mode=fault_mode,
        )
        return AdapterExecutionMeta(
            primary_adapter_requested=primary_requested,
            adapter_used=selection.adapter_id,
            fallback_triggered=fallback_triggered,
            result=result,
        )

    @staticmethod
    def _inject_fault_execution_note(
        result: VerificationResult, failed_adapter_id: str
    ) -> VerificationResult:
        note = f"Fault injection forced primary adapter failure ({failed_adapter_id})"
        if note in result.execution_notes:
            return result
        return result.model_copy(
            update={"execution_notes": list(result.execution_notes) + [note]}
        )

    async def _maybe_post_github_comment(
        self,
        response: VerifyResponse,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> VerifyResponse:
        if not should_post_github_comment(response):
            return response.model_copy(update={"comment_action": "skipped"})

        body = render_verification_comment(response)
        try:
            poster = self._resolve_comment_poster()
            post_result = await poster.upsert_managed_comment(
                owner, repo, issue_number, body
            )
            return response.model_copy(
                update={
                    "github_comment_url": post_result.url,
                    "github_comment_id": post_result.comment_id,
                    "comment_action": post_result.action,
                }
            )
        except GitHubCommentPosterError as exc:
            return self._append_comment_post_failure(response, str(exc))

    @staticmethod
    def _append_comment_post_failure(
        response: VerifyResponse, error: str
    ) -> VerifyResponse:
        if response.verification_result is None:
            return response
        notes = list(response.verification_result.execution_notes)
        notes.append(f"GitHub comment post skipped/failed: {error}")
        updated_result = response.verification_result.model_copy(
            update={"execution_notes": notes}
        )
        return response.model_copy(update={"verification_result": updated_result})

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
        brief = self._parser.parse(issue, comments, evidence)
        plan = plan_verification(brief)
        execution = self._execute_with_adapter(plan, brief, issue)
        return VerifyResponse(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            issue=issue,
            comments=comments,
            evidence_packet=evidence,
            verification_brief=brief,
            verification_plan=plan,
            verification_result=execution.result,
            adapter_used=execution.adapter_used,
            primary_adapter_requested=execution.primary_adapter_requested,
            fallback_triggered=execution.fallback_triggered,
        )

    def _ensure_full_payload(self, run: StoredRun) -> VerifyResponse:
        payload = run.normalized_payload
        if payload is None:
            raise ServiceError("Run has no stored payload", error_type="run_empty", status_code=409)

        updated = False
        if payload.verification_brief is None:
            brief = self._parser.parse(
                payload.issue, payload.comments, payload.evidence_packet
            )
            payload = payload.model_copy(update={"verification_brief": brief})
            updated = True

        if payload.verification_brief is not None and payload.verification_plan is None:
            plan = plan_verification(payload.verification_brief)
            payload = payload.model_copy(update={"verification_plan": plan})
            updated = True

        if (
            payload.verification_brief is not None
            and payload.verification_plan is not None
            and payload.verification_result is None
        ):
            execution = self._execute_with_adapter(
                payload.verification_plan,
                payload.verification_brief,
                payload.issue,
            )
            payload = payload.model_copy(
                update={
                    "verification_result": execution.result,
                    "adapter_used": execution.adapter_used,
                    "primary_adapter_requested": execution.primary_adapter_requested,
                    "fallback_triggered": execution.fallback_triggered,
                }
            )
            updated = True

        if updated:
            run.normalized_payload = payload
            self.run_store.save(run)
        return payload

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
