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
GETLARK_API_KEY = os.getenv("GETLARK_API_KEY", "").strip()
GETLARK_API_URL = os.getenv("GETLARK_API_URL", "https://api.getlark.ai").strip().rstrip("/")

_VALID_LARK_MODES = frozenset({"fake", "getlark_mcp", "getlark_cli"})
# Backward-compatible alias from early Step 4 (Lark Suite scaffold)
_MODE_ALIASES = {"openapi_mcp": "getlark_mcp"}


def require_github_token() -> str:
    if not GITHUB_TOKEN:
        raise ValueError(
            "GITHUB_TOKEN is not set. Copy .env.example to .env and add your token."
        )
    return GITHUB_TOKEN


def requested_lark_mode() -> str:
    mode = _MODE_ALIASES.get(LARK_MODE, LARK_MODE)
    if mode in _VALID_LARK_MODES:
        return mode
    return "fake"


def getlark_credentials_complete() -> bool:
    return bool(GETLARK_API_KEY)
