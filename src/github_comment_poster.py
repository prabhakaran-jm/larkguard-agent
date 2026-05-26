from __future__ import annotations

from typing import Any

import httpx

from src.config import COMMENT_ONLY_ON_COMPLETED, ENABLE_GITHUB_COMMENTS, fault_injection_mode
from src.models import CommentSummary, ResultStatus, RunStatus, VerifyResponse

LARKGUARD_RUN_MARKER = "<!-- larkguard-run:"


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
    """Detect comments posted by LarkGuard so they are not parsed as issue evidence."""
    return LARKGUARD_RUN_MARKER in body


def filter_larkguard_comments(comments: list[CommentSummary]) -> list[CommentSummary]:
    return [comment for comment in comments if not is_larkguard_comment(comment.body)]


def should_post_github_comment(response: VerifyResponse) -> bool:
    if not ENABLE_GITHUB_COMMENTS:
        return False
    if COMMENT_ONLY_ON_COMPLETED and response.status != RunStatus.COMPLETED:
        return False
    return response.verification_result is not None


def render_verification_comment(response: VerifyResponse) -> str:
    """Build a concise markdown comment for a completed verification run."""
    result = response.verification_result
    plan = response.verification_plan
    brief = response.verification_brief
    if result is None:
        return f"<!-- larkguard-run:{response.run_id} -->\n\n## LarkGuard Verification Result\n\nRun incomplete."

    status_label = _status_label(result.status)
    workflow = plan.workflow_name if plan else "unknown"
    lines: list[str] = [
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
            f"**Status:** {status_label}",
            f"**Workflow:** `{workflow}`",
            f"**Run ID:** `{response.run_id}`",
        ]
    )
    if response.primary_adapter_requested:
        lines.append(f"**Primary adapter requested:** `{response.primary_adapter_requested}`")
    if response.adapter_used:
        lines.append(f"**Adapter used:** `{response.adapter_used}`")
    if response.fallback_triggered:
        lines.append("**Fallback triggered:** yes")

    lines.extend(["", "### Summary", "", result.outcome_summary, "", "### Evidence", ""])
    if result.evidence:
        for artifact in result.evidence[:8]:
            lines.append(f"- **{artifact.label}** ({artifact.kind.value}): {artifact.content}")
    else:
        lines.append("- _(none)_")

    if brief and brief.missing_information:
        lines.extend(["", "### Missing Information", ""])
        lines.extend(f"- {item}" for item in brief.missing_information)

    lines.extend(["", "### Execution Notes", ""])
    for note in result.execution_notes[:10]:
        lines.append(f"- {note}")

    lines.extend(["", "### Resilience", ""])
    for note in result.resilience_notes[:10]:
        lines.append(f"- {note}")

    lines.extend(["", "### Next Action", "", _next_action(result.status), ""])
    lines.append("_Posted by LarkGuard — evidence-first bug verification._")
    return "\n".join(lines)


def _demo_banner(response: VerifyResponse) -> str:
    fault = fault_injection_mode()
    if response.fallback_triggered and fault == "force_adapter_failure":
        return (
            "> **Degraded run:** Primary adapter failure was simulated; "
            "verification completed via **fake** fallback."
        )
    if fault == "force_fallback_note":
        return "> **Demo note:** Resilience degradation was simulated for demonstration purposes."
    if response.fallback_triggered:
        return "> **Degraded run:** Fell back to the fake adapter to complete verification safely."
    return ""


def _status_label(status: ResultStatus) -> str:
    labels = {
        ResultStatus.BLOCKED: "Blocked",
        ResultStatus.SIMULATED: "Simulated",
        ResultStatus.REPRODUCED: "Reproduced",
        ResultStatus.NOT_REPRODUCED: "Not reproduced",
    }
    return labels.get(status, status.value.replace("_", " ").title())


def _next_action(status: ResultStatus) -> str:
    if status == ResultStatus.BLOCKED:
        return (
            "Ask the reporter for clearer reproduction steps, expected vs actual behavior, "
            "and environment details before retrying automated verification."
        )
    if status in (ResultStatus.SIMULATED, ResultStatus.REPRODUCED):
        return "Route to engineering for confirmation; connect live getlark workflow execution when ready."
    if status == ResultStatus.NOT_REPRODUCED:
        return "Request clarification from the reporter or retry verification manually."
    return "Review the run artifacts and decide next steps."


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

    async def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str,
    ) -> str:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    url,
                    headers=self._headers,
                    json={"body": body},
                )
            except httpx.RequestError as exc:
                raise GitHubCommentPosterError(
                    f"Failed to post GitHub comment: {exc}",
                    context={"url": url},
                ) from exc

        if response.status_code == 401:
            raise GitHubCommentPosterError(
                "GitHub rejected comment post (authentication failed)",
                status_code=401,
                context={"url": url},
            )
        if response.status_code == 403:
            raise GitHubCommentPosterError(
                "GitHub rejected comment post (insufficient permissions or rate limit)",
                status_code=403,
                context={"url": url, "body": response.text[:300]},
            )
        if response.status_code == 404:
            raise GitHubCommentPosterError(
                "GitHub issue not found for comment post",
                status_code=404,
                context={"url": url},
            )
        if response.status_code >= 400:
            raise GitHubCommentPosterError(
                f"GitHub comment post failed ({response.status_code})",
                status_code=response.status_code,
                context={"url": url, "body": response.text[:300]},
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubCommentPosterError(
                "GitHub returned malformed JSON for comment post",
                status_code=response.status_code,
            ) from exc

        html_url = data.get("html_url") if isinstance(data, dict) else None
        if not html_url:
            raise GitHubCommentPosterError(
                "GitHub comment post succeeded but response had no html_url",
                status_code=response.status_code,
                context={"url": url},
            )
        return str(html_url)
