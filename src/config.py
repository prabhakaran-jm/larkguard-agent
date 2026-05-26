from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RUNS_DIR = Path(os.getenv("LARKGUARD_RUNS_DIR", ".larkguard_runs"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip() or None
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip() or None


def require_github_token() -> str:
    if not GITHUB_TOKEN:
        raise ValueError(
            "GITHUB_TOKEN is not set. Copy .env.example to .env and add your token."
        )
    return GITHUB_TOKEN
