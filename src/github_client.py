from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.models import CommentSummary, IssueSummary


class GitHubClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.context = context or {}


class GitHubRateLimitError(GitHubClientError):
    pass


class GitHubNotFoundError(GitHubClientError):
    pass


class GitHubAuthError(GitHubClientError):
    pass


class GitHubClient:
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

    @retry(
        retry=retry_if_exception_type(GitHubRateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch_issue(self, owner: str, repo: str, issue_number: int) -> IssueSummary:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}"
        data = await self._get_json(url)
        return self._parse_issue(data)

    @retry(
        retry=retry_if_exception_type(GitHubRateLimitError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[CommentSummary]:
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        data = await self._get_json(url)
        if not isinstance(data, list):
            raise GitHubClientError(
                "Unexpected GitHub comments response shape",
                error_type="malformed_response",
                context={"expected": "list", "got": type(data).__name__},
            )
        return [self._parse_comment(item) for item in data]

    async def _get_json(self, url: str) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.get(url, headers=self._headers)
            except httpx.RequestError as exc:
                raise GitHubClientError(
                    f"GitHub request failed: {exc}",
                    error_type="network_error",
                ) from exc

        if response.status_code == 404:
            raise GitHubNotFoundError(
                "GitHub resource not found (issue may not exist, or owner/repo is wrong)",
                status_code=404,
                error_type="not_found",
                context={"url": url},
            )

        if response.status_code in {401, 403}:
            message = "GitHub authentication failed"
            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset = response.headers.get("X-RateLimit-Reset")
                raise GitHubRateLimitError(
                    "GitHub rate limit exceeded",
                    status_code=403,
                    error_type="rate_limit",
                    context={"reset": reset, "url": url},
                )
            raise GitHubAuthError(
                message,
                status_code=response.status_code,
                error_type="auth_error",
                context={"url": url},
            )

        if response.status_code == 429:
            reset = response.headers.get("X-RateLimit-Reset")
            raise GitHubRateLimitError(
                "GitHub rate limit exceeded",
                status_code=429,
                error_type="rate_limit",
                context={"reset": reset, "url": url},
            )

        if response.status_code >= 400:
            raise GitHubClientError(
                f"GitHub API error ({response.status_code})",
                status_code=response.status_code,
                error_type="github_api_error",
                context={"url": url, "body": response.text[:500]},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubClientError(
                "GitHub returned malformed JSON",
                status_code=response.status_code,
                error_type="malformed_response",
            ) from exc

    def _parse_issue(self, data: Any) -> IssueSummary:
        if not isinstance(data, dict):
            raise GitHubClientError(
                "Unexpected GitHub issue response shape",
                error_type="malformed_response",
                context={"expected": "dict", "got": type(data).__name__},
            )

        try:
            labels = [
                label["name"]
                for label in data.get("labels", [])
                if isinstance(label, dict) and "name" in label
            ]
            user = data.get("user") or {}
            return IssueSummary(
                number=int(data["number"]),
                title=str(data.get("title") or ""),
                body=str(data.get("body") or ""),
                state=str(data.get("state") or "unknown"),
                labels=labels,
                author=str(user.get("login") or "unknown"),
                url=str(data.get("html_url") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubClientError(
                "Failed to parse GitHub issue payload",
                error_type="malformed_response",
            ) from exc

    def _parse_comment(self, data: Any) -> CommentSummary:
        if not isinstance(data, dict):
            raise GitHubClientError(
                "Unexpected GitHub comment response shape",
                error_type="malformed_response",
                context={"expected": "dict", "got": type(data).__name__},
            )

        try:
            user = data.get("user") or {}
            return CommentSummary(
                author=str(user.get("login") or "unknown"),
                body=str(data.get("body") or ""),
                created_at=str(data.get("created_at") or ""),
                url=str(data.get("html_url") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubClientError(
                "Failed to parse GitHub comment payload",
                error_type="malformed_response",
            ) from exc
