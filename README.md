# LarkGuard: Evidence-First Bug Verification

Turn messy GitHub bug reports into proof — reproduced, not reproduced, or blocked — with evidence and graceful fallback when agent infrastructure fails.

**Sponsor integration:** [getlark.ai](https://getlark.ai) (testing workflows via CLI/MCP) — not Lark Suite / `larksuite.com`.

## Why this matters

Bug reports are noisy. Maintainers waste time guessing whether an issue is actionable. LarkGuard starts with evidence: fetch the issue, normalize it into a proof-oriented packet, and leave a replayable trail for verification agents to build on.

## Current MVP scope

**Step 1**
- Manual verification trigger (API + CLI)
- GitHub issue + comments fetch via REST API
- Normalized evidence packet for downstream parsing
- Local JSON run storage for replay/debug

**Step 2**
- Deterministic parser → structured `verification_brief`
- Rule-based classification, confidence, and verification mode
- Parser interface ready for a future LLM-backed implementation

**Step 3**
- Execution planning → `verification_plan` from the brief
- Fake Lark adapter → `verification_result` (plan → execute → result)
- End-to-end verify output: evidence → brief → plan → result

**Step 4**
- Config-driven adapter selection (`fake` vs getlark scaffold modes)
- Graceful fallback to fake when `GETLARK_API_KEY` is missing

**Step 4b (getlark.ai alignment)**
- `GetLarkScaffoldAdapter` for MCP and CLI transports
- Workflow descriptions built from GitHub issue + verification plan
- Points at `api.getlark.ai` (sponsor), not Feishu Open Platform

**Not yet:** Live getlark MCP/CLI calls, LLM parsing, resilience/fault injection, GitHub comment posting, auth, database, or webhooks.

## Parser (Step 2)

After fetching an issue, LarkGuard runs a **deterministic parser** over the evidence packet and returns a `verification_brief` with:

- Summary and classification (`reproducible_candidate`, `blocked_missing_info`, `unclear`)
- Extracted reproduction steps, expected/actual behavior
- Missing-information checklist and signal flags
- Rule-based confidence and recommended mode (`manual_review` vs `lark_workflow_candidate`)

**Why deterministic first?** It is fast, reproducible, and demo-friendly — the same issue always yields the same brief. That makes debugging and judging easier before we add LLM variability.

**Next:** `LLMParser` behind the same interface, then Lark workflow execution using the brief as input.

## Execution planning & fake adapter (Step 3)

After parsing, LarkGuard builds a **verification plan** (workflow name, goal, proposed steps, assumptions, blockers) and runs a **fake Lark adapter** that simulates execution without real MCP calls.

This makes the demo flow visible: **plan → execute → result**, with statuses like `blocked`, `simulated`, or `not_reproduced` and execution artifacts (notes, logs).

**Why fake execution first?** You can demo the full pipeline and stored run JSON before wiring Lark MCP. The `LarkAdapter` interface is ready to swap in a real implementation.

**Next:** Real Lark MCP adapter behind the same interface, wrapped by a resilience gateway.

## getlark.ai adapter modes

| Mode | Env | Behavior |
|------|-----|----------|
| `fake` (default) | `LARK_MODE=fake` or unset | Reliable simulated execution |
| `getlark_mcp` | `LARK_MODE=getlark_mcp` + `GETLARK_API_KEY` | Scaffold describes MCP workflow at `api.getlark.ai/mcp`; **no HTTP calls** |
| `getlark_cli` | `LARK_MODE=getlark_cli` + `GETLARK_API_KEY` | Scaffold shows `getlark workflows create/invoke` commands; **no subprocess** |
| Missing API key | mode set, no `GETLARK_API_KEY` | **Falls back to fake** with a note in `verification_result` |

**`.env` for getlark scaffold:**

```env
GETLARK_API_KEY=your_key_from_dashboard
GETLARK_API_URL=https://api.getlark.ai
LARK_MODE=getlark_mcp
```

Get your API key: [getlark.ai](https://getlark.ai) → Settings → API Keys. Docs: [MCP](https://docs.getlark.ai/mcp-quickstart), [CLI](https://docs.getlark.ai/cli).

**Why fallback is intentional:** Demos should not fail without credentials. `execution_notes` always shows which adapter ran.

**Current limitation:** Scaffolds only — real `api.getlark.ai` MCP/CLI wiring is the next step.

## Quickstart

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and set GITHUB_TOKEN (required) and optional GITHUB_OWNER / GITHUB_REPO

# 4. Start the API
uvicorn src.main:app --reload

# 5. In another terminal, verify an issue (uses env defaults if owner/repo omitted)
python -m src.cli verify --issue-number 1 --local

# Or call the API directly
curl -X POST http://127.0.0.1:8000/verify \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 1, "owner": "your-org", "repo": "your-repo"}'

# 6. Inspect stored runs
python -m src.cli runs --local
ls .larkguard_runs/
```

### CLI commands

```bash
python -m src.cli verify --issue-number 123 --owner org --repo repo --local
python -m src.cli replay --run-id <run_id> --local
python -m src.cli runs --local
```

Omit `--local` to call the running API instead (default `http://127.0.0.1:8000`).

## API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/verify` | Fetch and normalize a GitHub issue |
| POST | `/replay` | Return stored result or re-run |
| GET | `/runs` | List recent run summaries |

## Planned next steps

- **Live getlark MCP/CLI** — HTTP to `api.getlark.ai/mcp` or `@getlark/cli workflows invoke --wait`
- **LLM parser** — swap in behind the same `Parser` interface for ambiguous reports
- **Resilience/fallback layer** — wrap adapter execution with graceful degradation
- **GitHub comment posting** — publish verification evidence back to the issue

## Project layout

```
src/
  main.py           # FastAPI routes
  service.py        # Orchestration
  parser.py         # Deterministic verification brief parser
  lark_adapter.py   # Plan + fake Lark execution adapter
  github_client.py  # GitHub REST client
  run_store.py      # Local JSON persistence
  models.py         # Pydantic schemas
  config.py         # Environment config
  cli.py            # Rich CLI
```

## License

See [LICENSE](LICENSE).
