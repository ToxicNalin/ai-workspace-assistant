# AI Workspace Assistant

Multi-tenant AI workspace assistant: FastAPI backend, React frontend, Postgres +
pgvector for retrieval-augmented generation, and a LangGraph agent that must get
human approval before any side-effecting action (sending email, creating an event,
creating tasks).

**Status:** Step 3 of 9 — documents upload to object storage and land as `pending`.
Auth, workspaces, RBAC and the tenant-isolation suite are in place; ingestion, RAG
and the agent are not yet built. See [`docs/SPEC-v2.md`](docs/SPEC-v2.md) for the
architecture and [`docs/BUILD-ORDER.md`](docs/BUILD-ORDER.md) for the build sequence.

The highest-value file in the repo is
[`app/tests/test_tenant_isolation.py`](app/tests/test_tenant_isolation.py): it proves
that every tenant-scoped route returns **404, never 403**, for a resource in another
workspace — a 403 would confirm the resource exists and leak tenant structure.

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
