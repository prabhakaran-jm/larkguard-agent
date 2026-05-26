from __future__ import annotations

from src.models import VerificationResult


def evidence_content(result: VerificationResult | None, label: str) -> str | None:
    if result is None or not result.evidence:
        return None
    for artifact in result.evidence:
        if artifact.label == label:
            return artifact.content
    return None
