# AI Workspace Assistant

Multi-tenant AI workspace assistant: FastAPI backend, React frontend, Postgres +
pgvector for retrieval-augmented generation, and a LangGraph agent that must get
human approval before any side-effecting action (sending email, creating an event,
creating tasks).

**Status:** Step 4 of 9 — uploaded documents are chunked, embedded and indexed
on their own. Auth, workspaces, RBAC, the tenant-isolation suite and the ingestion
queue are in place; retrieval, chat and the agent are not yet built. See
[`docs/SPEC-v2.md`](docs/SPEC-v2.md) for the architecture and
[`docs/BUILD-ORDER.md`](docs/BUILD-ORDER.md) for the build sequence.

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

The suite runs against real Postgres, not SQLite — pgvector and `SKIP LOCKED`
do not exist there. Embeddings default to a deterministic offline implementation
(`EMBEDDING_PROVIDER=fake`), so no API key is needed to run the tests or ingest a
document locally.

## Background processing

There is no Celery. Ingestion runs through the `ingestion_jobs` table, claimed with
`FOR UPDATE SKIP LOCKED` under a `lease_until` heartbeat and drained by an asyncio
task inside the API process — because no free tier offers a persistent worker.

That process can be killed mid-job at any time, which is handled rather than
ignored: a job whose lease expires is reclaimed by the next process to boot, its
attempt count is incremented, and after three attempts the document is marked
`failed` with an `error_message` the UI can show. Re-running a job is idempotent —
existing chunks are deleted and rewritten in the same transaction that marks the
document `ready`, so a crash anywhere in the middle leaves the document exactly as
it was.

If throughput ever demanded it, the same table is drained by a separate worker
process with no code change.

More to come as each build step lands — see `docs/BUILD-ORDER.md`.
