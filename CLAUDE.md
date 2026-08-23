# AI Workspace Assistant

Multi-tenant AI workspace assistant. FastAPI backend, React frontend, Postgres +
pgvector for RAG, LangGraph agent with human-in-the-loop approval on every
side-effecting action.

Read `docs/SPEC-v2.md` before making architectural decisions. It supersedes the
original blueprint and contains a decision log explaining what changed and why.

## Conventions

- **en-GB spelling** in all code, comments, docs and UI copy (`organisation`,
  `authorise`, `behaviour`).
- Python 3.12+. Async everywhere — `async def` endpoints, SQLAlchemy 2.0 async
  session, `httpx.AsyncClient`. Never a blocking call in a request path.
- Type hints on every function. `mypy` and `ruff` must pass before a commit.
- Pydantic v2 schemas for all request and response bodies. No raw dicts crossing
  an API boundary.
- Primary keys are UUIDv7, not integers.
- Alembic for every schema change. Never `Base.metadata.create_all()` outside tests.

## Architecture rules — do not violate these

1. **Every tenant-scoped query filters by `workspace_id`.** No exceptions. Access it
   through the shared scoping dependency rather than writing the filter by hand.
2. **A missing resource in another workspace returns 404, never 403.** A 403 confirms
   the resource exists and leaks tenant structure.
3. **No LLM output ever triggers an outbound API call directly.** Side-effecting tools
   interrupt for human approval via `HumanInTheLoopMiddleware`, and the approval is
   validated against `pending_actions.payload_hash` before execution.
4. **Recipients, assignees and guests are resolved server-side** from
   `workspace_members`. The model may name a person; it may never supply an email
   address. Anything unresolvable is refused.
5. **Retrieved document text is untrusted.** It goes in a user-role message inside
   explicit delimiters, never in the system prompt.
6. Secrets come from environment variables via `app/config.py`. Never hardcoded,
   never logged, never committed.

## Layout

```
app/
  api/          routers — thin, parse and delegate, no business logic
  services/     business logic, the only layer that touches both DB and AI
  database/     SQLAlchemy models, session, migrations
  schemas/      Pydantic request/response models
  auth/         JWT, password hashing, RBAC dependencies, OAuth
  ai/           rag/ agent/ tools/ prompts/ retriever/ embeddings/
  storage/      R2 upload/delete
  workers/      Postgres-backed ingestion queue
  utils/
  tests/
```

Routers never import from `ai/` directly — they go through `services/`.

## Background jobs

There is no Celery. Ingestion runs through the `ingestion_jobs` table, claimed with
`FOR UPDATE SKIP LOCKED` and a `lease_until` heartbeat, drained by an asyncio task in
the FastAPI lifespan. Jobs must be idempotent and safe to retry — the free-tier host
can kill the process mid-job at any time.

## Deployment constraints

Everything runs on free tiers: Render (512 MB, spins down at ~15 min idle), Neon
(0.5 GB, scale-to-zero at 5 min), Cloudflare Pages, Cloudflare R2.

- SQLAlchemy pool stays small: `pool_size=2, max_overflow=3, pool_pre_ping=True`.
- Use Neon's **pooled** connection string.
- `/health` must not touch the database — it is pinged every 10 minutes to keep the
  service warm, and hitting Postgres would burn the compute-hour allowance.
- Storage is capped. 768-dim `halfvec` embeddings, per-workspace document limits.

## Testing

- `pytest` + `pytest-asyncio`, real Postgres in Docker, not SQLite — pgvector and
  `SKIP LOCKED` do not exist in SQLite.
- LLM and email providers are mocked at the interface.
- `tests/test_tenant_isolation.py` is the highest-value file in the repo. Every new
  tenant-scoped endpoint gets a case there proving workspace A cannot reach
  workspace B.
- `tests/test_prompt_injection.py` asserts that a poisoned document cannot cause an
  email to an address outside the workspace.

## When writing code

- Small, complete files. Working end-to-end vertical slices over broad scaffolding.
- If a decision is not covered by the spec, ask rather than inventing one — this
  project is built module by module and consistency matters more than speed.
