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

GETLARK_STRICT_MODE = os.getenv("GETLARK_STRICT_MODE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
try:
    GETLARK_TIMEOUT_SECONDS = float(os.getenv("GETLARK_TIMEOUT_SECONDS", "15"))
except ValueError:
    GETLARK_TIMEOUT_SECONDS = 15.0
GETLARK_ENABLE_WORKFLOW_INVOKE = os.getenv("GETLARK_ENABLE_WORKFLOW_INVOKE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

_VALID_LARK_MODES = frozenset({"fake", "getlark_mcp", "getlark_cli", "getlark_live_check"})
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


def getlark_strict_mode() -> bool:
    return GETLARK_STRICT_MODE


# Parser (optional TrueFoundry AI Gateway)
PARSER_MODE = os.getenv("PARSER_MODE", "deterministic").strip().lower()
TRUEFOUNDRY_API_KEY = os.getenv("TRUEFOUNDRY_API_KEY", "").strip()
TRUEFOUNDRY_GATEWAY_BASE_URL = os.getenv("TRUEFOUNDRY_GATEWAY_BASE_URL", "").strip().rstrip("/")
TRUEFOUNDRY_MODEL = os.getenv("TRUEFOUNDRY_MODEL", "").strip()
TRUEFOUNDRY_STRICT_MODE = os.getenv("TRUEFOUNDRY_STRICT_MODE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
try:
    TRUEFOUNDRY_TIMEOUT_SECONDS = float(os.getenv("TRUEFOUNDRY_TIMEOUT_SECONDS", "20"))
except ValueError:
    TRUEFOUNDRY_TIMEOUT_SECONDS = 20.0

_VALID_PARSER_MODES = frozenset({"deterministic", "truefoundry_gateway"})


def parser_mode() -> str:
    if PARSER_MODE in _VALID_PARSER_MODES:
        return PARSER_MODE
    return "deterministic"


def truefoundry_credentials_complete() -> bool:
    return bool(TRUEFOUNDRY_API_KEY and TRUEFOUNDRY_GATEWAY_BASE_URL and TRUEFOUNDRY_MODEL)


def truefoundry_strict_mode() -> bool:
    return TRUEFOUNDRY_STRICT_MODE
