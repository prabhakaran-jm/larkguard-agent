from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from src.config import (
    TRUEFOUNDRY_API_KEY,
    TRUEFOUNDRY_GATEWAY_BASE_URL,
    TRUEFOUNDRY_MODEL,
    TRUEFOUNDRY_TIMEOUT_SECONDS,
    parser_mode,
    truefoundry_credentials_complete,
    truefoundry_strict_mode,
)
from src.models import (
    BriefClassification,
    BriefConfidence,
    BriefSignals,
    CommentSummary,
    ConfidenceLevel,
    EvidencePacket,
    IssueSummary,
    VerificationBrief,
    VerificationMode,
)

# TrueFoundryGatewayParser — optional OpenAI-compatible chat completions (parser layer only)
# TODO: fault_injection — simulate parser/gateway failures for resilience demos

SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "repro": re.compile(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:steps?\s+to\s+reproduce|reproduction\s+steps?|repro(?:duce)?)\s*:?\s*\n",
        re.IGNORECASE,
    ),
    "expected": re.compile(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?expected(?:\s+behavior)?\s*:?\s*\n?",
        re.IGNORECASE,
    ),
    "actual": re.compile(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?actual(?:\s+behavior)?\s*:?\s*\n?",
        re.IGNORECASE,
    ),
    "error": re.compile(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:error(?:\s+message)?|stack\s+trace)\s*:?\s*\n?",
        re.IGNORECASE,
    ),
    "environment": re.compile(
        r"(?:^|\n)\s*(?:#{1,3}\s*)?(?:environment|env(?:ironment)?|browser|version|os)\s*:?\s*\n?",
        re.IGNORECASE,
    ),
}

NUMBERED_STEP = re.compile(r"^\s*\d+[\.)]\s+.+", re.MULTILINE)
BULLET_STEP = re.compile(r"^\s*[-*•]\s+.+", re.MULTILINE)
ERROR_LINE = re.compile(
    r"\b(error|exception|traceback|stack trace|failed|failure|panic)\b",
    re.IGNORECASE,
)
ENV_LINE = re.compile(
    r"\b(browser|chrome|firefox|safari|windows|macos|linux|ubuntu|version|os\s*:|python\s+\d)",
    re.IGNORECASE,
)
UI_ACTION = re.compile(
    r"\b(click|open|navigate|select|press|submit|login|sign\s*in|scroll|tap|type|enter|go\s+to)\b",
    re.IGNORECASE,
)
NEXT_SECTION = re.compile(r"(?:^|\n)\s*(?:#{1,3}\s*)?[A-Za-z][\w\s]{0,40}:\s*\n?")


class Parser(ABC):
    @abstractmethod
    def parse(
        self,
        issue: IssueSummary,
        comments: list[CommentSummary],
        evidence: EvidencePacket,
    ) -> VerificationBrief:
        """Produce a structured verification brief from normalized issue evidence."""


class DeterministicParser(Parser):
    def parse(
        self,
        issue: IssueSummary,
        comments: list[CommentSummary],
        evidence: EvidencePacket,
    ) -> VerificationBrief:
        source = evidence.raw_report_text.strip() or issue.body.strip()
        signals = self._detect_signals(source)
        reproduction_steps = self._extract_steps(source)
        expected = self._extract_section(source, "expected")
        actual = self._extract_section(source, "actual") or self._extract_section(source, "error")
        missing = self._missing_information(signals, reproduction_steps, expected, actual, source)
        classification = self._classify(signals, reproduction_steps, expected, actual, source, evidence)
        confidence = self._confidence(signals, reproduction_steps, actual, classification)
        mode = self._verification_mode(reproduction_steps, source, signals)
        summary = self._summary(issue, classification, reproduction_steps)

        return VerificationBrief(
            summary=summary,
            classification=classification,
            reproduction_steps=reproduction_steps,
            expected_behavior=expected,
            actual_behavior=actual,
            missing_information=missing,
            signals=signals,
            confidence=confidence,
            recommended_verification_mode=mode,
            parser_source="deterministic",
        )

    def _detect_signals(self, text: str) -> BriefSignals:
        return BriefSignals(
            has_numbered_steps=bool(NUMBERED_STEP.search(text)),
            has_expected_behavior=bool(SECTION_PATTERNS["expected"].search(text)),
            has_actual_behavior=bool(SECTION_PATTERNS["actual"].search(text)),
            has_error_message=bool(SECTION_PATTERNS["error"].search(text) or ERROR_LINE.search(text)),
            has_environment_details=bool(
                SECTION_PATTERNS["environment"].search(text) or ENV_LINE.search(text)
            ),
        )

    def _extract_steps(self, text: str) -> list[str]:
        block = self._extract_block_after(text, "repro")
        candidates = block or text
        steps: list[str] = []

        for match in NUMBERED_STEP.finditer(candidates):
            step = re.sub(r"^\s*\d+[\.)]\s+", "", match.group(0)).strip()
            if step:
                steps.append(step)

        if not steps:
            for match in BULLET_STEP.finditer(candidates):
                step = re.sub(r"^\s*[-*•]\s+", "", match.group(0)).strip()
                if step and len(step) > 8:
                    steps.append(step)

        if not steps:
            for line in candidates.splitlines():
                cleaned = line.strip()
                if cleaned.lower().startswith(("first,", "then ", "finally ", "next ")):
                    steps.append(cleaned)

        return steps[:12]

    def _extract_section(self, text: str, key: str) -> str:
        block = self._extract_block_after(text, key)
        if not block:
            return ""
        first_line = block.splitlines()[0].strip()
        return first_line[:2000]

    def _extract_block_after(self, text: str, key: str) -> str:
        pattern = SECTION_PATTERNS.get(key)
        if not pattern:
            return ""
        match = pattern.search(text)
        if not match:
            return ""
        remainder = text[match.end() :]
        end = self._find_section_end(remainder)
        return remainder[:end].strip()

    @staticmethod
    def _find_section_end(text: str) -> int:
        match = NEXT_SECTION.search(text)
        if not match or match.start() < 20:
            return len(text)
        return match.start()

    def _missing_information(
        self,
        signals: BriefSignals,
        steps: list[str],
        expected: str,
        actual: str,
        text: str,
    ) -> list[str]:
        missing: list[str] = []
        if not text.strip():
            missing.append("Issue body is empty")
        if not steps:
            missing.append("No clear reproduction steps")
        if not expected and not signals.has_expected_behavior:
            missing.append("Expected behavior not described")
        if not actual and not signals.has_error_message:
            missing.append("Observed failure or actual behavior not described")
        if not signals.has_environment_details:
            missing.append("Environment details (OS, browser, version) not provided")
        return missing

    def _classify(
        self,
        signals: BriefSignals,
        steps: list[str],
        expected: str,
        actual: str,
        text: str,
        evidence: EvidencePacket,
    ) -> BriefClassification:
        hints = evidence.report_quality_hints
        body_len = len(text.strip())
        has_problem = bool(actual.strip()) or signals.has_error_message
        has_repro_clues = bool(steps) or signals.has_numbered_steps or hints.has_repro_signals

        if body_len < 40 and not has_repro_clues and not has_problem:
            return BriefClassification.BLOCKED_MISSING_INFO

        if has_repro_clues and has_problem and (len(steps) >= 1 or signals.has_numbered_steps):
            return BriefClassification.REPRODUCIBLE_CANDIDATE

        if body_len < 80 and not has_repro_clues and not has_problem:
            return BriefClassification.BLOCKED_MISSING_INFO

        if has_repro_clues or has_problem or expected:
            return BriefClassification.UNCLEAR

        return BriefClassification.BLOCKED_MISSING_INFO

    def _confidence(
        self,
        signals: BriefSignals,
        steps: list[str],
        actual: str,
        classification: BriefClassification,
    ) -> BriefConfidence:
        has_problem = bool(actual.strip()) or signals.has_error_message
        structured = signals.has_numbered_steps and len(steps) >= 2

        if structured and has_problem:
            return BriefConfidence(
                level=ConfidenceLevel.HIGH,
                reason="Structured reproduction steps with a clear observed failure.",
            )
        if (steps or signals.has_numbered_steps) and has_problem:
            return BriefConfidence(
                level=ConfidenceLevel.MEDIUM,
                reason="Some reproduction clues and failure signals, but structure is incomplete.",
            )
        if classification == BriefClassification.BLOCKED_MISSING_INFO:
            return BriefConfidence(
                level=ConfidenceLevel.LOW,
                reason="Report is too sparse to attempt automated verification.",
            )
        return BriefConfidence(
            level=ConfidenceLevel.LOW,
            reason="Ambiguous report with limited structured repro or failure details.",
        )

    def _verification_mode(
        self,
        steps: list[str],
        text: str,
        signals: BriefSignals,
    ) -> VerificationMode:
        if len(steps) >= 2:
            return VerificationMode.LARK_WORKFLOW_CANDIDATE
        if steps and UI_ACTION.search(text):
            return VerificationMode.LARK_WORKFLOW_CANDIDATE
        if signals.has_numbered_steps and UI_ACTION.search(text):
            return VerificationMode.LARK_WORKFLOW_CANDIDATE
        return VerificationMode.MANUAL_REVIEW

    @staticmethod
    def _summary(
        issue: IssueSummary,
        classification: BriefClassification,
        steps: list[str],
    ) -> str:
        title = issue.title.strip() or f"Issue #{issue.number}"
        step_note = f"{len(steps)} reproduction step(s) detected." if steps else "No structured steps detected."
        return f"{title} — classified as {classification.value}; {step_note}"


class GatewayParseFailedError(Exception):
    """Raised when TrueFoundry gateway parsing cannot complete (strict mode)."""


class _GatewayBriefPayload(BaseModel):
    summary: str = ""
    classification: str = ""
    reproduction_steps: list[str] = Field(default_factory=list)
    expected_behavior: str = ""
    actual_behavior: str = ""
    missing_information: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    confidence: dict[str, Any] = Field(default_factory=dict)
    recommended_verification_mode: str = ""


_SYSTEM_PROMPT = """You extract bug-report structure for automated verification.
Respond with ONLY a single JSON object (no markdown, no code fences) matching this schema:
{
  "summary": "string",
  "classification": "reproducible_candidate|blocked_missing_info|unclear",
  "reproduction_steps": ["string"],
  "expected_behavior": "string",
  "actual_behavior": "string",
  "missing_information": ["string"],
  "signals": {
    "has_numbered_steps": true,
    "has_expected_behavior": true,
    "has_actual_behavior": true,
    "has_error_message": true,
    "has_environment_details": true
  },
  "confidence": {"level": "low|medium|high", "reason": "string"},
  "recommended_verification_mode": "manual_review|lark_workflow_candidate"
}
Be concise. Use empty strings/lists when unknown. Do not invent reproduction steps not supported by the report."""


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GatewayParseFailedError("Gateway response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise GatewayParseFailedError("Gateway response missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise GatewayParseFailedError("Gateway response missing message content")
    return content.strip()


def _parse_json_from_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayParseFailedError(f"Gateway returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GatewayParseFailedError("Gateway JSON must be an object")
    return parsed


def _coerce_classification(value: str) -> BriefClassification:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    for candidate in BriefClassification:
        if normalized == candidate.value:
            return candidate
    aliases = {
        "reproducible": BriefClassification.REPRODUCIBLE_CANDIDATE,
        "blocked": BriefClassification.BLOCKED_MISSING_INFO,
        "missing_info": BriefClassification.BLOCKED_MISSING_INFO,
    }
    return aliases.get(normalized, BriefClassification.UNCLEAR)


def _coerce_verification_mode(value: str) -> VerificationMode:
    normalized = value.strip().lower().replace("-", "_")
    for candidate in VerificationMode:
        if normalized == candidate.value:
            return candidate
    if "lark" in normalized or "workflow" in normalized:
        return VerificationMode.LARK_WORKFLOW_CANDIDATE
    return VerificationMode.MANUAL_REVIEW


def _coerce_confidence_level(value: str) -> ConfidenceLevel:
    normalized = value.strip().lower()
    for candidate in ConfidenceLevel:
        if normalized == candidate.value:
            return candidate
    return ConfidenceLevel.MEDIUM


def _coerce_signals(raw: dict[str, Any]) -> BriefSignals:
    def as_bool(key: str) -> bool:
        value = raw.get(key, False)
        return bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")

    return BriefSignals(
        has_numbered_steps=as_bool("has_numbered_steps"),
        has_expected_behavior=as_bool("has_expected_behavior"),
        has_actual_behavior=as_bool("has_actual_behavior"),
        has_error_message=as_bool("has_error_message"),
        has_environment_details=as_bool("has_environment_details"),
    )


def _gateway_payload_to_brief(data: dict[str, Any]) -> VerificationBrief:
    try:
        payload = _GatewayBriefPayload.model_validate(data)
    except ValidationError as exc:
        raise GatewayParseFailedError(f"Gateway JSON schema invalid: {exc}") from exc

    confidence_raw = payload.confidence if isinstance(payload.confidence, dict) else {}
    level = _coerce_confidence_level(str(confidence_raw.get("level", "medium")))
    reason = str(confidence_raw.get("reason", "")).strip() or (
        "Structured by TrueFoundry AI Gateway parser."
    )

    summary = payload.summary.strip()
    if not summary:
        raise GatewayParseFailedError("Gateway JSON missing summary")

    classification = _coerce_classification(payload.classification)
    reproduction_steps = [s.strip() for s in payload.reproduction_steps if s.strip()][:12]
    recommended_mode = _coerce_verification_mode(payload.recommended_verification_mode)
    parser_notes = ["Brief produced by TrueFoundry AI Gateway chat completions."]
    if (
        classification == BriefClassification.REPRODUCIBLE_CANDIDATE
        and reproduction_steps
        and recommended_mode == VerificationMode.MANUAL_REVIEW
    ):
        # Tiny deterministic guardrail for demo stability: reproducible + steps should prefer workflow candidate.
        recommended_mode = VerificationMode.LARK_WORKFLOW_CANDIDATE
        parser_notes.append(
            "Guardrail applied: coerced recommended_verification_mode to "
            "lark_workflow_candidate for reproducible candidate with steps."
        )

    return VerificationBrief(
        summary=summary,
        classification=classification,
        reproduction_steps=reproduction_steps,
        expected_behavior=payload.expected_behavior.strip()[:2000],
        actual_behavior=payload.actual_behavior.strip()[:2000],
        missing_information=[m.strip() for m in payload.missing_information if m.strip()][:12],
        signals=_coerce_signals(payload.signals),
        confidence=BriefConfidence(level=level, reason=reason),
        recommended_verification_mode=recommended_mode,
        parser_source="truefoundry_gateway",
        parser_notes=parser_notes,
    )


class TrueFoundryGatewayClient:
    """Minimal OpenAI-compatible client for TrueFoundry AI Gateway."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = TRUEFOUNDRY_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._endpoint = _chat_completions_url(base_url)
        self._model = model
        self._timeout = timeout_seconds

    def parse_report(self, combined_text: str) -> VerificationBrief:
        user_content = combined_text.strip()
        if len(user_content) > 12000:
            user_content = user_content[:12000] + "\n\n[truncated for gateway parser]"

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Parse this GitHub bug report evidence into the JSON schema:\n\n"
                        f"{user_content}"
                    ),
                },
            ],
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(self._endpoint, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise GatewayParseFailedError(
                f"Network error calling TrueFoundry gateway: {exc}"
            ) from exc

        if response.status_code in {401, 403}:
            raise GatewayParseFailedError(
                f"TrueFoundry gateway rejected credentials (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise GatewayParseFailedError(
                f"TrueFoundry gateway endpoint not found (HTTP 404) at {self._endpoint}"
            )
        if response.status_code >= 500:
            raise GatewayParseFailedError(
                f"TrueFoundry gateway server error (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise GatewayParseFailedError(
                f"TrueFoundry gateway error (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )

        try:
            response_payload: Any = response.json()
        except ValueError as exc:
            raise GatewayParseFailedError("TrueFoundry gateway returned non-JSON") from exc

        content = _extract_message_content(response_payload)
        data = _parse_json_from_content(content)
        return _gateway_payload_to_brief(data)


class TrueFoundryGatewayParser(Parser):
    """
    Optional parser: one chat-completions call via TrueFoundry AI Gateway.
    Falls back to DeterministicParser unless TRUEFOUNDRY_STRICT_MODE=true.
    """

    def __init__(
        self,
        fallback: DeterministicParser | None = None,
        client: TrueFoundryGatewayClient | None = None,
    ) -> None:
        self._fallback = fallback or DeterministicParser()
        self._client = client

    def parse(
        self,
        issue: IssueSummary,
        comments: list[CommentSummary],
        evidence: EvidencePacket,
    ) -> VerificationBrief:
        if not truefoundry_credentials_complete():
            message = (
                "TrueFoundry gateway parser requested but "
                "TRUEFOUNDRY_API_KEY, TRUEFOUNDRY_GATEWAY_BASE_URL, or TRUEFOUNDRY_MODEL is missing"
            )
            if truefoundry_strict_mode():
                raise GatewayParseFailedError(message)
            return self._fallback_with_note(
                issue, comments, evidence, message
            )

        client = self._client or TrueFoundryGatewayClient(
            api_key=TRUEFOUNDRY_API_KEY,
            base_url=TRUEFOUNDRY_GATEWAY_BASE_URL,
            model=TRUEFOUNDRY_MODEL,
        )
        try:
            return client.parse_report(evidence.combined_text)
        except GatewayParseFailedError as exc:
            if truefoundry_strict_mode():
                raise
            return self._fallback_with_note(issue, comments, evidence, str(exc))

    def _fallback_with_note(
        self,
        issue: IssueSummary,
        comments: list[CommentSummary],
        evidence: EvidencePacket,
        error: str,
    ) -> VerificationBrief:
        brief = self._fallback.parse(issue, comments, evidence)
        notes = list(brief.parser_notes)
        notes.insert(
            0,
            f"TrueFoundry gateway parser failed ({error}); used DeterministicParser.",
        )
        confidence = brief.confidence.model_copy(
            update={
                "reason": (
                    f"{brief.confidence.reason} "
                    f"(Parser fallback: TrueFoundry gateway unavailable — {error[:120]})"
                ).strip()
            }
        )
        return brief.model_copy(
            update={
                "parser_source": "deterministic",
                "parser_notes": notes,
                "confidence": confidence,
            }
        )


def resolve_parser() -> Parser:
    if parser_mode() == "truefoundry_gateway":
        return TrueFoundryGatewayParser()
    return DeterministicParser()


def default_parser() -> Parser:
    return resolve_parser()
