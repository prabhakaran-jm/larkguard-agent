from __future__ import annotations

from src.models import VerifyResponse


def compute_run_health(response: VerifyResponse) -> str:
    parser_degraded = bool(response.parser_fallback_triggered)
    adapter_degraded = bool(response.fallback_triggered)
    if parser_degraded and adapter_degraded:
        return "degraded-both"
    if parser_degraded:
        return "degraded-parser"
    if adapter_degraded:
        return "degraded-adapter"
    return "healthy"


def format_health_summary_line(response: VerifyResponse) -> str:
    health = compute_run_health(response)
    lark = response.adapter_used or "unknown"
    parser = response.parser_used or "deterministic"
    return f"Health={health} · Lark={lark} · Parser={parser}"


def _has_execution_id(response: VerifyResponse) -> bool:
    result = response.verification_result
    if result is None or not result.evidence:
        return False
    return any(artifact.label == "execution_id" for artifact in result.evidence)


def run_comment_headline(response: VerifyResponse) -> str | None:
    """Top-of-comment banner for judge glanceability."""
    health = compute_run_health(response)
    if health == "degraded-both":
        return (
            "> **Degraded run** — parser and adapter fallbacks executed; "
            "verification still completed."
        )
    if health == "degraded-parser":
        return "> **Degraded run** — TrueFoundry parser fallback; verification completed."
    if health == "degraded-adapter":
        return "> **Degraded run** — fell back to resilient fallback executor."

    if _has_execution_id(response) and (
        response.adapter_used or ""
    ).startswith("getlark"):
        return "> **Live sponsor run** — getlark execution proof captured."
    if response.parser_used == "truefoundry_gateway":
        return "> **Live sponsor run** — TrueFoundry gateway parser active."
    if (response.adapter_used or "").endswith("_live") or response.adapter_used == "getlark_cli_live":
        return "> **Live sponsor run** — real getlark CLI/API path executed."
    return "> **Healthy run** — completed without fallback."
