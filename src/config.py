from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RUNS_DIR = Path(os.getenv("LARKGUARD_RUNS_DIR", ".larkguard_runs"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip() or None
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip() or None

# getlark.ai (hackathon sponsor) — https://docs.getlark.ai
LARK_MODE = os.getenv("LARK_MODE", "fake").strip().lower()
PRIMARY_ADAPTER_MODE = os.getenv("PRIMARY_ADAPTER_MODE", "").strip().lower()
GETLARK_API_KEY = os.getenv("GETLARK_API_KEY", "").strip()
GETLARK_API_URL = os.getenv("GETLARK_API_URL", "https://api.getlark.ai").strip().rstrip("/")

# Fault injection (Step 5)
FAULT_INJECTION_MODE = os.getenv("FAULT_INJECTION_MODE", "none").strip().lower()

# GitHub issue comments (Step 5)
ENABLE_GITHUB_COMMENTS = os.getenv("ENABLE_GITHUB_COMMENTS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
COMMENT_ONLY_ON_COMPLETED = os.getenv("COMMENT_ONLY_ON_COMPLETED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

_VALID_LARK_MODES = frozenset({"fake", "getlark_mcp", "getlark_cli"})
_VALID_FAULT_MODES = frozenset({"none", "force_adapter_failure", "force_fallback_note"})
_MODE_ALIASES = {"openapi_mcp": "getlark_mcp"}


def require_github_token() -> str:
    if not GITHUB_TOKEN:
        raise ValueError(
            "GITHUB_TOKEN is not set. Copy .env.example to .env and add your token."
        )
    return GITHUB_TOKEN


def _normalize_adapter_mode(mode: str) -> str:
    normalized = _MODE_ALIASES.get(mode, mode)
    if normalized in _VALID_LARK_MODES:
        return normalized
    return "fake"


def requested_lark_mode() -> str:
    return _normalize_adapter_mode(LARK_MODE)


def effective_primary_adapter_mode() -> str:
    if PRIMARY_ADAPTER_MODE:
        return _normalize_adapter_mode(PRIMARY_ADAPTER_MODE)
    return requested_lark_mode()


def getlark_credentials_complete() -> bool:
    return bool(GETLARK_API_KEY)


def fault_injection_mode() -> str:
    if FAULT_INJECTION_MODE in _VALID_FAULT_MODES:
        return FAULT_INJECTION_MODE
    return "none"
