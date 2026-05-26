from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import NoReturn

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.evidence_utils import evidence_content
from src.models import ReplayRequest, VerifyRequest, VerifyResponse
from src.run_health import compute_run_health, format_health_summary_line
from src.service import ServiceError, VerificationService

console = Console()
err_console = Console(stderr=True)
DEFAULT_API_BASE = "http://127.0.0.1:8000"

DEMO_ISSUE_EXAMPLES: dict[str, dict[str, str]] = {
    "vague": {
        "title": "Checkout sometimes fails",
        "body": (
            "The checkout page feels broken after the last deploy. "
            "Sometimes it works, sometimes it doesn't. Please fix ASAP."
        ),
        "note": "Expect: blocked_missing_info, manual_review, status Blocked.",
    },
    "structured": {
        "title": "Save profile returns 500 on settings page",
        "body": """## Steps to reproduce
1. Log in as a test user
2. Open Settings → Profile
3. Change display name and click **Save**

## Expected
Profile saves and a success toast appears.

## Actual
HTTP 500 with response `Internal Server Error`.

## Environment
- Chrome 124 on Fedora 44
- App version 2.3.1 (staging)
""",
        "note": "Expect: reproducible_candidate, lark_workflow_candidate, status Simulated (scaffold).",
    },
    "degraded": {
        "title": "Login button unresponsive on mobile viewport",
        "body": """## Steps to reproduce
1. Open the app at 375px width (mobile)
2. Go to `/login`
3. Tap **Sign in**

## Expected
Login form submits and navigates to dashboard.

## Actual
Button highlight appears but nothing happens (no network request).

## Environment
- Safari iOS 17, staging build 2.3.1
""",
        "note": (
            "Use with fault injection for degraded demo:\n"
            "  PRIMARY_ADAPTER_MODE=getlark_mcp FAULT_INJECTION_MODE=force_adapter_failure"
        ),
    },
}


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

    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Print full VerifyResponse JSON after the summary",
    )

    replay_parser = subparsers.add_parser("replay", help="Replay a stored run")
    replay_parser.add_argument("--run-id", type=str, required=True)
    replay_parser.add_argument("--local", action="store_true")
    replay_parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE)
    replay_parser.add_argument(
        "--json",
        action="store_true",
        help="Print full VerifyResponse JSON after the summary",
    )

    runs_parser = subparsers.add_parser("runs", help="List recent runs")
    runs_parser.add_argument("--local", action="store_true")
    runs_parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE)
    runs_parser.add_argument("--limit", type=int, default=20)

    summary_parser = subparsers.add_parser(
        "demo-summary",
        help="Print judge-friendly summary for a run",
    )
    summary_parser.add_argument("--run-id", type=str, required=True)
    summary_parser.add_argument("--local", action="store_true")
    summary_parser.add_argument("--api-base", type=str, default=DEFAULT_API_BASE)

    demo_parser = subparsers.add_parser(
        "demo-issue-text",
        help="Print ready-to-paste GitHub issue title/body for demos",
    )
    demo_parser.add_argument(
        "--type",
        choices=["vague", "structured", "degraded"],
        required=True,
    )

    args = parser.parse_args()

    try:
        if args.command == "verify":
            _run_verify(args)
        elif args.command == "replay":
            _run_replay(args)
        elif args.command == "runs":
            _run_list_runs(args)
        elif args.command == "demo-summary":
            _run_demo_summary(args)
        elif args.command == "demo-issue-text":
            _run_demo_issue_text(args)
    except ServiceError as exc:
        _fail(_format_service_error(exc))
    except httpx.HTTPError as exc:
        _fail(f"API request failed: {exc}")
    except KeyboardInterrupt:
        _fail("Interrupted", code=130)


def _run_demo_issue_text(args: argparse.Namespace) -> None:
    example = DEMO_ISSUE_EXAMPLES[args.type]
    console.print(
        Panel(
            f"[bold]Title[/bold]\n{example['title']}\n\n"
            f"[bold]Body[/bold]\n{example['body']}\n\n"
            f"[dim]{example['note']}[/dim]",
            title=f"Demo issue — {args.type}",
            border_style="cyan",
        )
    )


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
        result = VerifyResponse.model_validate(response.json())

    _print_verify_summary(result)
    if args.json:
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
        result = VerifyResponse.model_validate(response.json())

    console.print(f"[green]Replay complete[/green] — run_id={result.run_id}")
    _print_verify_summary(result)
    if args.json:
        console.print_json(data=result.model_dump(mode="json"))


def _print_verify_summary(result: VerifyResponse) -> None:
    console.print(f"[green]Verification complete[/green] — run_id={result.run_id}")
    console.print(f"[bold cyan]{format_health_summary_line(result)}[/bold cyan]")
    if result.verification_result:
        status = result.verification_result.status.value
        workflow = (
            result.verification_plan.workflow_name
            if result.verification_plan
            else "unknown"
        )
        console.print(
            f"[bold]Result:[/bold] {status} · workflow `{workflow}` · "
            f"adapter `{result.adapter_used or 'unknown'}`"
        )
        execution_id = _execution_id_from_result(result)
        if execution_id:
            console.print(f"[bold green]Execution proof:[/bold green] `{execution_id}`")
        workflow_selected = evidence_content(result.verification_result, "workflow_selected")
        workflow_source = evidence_content(result.verification_result, "workflow_selection_source")
        invoke_status = evidence_content(result.verification_result, "invoke_status")
        if workflow_selected:
            console.print(f"[bold]Workflow selected:[/bold] `{workflow_selected}`")
        if workflow_source:
            console.print(f"[dim]Selection source:[/dim] {workflow_source}")
        if invoke_status:
            console.print(f"[dim]Invoke status:[/dim] {invoke_status}")
        if result.fallback_triggered:
            console.print(
                "[yellow]Degraded:[/yellow] fallback from "
                f"`{result.primary_adapter_requested}` → `{result.adapter_used}`"
            )
    if result.parser_used:
        if result.parser_fallback_triggered:
            console.print(
                "[yellow]Parser:[/yellow] fallback "
                f"`{result.parser_requested}` → `{result.parser_used}`"
            )
        elif result.parser_used == "truefoundry_gateway":
            console.print(
                f"[green]Parser:[/green] `{result.parser_used}` (TrueFoundry gateway)"
            )
    if result.comment_action:
        label = "cyan" if result.comment_action == "updated" else "green"
        console.print(
            f"[{label}]GitHub comment:[/{label}] {result.comment_action}",
            end="",
        )
        if result.github_comment_url:
            console.print(f" — {result.github_comment_url}")
        else:
            console.print()


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


def _run_demo_summary(args: argparse.Namespace) -> None:
    if args.local:
        result = asyncio.run(VerificationService().replay(args.run_id))
    else:
        response = httpx.post(
            f"{args.api_base.rstrip('/')}/replay",
            json=ReplayRequest(run_id=args.run_id).model_dump(),
            timeout=60.0,
        )
        _raise_for_api_error(response)
        result = VerifyResponse.model_validate(response.json())

    console.print(_render_demo_summary(result))


def _execution_id_from_result(result: VerifyResponse) -> str | None:
    if result.verification_result is None or not result.verification_result.evidence:
        return None
    for artifact in result.verification_result.evidence:
        if artifact.label == "execution_id":
            return artifact.content.split()[0]
    return None


def _render_demo_summary(result: VerifyResponse) -> str:
    status = result.verification_result.status.value if result.verification_result else result.status.value
    workflow = result.verification_plan.workflow_name if result.verification_plan else "unknown"
    adapter = result.adapter_used or "unknown"
    parser = result.parser_used or "deterministic"
    fallback = "yes" if result.fallback_triggered else "no"
    parser_fallback = "yes" if result.parser_fallback_triggered else "no"
    timing = []
    if result.parser_duration_ms is not None:
        timing.append(f"parser={result.parser_duration_ms}ms")
    if result.adapter_duration_ms is not None:
        timing.append(f"adapter={result.adapter_duration_ms}ms")
    timing_text = ", ".join(timing) if timing else "n/a"
    execution_id = _execution_id_from_result(result)
    workflow_selected = evidence_content(result.verification_result, "workflow_selected")
    workflow_source = evidence_content(result.verification_result, "workflow_selection_source")
    invoke_status = evidence_content(result.verification_result, "invoke_status")

    lines = [
        f"# LarkGuard Demo Summary ({result.run_id})",
        "",
        f"- {format_health_summary_line(result)}",
        f"- Status: **{status}**",
    ]
    if execution_id:
        lines.append(f"- Execution proof: `{execution_id}`")
    if workflow_selected:
        lines.append(f"- Workflow selected: `{workflow_selected}`")
    if workflow_source:
        lines.append(f"- Selection source: `{workflow_source}`")
    if invoke_status:
        lines.append(f"- Invoke status: `{invoke_status}`")
    lines.extend(
        [
            f"- Workflow: `{workflow}`",
            f"- Adapter: `{adapter}` (fallback: {fallback})",
            f"- Parser: `{parser}` (fallback: {parser_fallback})",
            f"- Timings: {timing_text}",
        ]
    )
    if result.github_comment_url:
        lines.append(f"- Managed comment: {result.github_comment_url}")
    if result.verification_result and result.verification_result.evidence:
        lines.extend(["", "## Evidence Highlights"])
        for artifact in result.verification_result.evidence[:5]:
            snippet = artifact.content if len(artifact.content) <= 140 else artifact.content[:137] + "..."
            lines.append(f"- `{artifact.label}`: {snippet}")
    return "\n".join(lines)


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
