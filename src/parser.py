from __future__ import annotations

import re
from abc import ABC, abstractmethod

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

# TODO: LLMParser — same interface, backed by an LLM for ambiguous reports
# TODO: fallback parser chain — try LLMParser, fall back to DeterministicParser on outage
# TODO: fault_injection — simulate parser failures for resilience demos

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


def default_parser() -> Parser:
    return DeterministicParser()
