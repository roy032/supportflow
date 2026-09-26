# SupportFlow: development notes

SupportFlow is an AI support-automation pipeline:
Tally form -> signed webhook -> FastAPI -> stored event -> LLM classification (structured JSON) -> Slack.

## Ground rules
- Python 3.11+, virtual environment in `.venv`. Developed on Windows; code must also run on Linux (CI).
- FastAPI + SQLAlchemy 2.0. SQLite locally; keep the code database-agnostic so Postgres is a URL change.
- The LLM sits behind `app/llm/base.py` (`LLMProvider`). Ollama (`qwen2.5:3b`) is the default;
  cloud providers are added as new classes, not by editing the pipeline.
- Secrets live only in `.env` (git-ignored). Every new setting also goes in `.env.example` with a placeholder.
- Tests use pytest. All external calls (Ollama, Slack) are mocked. No test may need network access.
- Every failure is recorded in the `failures` table with its stage; the app never crashes on bad LLM output.
- Work one step at a time, run `pytest` after each step, and commit working steps separately.

## Commands
- Install: `pip install -r requirements-dev.txt`
- Run: `uvicorn app.main:app --reload --port 8000`
- Test: `pytest -q`
- Send a signed test webhook: `python scripts/send_test_webhook.py`
