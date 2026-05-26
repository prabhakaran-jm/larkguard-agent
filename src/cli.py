from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import NoReturn

import httpx
from rich.console import Console
from rich.table import Table

from src.models import ReplayRequest, VerifyRequest
from src.service import ServiceError, VerificationService

console = Console()
err_console = Console(stderr=True)
DEFAULT_API_BASE = "http://127.0.0.1:8000"


def main() -> None:
    parser = argparse.ArgumentParser(description="LarkGuard CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="Verify a GitHub issue")
    verify_parser.add_argument("--issue-number", type=int, required=True)
    verify_parser.add_argument("--owner", type=str, default=None)
    verify_parser.add_argument("--repo", type=str, default=None)
    verify_parser.add_argument(
        "--local",
        action="store_true",
        help="Run verification locally without calling the API",
    )
    verify_parser.add_argument(
        "--api-base",
        type=str,
        default=DEFAULT_API_BASE,
        help="API base URL when not using --local",
    )

    replay_parser = subparsers.add_parser("replay", help="Replay a stored run")
    replay_parser.add_argument("--run-id", type=str, required=True)
    replay_parser.add_argument("--local", action="store_true")
    replay_parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE)

    runs_parser = subparsers.add_parser("runs", help="List recent runs")
    runs_parser.add_argument("--local", action="store_true")
    runs_parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE)
    runs_parser.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    try:
        if args.command == "verify":
            _run_verify(args)
        elif args.command == "replay":
            _run_replay(args)
        elif args.command == "runs":
            _run_list_runs(args)
    except ServiceError as exc:
        _fail(_format_service_error(exc))
    except httpx.HTTPError as exc:
        _fail(f"API request failed: {exc}")
    except KeyboardInterrupt:
        _fail("Interrupted", code=130)


def _run_verify(args: argparse.Namespace) -> None:
    request = VerifyRequest(
        owner=args.owner,
        repo=args.repo,
        issue_number=args.issue_number,
    )
    if args.local:
        result = asyncio.run(VerificationService().verify(request))
    else:
        response = httpx.post(
            f"{args.api_base.rstrip('/')}/verify",
            json=request.model_dump(),
            timeout=60.0,
        )
        _raise_for_api_error(response)
        from src.models import VerifyResponse

        result = VerifyResponse.model_validate(response.json())

    console.print(f"[green]Verification complete[/green] — run_id={result.run_id}")
    console.print_json(data=result.model_dump(mode="json"))


def _run_replay(args: argparse.Namespace) -> None:
    if args.local:
        result = asyncio.run(VerificationService().replay(args.run_id))
    else:
        response = httpx.post(
            f"{args.api_base.rstrip('/')}/replay",
            json=ReplayRequest(run_id=args.run_id).model_dump(),
            timeout=60.0,
        )
        _raise_for_api_error(response)
        from src.models import VerifyResponse

        result = VerifyResponse.model_validate(response.json())

    console.print(f"[green]Replay complete[/green] — run_id={result.run_id}")
    console.print_json(data=result.model_dump(mode="json"))


def _run_list_runs(args: argparse.Namespace) -> None:
    if args.local:
        runs = VerificationService().list_runs(limit=args.limit)
        payload = [run.model_dump(mode="json") for run in runs]
    else:
        response = httpx.get(
            f"{args.api_base.rstrip('/')}/runs",
            params={"limit": args.limit},
            timeout=30.0,
        )
        _raise_for_api_error(response)
        payload = response.json()

    if not payload:
        console.print("[yellow]No runs found.[/yellow]")
        return

    table = Table(title="Recent LarkGuard Runs")
    table.add_column("Run ID")
    table.add_column("Created")
    table.add_column("Repo")
    table.add_column("Issue")
    table.add_column("Status")
    table.add_column("Stage")

    for run in payload:
        table.add_row(
            run["run_id"],
            str(run["created_at"]),
            f"{run['owner']}/{run['repo']}",
            str(run["issue_number"]),
            run["status"],
            run["stage"],
        )
    console.print(table)


def _raise_for_api_error(response: httpx.Response) -> None:
    if response.is_success:
        return

    detail = response.text
    error_type = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail", detail)
            if isinstance(detail, dict):
                error_type = detail.get("error_type")
                detail = detail.get("detail", json.dumps(detail))
            else:
                error_type = body.get("error_type")
    except json.JSONDecodeError:
        pass

    raise ServiceError(
        f"API error ({response.status_code}): {detail}",
        error_type=error_type,
        status_code=response.status_code,
    )


def _format_service_error(exc: ServiceError) -> str:
    message = str(exc)
    if exc.error_type == "not_found":
        ctx = exc.context
        url = ctx.get("url") if ctx else None
        hint = (
            "Issue not found. Check issue_number and that GITHUB_OWNER/GITHUB_REPO "
            "point at a repo with that issue (or pass --owner/--repo)."
        )
        if url:
            return f"{message}\n  URL: {url}\n  Hint: {hint}"
        return f"{message}\n  Hint: {hint}"
    return message


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]Error:[/red] {message}")
    sys.exit(code)


if __name__ == "__main__":
    main()
