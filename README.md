# AI Workspace Assistant

Multi-tenant AI workspace assistant: FastAPI backend, React frontend, Postgres +
pgvector for retrieval-augmented generation, and a LangGraph agent that must get
human approval before any side-effecting action (sending email, creating an event,
creating tasks).

**Status:** Step 0 — skeleton and deployment. No features yet; this is the
foundation the rest of the project builds on. See [`docs/SPEC-v2.md`](docs/SPEC-v2.md)
for the architecture and [`docs/BUILD-ORDER.md`](docs/BUILD-ORDER.md) for the build
sequence.

## Local development

```bash
cp .env.example .env
python -m venv .venv && .venv/Scripts/activate  # or source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

`GET /health` should return `{"status": "ok"}`.

Or with Docker Compose (also starts a local Postgres + pgvector, unused until
Step 1):

```bash
cp .env.example .env
docker compose up --build
```

## Testing

```bash
ruff check .
mypy app
pytest
```

More to come as each build step lands — see `docs/BUILD-ORDER.md`.
