from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.config import RUNS_DIR
from src.models import RunSummary, StoredRun


class RunStoreError(Exception):
    pass


class RunNotFoundError(RunStoreError):
    pass


class RunStore:
    def __init__(self, runs_dir: Path | None = None) -> None:
        self.runs_dir = runs_dir or RUNS_DIR
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def save(self, run: StoredRun) -> StoredRun:
        path = self._path_for(run.run_id)
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        return run

    def load(self, run_id: str) -> StoredRun:
        path = self._path_for(run_id)
        if not path.exists():
            raise RunNotFoundError(f"Run not found: {run_id}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return StoredRun.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RunStoreError(f"Failed to read run file for {run_id}") from exc

    def list_recent(self, limit: int = 20) -> list[RunSummary]:
        runs: list[RunSummary] = []
        for path in sorted(self.runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                stored = StoredRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValueError):
                continue

            runs.append(
                RunSummary(
                    run_id=stored.run_id,
                    created_at=stored.created_at,
                    stage=stored.stage,
                    status=stored.status,
                    owner=stored.input_params.owner,
                    repo=stored.input_params.repo,
                    issue_number=stored.input_params.issue_number,
                    error=stored.error,
                )
            )
            if len(runs) >= limit:
                break
        return runs

    def _path_for(self, run_id: str) -> Path:
        safe_id = run_id.replace("/", "_").replace("\\", "_")
        return self.runs_dir / f"{safe_id}.json"
