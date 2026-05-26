from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from src.config import COMMENT_ONLY_ON_COMPLETED, ENABLE_GITHUB_COMMENTS, fault_injection_mode
from src.models import CommentSummary, ResultStatus, RunStatus, VerificationResult, VerifyResponse

LARKGUARD_MANAGED_MARKER = "<!-- larkguard:managed -->"
LARKGUARD_RUN_MARKER = "<!-- larkguard-run:"

CommentAction = Literal["created", "updated", "skipped"]


@dataclass
class CommentPostResult:
    url: str | None
    comment_id: int | None
    action: CommentAction


class GitHubCommentPosterError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.context = context or {}


def is_larkguard_comment(body: str) -> bool:
    """Detect LarkGuard-managed comments so they are not parsed as issue evidence."""
    return LARKGUARD_MANAGED_MARKER in body or LARKGUARD_RUN_MARKER in body


def filter_larkguard_comments(comments: list[CommentSummary]) -> list[CommentSummary]:
    return [comment for comment in comments if not is_larkguard_comment(comment.body)]


def should_post_github_comment(response: VerifyResponse) -> bool:
    if not ENABLE_GITHUB_COMMENTS:
        return False
    if COMMENT_ONLY_ON_COMPLETED and response.status != RunStatus.COMPLETED:
        return False
    return response.verification_result is not None


def render_verification_comment(response: VerifyResponse) -> str:
    """Build a compact markdown comment for a completed verification run."""
    result = response.verification_result
    plan = response.verification_plan
    brief = response.verification_brief
    if result is None:
        return (
            f"{LARKGUARD_MANAGED_MARKER}\n"
            f"<!-- larkguard-run:{response.run_id} -->\n\n"
            "## LarkGuard Verification Result\n\nRun incomplete."
        )

    status_label = _render_status_label(result, response)
    workflow = plan.workflow_name if plan else "unknown"
    adapter = response.adapter_used or "unknown"

    lines: list[str] = [
        LARKGUARD_MANAGED_MARKER,
        f"<!-- larkguard-run:{response.run_id} -->",
        "",
        "## LarkGuard Verification Result",
        "",
    ]

    banner = _demo_banner(response)
    if banner:
        lines.extend([banner, ""])

    lines.extend(
        [
            f"- **Status:** {status_label}",
            f"- **Workflow:** `{workflow}`",
            f"- **Adapter:** `{adapter}`",
            f"- **Run ID:** `{response.run_id}`",
        ]
    )
    if response.adapter_used and response.adapter_used.startswith("getlark"):
        lines.append("- **Execution engine:** Powered by [getlark.ai](https://getlark.ai)")
    if response.parser_used == "truefoundry_gateway":
        lines.append("- **Parser engine:** Powered by [TrueFoundry AI Gateway](https://www.truefoundry.com)")
    if response.fallback_triggered:
        lines.append(
            f"- **Fallback:** yes (`{response.primary_adapter_requested}` → `{adapter}`)"
        )
    if response.parser_used:
        lines.append(f"- **Parser:** `{response.parser_used}`")
        if response.parser_fallback_triggered:
            lines.append(
                f"- **Parser fallback:** yes (`{response.parser_requested}` → "
                f"`{response.parser_used}`)"
            )
    if response.parser_duration_ms is not None or response.adapter_duration_ms is not None:
        parts: list[str] = []
        if response.parser_duration_ms is not None:
            parts.append(f"parser {response.parser_duration_ms}ms")
        if response.adapter_duration_ms is not None:
            parts.append(f"adapter {response.adapter_duration_ms}ms")
        lines.append(f"- **Timings:** {', '.join(parts)}")

    lines.extend(["", result.outcome_summary, "", "**Evidence**"])
    execution_id_artifact = None
    if result.evidence:
        execution_id_artifact = next(
            (artifact for artifact in result.evidence if artifact.label == "execution_id"),
            None,
        )
        shown = result.evidence[:4]
        shown_labels = {artifact.label for artifact in shown}
        if execution_id_artifact is not None and execution_id_artifact.label not in shown_labels:
            shown = shown[:3] + [execution_id_artifact]
        for artifact in shown:
            text = artifact.content if len(artifact.content) <= 120 else artifact.content[:117] + "..."
            lines.append(f"- `{artifact.label}`: {text}")
        if execution_id_artifact is not None:
            lines.append(f"- **Lark execution:** `{execution_id_artifact.content}`")
        invoked = next(
            (
                artifact
                for artifact in result.evidence
                if artifact.label == "invoke_attempt"
                and "invoked successfully" in artifact.content.lower()
            ),
            None,
        )
        if invoked is not None:
            lines.append("- **Lark invoke:** workflow invoked successfully.")
    else:
        lines.append("- _(none)_")

    if brief and brief.missing_information:
        lines.extend(["", "**Missing information**"])
        for item in brief.missing_information[:4]:
            lines.append(f"- {item}")

    lines.extend(["", "**Resilience**"])
    for note in result.resilience_notes[:4]:
        lines.append(f"- {note}")

    lines.extend(["", f"**Next:** {_next_action(result.status)}", ""])
    lines.append(
        "_Managed by LarkGuard. Re-run verification to update this comment._"
    )
    return "\n".join(lines)


def _demo_banner(response: VerifyResponse) -> str:
    if response.fallback_triggered and fault_injection_mode() == "force_adapter_failure":
        return (
            "> **Degraded run** — primary adapter failure was simulated; "
            "completed via **fake** fallback."
        )
    if fault_injection_mode() == "force_fallback_note":
        return "> **Demo note** — resilience degradation simulated for demonstration."
    if response.fallback_triggered:
        return "> **Degraded run** — fell back to fake adapter to complete verification."
    return ""


def _status_label(status: ResultStatus) -> str:
    labels = {
        ResultStatus.BLOCKED: "Blocked",
        ResultStatus.SIMULATED: "Simulated",
        ResultStatus.REPRODUCED: "Reproduced",
        ResultStatus.NOT_REPRODUCED: "Not reproduced",
    }
    return labels.get(status, status.value.replace("_", " ").title())


def _render_status_label(result: VerificationResult, response: VerifyResponse) -> str:
    label = _status_label(result.status)
    if (
        result.status == ResultStatus.REPRODUCED
        and response.adapter_used
        and response.adapter_used.startswith("getlark")
        and any(artifact.label == "execution_id" for artifact in (result.evidence or []))
    ):
        return (
            f"{label} "
            "(live getlark workflow execution proof — not target-app reproduction)"
        )
    return label


def _next_action(status: ResultStatus) -> str:
    if status == ResultStatus.BLOCKED:
        return "Ask reporter for repro steps, expected/actual behavior, and environment details."
    if status in (ResultStatus.SIMULATED, ResultStatus.REPRODUCED):
        return "Route to engineering; connect live getlark execution when ready."
    if status == ResultStatus.NOT_REPRODUCED:
        return "Request clarification or retry verification manually."
    return "Review artifacts and decide next steps."


@dataclass
class _IssueCommentRecord:
    comment_id: int
    body: str
    html_url: str


class GitHubCommentPoster:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def upsert_managed_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
    ) -> CommentPostResult:
        existing = await self._find_managed_comment(owner, repo, issue_number)
        if existing is not None:
            url = await self._update_issue_comment(owner, repo, existing.comment_id, body)
            return CommentPostResult(
                url=url,
                comment_id=existing.comment_id,
                action="updated",
            )

        url, comment_id = await self._create_issue_comment(owner, repo, issue_number, body)
        return CommentPostResult(url=url, comment_id=comment_id, action="created")

    async def _find_managed_comment(
        self, owner: str, repo: str, issue_number: int
    ) -> _IssueCommentRecord | None:
        comments = await self._list_issue_comments(owner, repo, issue_number)
        legacy: _IssueCommentRecord | None = None
        for comment in comments:
            if LARKGUARD_MANAGED_MARKER in comment.body:
                return comment
            if LARKGUARD_RUN_MARKER in comment.body and legacy is None:
                legacy = comment
        return legacy

    async def _list_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[_IssueCommentRecord]:
        url = (
            f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
            "?per_page=100"
        )
        data = await self._request_json("GET", url)
        if not isinstance(data, list):
            raise GitHubCommentPosterError("Unexpected GitHub comments list response")

        records: list[_IssueCommentRecord] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                records.append(
                    _IssueCommentRecord(
                        comment_id=int(item["id"]),
                        body=str(item.get("body") or ""),
                        html_url=str(item.get("html_url") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records

    async def _create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> tuple[str, int]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        data = await self._request_json("POST", url, json_body={"body": body})
        return self._parse_comment_response(data, url)

    async def _update_issue_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> str:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/comments/{comment_id}"
        data = await self._request_json("PATCH", url, json_body={"body": body})
        html_url, _ = self._parse_comment_response(data, url)
        return html_url

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers,
                    json=json_body,
                )
            except httpx.RequestError as exc:
                raise GitHubCommentPosterError(
                    f"GitHub comment API request failed: {exc}",
                    context={"url": url, "method": method},
                ) from exc

        if response.status_code == 401:
            raise GitHubCommentPosterError(
                "GitHub rejected comment request (authentication failed)",
                status_code=401,
                context={"url": url},
            )
        if response.status_code == 403:
            raise GitHubCommentPosterError(
                "GitHub rejected comment request (insufficient permissions or rate limit)",
                status_code=403,
                context={"url": url, "body": response.text[:300]},
            )
        if response.status_code == 404:
            raise GitHubCommentPosterError(
                "GitHub resource not found for comment request",
                status_code=404,
                context={"url": url},
            )
        if response.status_code >= 400:
            raise GitHubCommentPosterError(
                f"GitHub comment API error ({response.status_code})",
                status_code=response.status_code,
                context={"url": url, "body": response.text[:300]},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubCommentPosterError(
                "GitHub returned malformed JSON for comment request",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _parse_comment_response(data: Any, url: str) -> tuple[str, int]:
        if not isinstance(data, dict):
            raise GitHubCommentPosterError(
                "Unexpected GitHub comment response shape",
                context={"url": url},
            )
        try:
            html_url = str(data["html_url"])
            comment_id = int(data["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubCommentPosterError(
                "GitHub comment response missing id or html_url",
                context={"url": url},
            ) from exc
        return html_url, comment_id
