# Build Order — file by file

**Companion to** `docs/SPEC-v2.md`. That document says *what* and *why*; this one says
*in what order* and *which file does what*.

Nine steps. Each step is a vertical slice that ends in something you can run, test and
push. Do not start a step before the one above it is green — the dependency order here
is real, not stylistic.

---

## 0. The finished shape

```
ai-workspace-assistant/
├── .github/workflows/ci.yml
├── .gitignore
├── .env.example
├── README.md
├── CLAUDE.md
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── alembic.ini
├── docs/
│   ├── SPEC-v2.md
│   └── BUILD-ORDER.md
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── app/
│   ├── main.py
│   ├── lifespan.py
│   ├── config.py
│   ├── constants.py
│   ├── exceptions.py
│   ├── dependencies.py
│   ├── middleware/
│   ├── database/
│   │   ├── base.py · mixins.py · session.py
│   │   └── models/            ← one file per aggregate, not one giant module
│   ├── schemas/
│   ├── auth/
│   ├── services/
│   ├── ai/
│   │   ├── provider.py
│   │   ├── chunking/ · embeddings/ · retriever/ · prompts/ · tools/ · agent/ · eval/
│   ├── storage/
│   ├── workers/
│   ├── api/
│   ├── utils/
│   └── tests/
└── frontend/
```

### Deviations from the v1 file list, and why

| v1 | v2 | Reason |
| --- | --- | --- |
| `app/database/models.py` (one file) | `app/database/models/` (one file per aggregate) | Seventeen models in one module becomes unnavigable by week two and produces constant merge conflicts. |
| `app/storage/upload.py`, `delete.py`, `replace.py` | `app/storage/base.py` + `s3.py` + `local.py` | Three verbs are not three modules. One interface with two implementations lets tests run without network. |
| — | `app/workers/` | New. Holds the Postgres job queue that replaced Celery. |
| — | `app/middleware/` | v1 put CORS and rate limiting in `main.py`. They grow; give them a home. |
| — | `app/ai/provider.py` | The single place a model provider is chosen, so swapping Gemini for OpenAI is one file. |
| — | `app/ai/eval/` | The retrieval evaluation set. This is a portfolio differentiator. |
| — | `app/lifespan.py` | Startup and shutdown (migrations, job runner, checkpointer) — keeps `main.py` readable. |

---

## Step 0 — Skeleton and deployment

**Goal:** a public URL returning `{"status":"ok"}`, with CI green, before any feature exists.

| File | Responsibility |
| --- | --- |
| `pyproject.toml` | Dependencies and pinned versions; `ruff` and `mypy` configuration. |
| `.gitignore` | `.env`, `__pycache__`, `.venv`, `*.db`. **Commit this first, before anything else.** |
| `.env.example` | Every environment variable the app reads, with dummy values. The real `.env` is never committed. |
| `app/config.py` | `Settings(BaseSettings)` — the single source of configuration. Nothing else in the codebase reads `os.environ`. |
| `app/main.py` | Creates the `FastAPI` app, mounts middleware and the root router. Thin — no logic. |
| `app/api/health.py` | `GET /health`. **Must not touch the database** — it is pinged every 10 minutes and would burn Neon compute hours. |
| `app/api/router.py` | Aggregates every sub-router. Each later step adds one line here. |
| `app/utils/logger.py` | Structured JSON logging configuration. |
| `Dockerfile` | Multi-stage build, non-root user, `uvicorn` entrypoint. |
| `docker-compose.yml` | Local Postgres 18 + pgvector, and the API. Local development only. |
| `.github/workflows/ci.yml` | `ruff` → `mypy` → `pytest` on every push. |
| `README.md` | Stub for now. Grows every step. |

**Deploy now:** Neon project (**select Postgres 18**), Render web service from the
Dockerfile, Cloudflare Pages placeholder, environment variables set in Render's
dashboard.

**Done when:** the Render URL returns 200, and the CI badge is green.

---

## Step 1 — Database foundation

**Goal:** migrations run against Neon; no models yet.

| File | Responsibility |
| --- | --- |
| `app/database/base.py` | `DeclarativeBase` plus a constraint **naming convention**. Set this before the first migration — without it Alembic generates unnamed constraints that cannot be dropped later. |
| `app/database/mixins.py` | `UUIDPrimaryKey` (server default `uuidv7()`), `Timestamped` (`created_at`, `updated_at`), `WorkspaceScoped` (`workspace_id` FK + index). Every model composes these. |
| `app/database/session.py` | Async engine and `async_sessionmaker`. Pool settings: `pool_size=2, max_overflow=3, pool_pre_ping=True, pool_recycle=300`. Yields `get_db`. |
| `app/utils/ids.py` | UUIDv7 helper for application-side generation (tests, fixtures). |
| `alembic.ini`, `migrations/env.py` | Async Alembic wired to `Settings.database_url` and `Base.metadata`. |
| `migrations/versions/0001_extensions.py` | `CREATE EXTENSION IF NOT EXISTS vector;` and `pg_trgm`. Nothing else. |
| `app/exceptions.py` | `AppError` base, plus `NotFound`, `Forbidden`, `Conflict`, `RateLimited`. |
| `app/middleware/errors.py` | Maps `AppError` to JSON responses. **`NotFound` and `Forbidden` on a cross-tenant resource must both render as 404** (see Step 2). |
| `app/constants.py` | Role names, document status values, job status values, size caps. |

**Done when:** `alembic upgrade head` succeeds against Neon and against local Docker.

---

## Step 2 — Identity, workspaces, and the isolation test

**Goal:** register, log in, create a workspace, invite a member — and prove tenants cannot see each other.

**Models** (`app/database/models/`)

| File | Table |
| --- | --- |
| `user.py` | `users` |
| `workspace.py` | `workspaces` |
| `membership.py` | `workspace_members` |
| `invite.py` | `workspace_invites` |

**Auth**

| File | Responsibility |
| --- | --- |
| `app/auth/password.py` | `bcrypt` hash and verify. Nothing else. |
| `app/auth/jwt.py` | Encode and decode access and refresh tokens; refresh rotation with a `jti`. |
| `app/auth/permissions.py` | `require_role(...)` dependencies — `require_admin`, `require_member`. |
| `app/dependencies.py` | `get_current_user`, and **`get_workspace_context`** — the one dependency that resolves a `workspace_id` path parameter, checks membership, and returns a scoped context. Every tenant-scoped route depends on it. |
| `app/utils/crypto.py` | Fernet encrypt/decrypt for tokens at rest; SHA-256 for invite token hashing. |

**Schemas / services / routes**

| File | Responsibility |
| --- | --- |
| `app/schemas/common.py` | Pagination, error envelope, `ORMModel` base with `from_attributes`. |
| `app/schemas/auth.py` | `RegisterRequest`, `LoginRequest`, `TokenPair`, `UserOut`. |
| `app/schemas/workspace.py` | `WorkspaceCreate`, `WorkspaceOut`, `MemberOut`, `InviteCreate`, `InviteAccept`. |
| `app/services/auth_service.py` | Register, authenticate, rotate refresh token. Owns password rules. |
| `app/services/workspace_service.py` | Create workspace (creator becomes admin in the same transaction), list, list members, change role. |
| `app/services/invite_service.py` | Generate token → store `token_hash` → return raw token once; accept, expire, revoke. |
| `app/api/auth.py` | `/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/me`. |
| `app/api/workspace.py` | `/workspaces`, `/workspaces/{id}/members`, `/invite`, `/join`. |

**Tests** — this is the step where the test suite starts earning its keep.

| File | Responsibility |
| --- | --- |
| `app/tests/conftest.py` | Async test client, per-test transaction rollback, **real Postgres in Docker** — not SQLite, which has neither pgvector nor `SKIP LOCKED`. |
| `app/tests/factories.py` | `make_user`, `make_workspace`, `make_member` builders. |
| `app/tests/test_auth.py` | Registration, duplicate email, login, bad password, refresh rotation, expired token. |
| `app/tests/test_workspace.py` | Create, list, invite, accept, role changes, last-admin-cannot-leave. |
| `app/tests/test_tenant_isolation.py` | **The most important file in the repo.** For every tenant-scoped route: a member of workspace A gets **404**, never 403, for a resource in workspace B. Add a case here whenever you add a route. |

**Migration:** `0002_identity.py`

**Done when:** you can register two users, give each a workspace, and the isolation test passes for every route that exists.

---

## Step 3 — Storage and document upload

**Goal:** a file lands in object storage and a `documents` row exists with status `pending`. No processing yet.

| File | Responsibility |
| --- | --- |
| `app/storage/base.py` | `ObjectStore` protocol: `put`, `get`, `delete`, `signed_url`. |
| `app/storage/s3.py` | S3-compatible implementation over `aioboto3`. Supabase Storage in production, R2 or AWS by changing two settings — see SPEC-v2 D24. |
| `app/storage/local.py` | Filesystem implementation for tests and local development. |
| `app/utils/validators.py` | **Magic-byte** content sniffing, size cap, extension/MIME agreement check. Extension checking alone is not validation. |
| `app/database/models/document.py` | `documents` |
| `app/schemas/document.py` | `DocumentOut`, `DocumentUploadResponse`, `DocumentStatus`. |
| `app/services/document_service.py` | Validate → hash → deduplicate by `content_hash` → store → insert row → enqueue job. Also delete (row + object + chunks). |
| `app/api/documents.py` | `GET /workspaces/{id}/documents`, `POST .../upload`, `DELETE .../{doc_id}`, `GET .../{doc_id}/status`. |
| `app/tests/test_documents.py` | Upload, list, delete, oversize rejection, wrong-type rejection, duplicate detection, cross-tenant 404. |

**Migration:** `0003_documents.py`

**Done when:** an uploaded PDF appears in the bucket and in the documents list with status `pending`. `python -m scripts.check_storage` proves the credentials first.

---

## Step 4 — The ingestion queue

**Goal:** `pending` becomes `ready` on its own, and survives the process being killed mid-job.

| File | Responsibility |
| --- | --- |
| `app/database/models/ingestion_job.py` | `ingestion_jobs` |
| `app/database/models/chunk.py` | `document_chunks`, including `embedding halfvec(768)` and `tsv tsvector`. |
| `app/workers/queue.py` | `enqueue`, and `claim_next` using `FOR UPDATE SKIP LOCKED` with a `lease_until` heartbeat. Also `release`, `fail`, `reclaim_expired`. |
| `app/workers/runner.py` | The asyncio loop started in the lifespan. Polls, claims, dispatches, heartbeats, backs off. |
| `app/workers/jobs/ingest_document.py` | The job body: fetch from object storage → extract text → chunk → embed → insert chunks → mark `ready`. **Must be idempotent** — delete existing chunks for the document before inserting. |
| `app/ai/chunking/splitter.py` | Text extraction per MIME type and splitting into chunks with page numbers preserved. |
| `app/ai/provider.py` | `get_chat_model()` and `get_embedder()` from `Settings`. The only file that names a provider. |
| `app/ai/embeddings/embedder.py` | Batched embedding calls, `gemini-embedding-2` at `output_dimensionality=768`, retry with backoff on 429. |
| `app/lifespan.py` | Starts and cleanly stops the runner; runs `alembic upgrade head` behind a Postgres advisory lock. |
| `app/tests/test_ingestion.py` | Job claimed once under concurrency; expired lease reclaimed; failure increments `attempts`; re-ingest is idempotent; document reaches `ready`. |

**Migration:** `0004_chunks_and_jobs.py` — creates the HNSW index:

```sql
CREATE INDEX ix_chunks_embedding ON document_chunks
  USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX ix_chunks_tsv ON document_chunks USING gin (tsv);
```

**Done when:** you upload a document, wait, and it flips to `ready` with a non-zero `chunk_count` — and it still does after you kill the container mid-ingest.

---

## Step 5 — Retrieval and RAG chat

**Goal:** ask a question, get an answer with citations. Still no agent.

| File | Responsibility |
| --- | --- |
| `app/database/models/chat.py` | `chat_threads`, `chat_messages` |
| `app/database/models/citation.py` | `message_citations` |
| `app/ai/retriever/vector.py` | Cosine search over `document_chunks`, filtered by `workspace_id`, `SET LOCAL hnsw.ef_search = 60`. |
| `app/ai/retriever/keyword.py` | Postgres full-text search over `tsv`, same workspace filter. |
| `app/ai/retriever/hybrid.py` | Runs both and fuses with Reciprocal Rank Fusion. This is the default retriever. |
| `app/ai/prompts/system.py` | The base system prompt, including the rule that delimited content is data, never instructions. |
| `app/ai/prompts/rag.py` | Answer template. Retrieved chunks go in a **user-role** message inside delimiters — never in the system prompt. |
| `app/schemas/chat.py` | `ChatRequest`, `ChatResponse`, `CitationOut`, `ThreadOut`. |
| `app/services/chat_service.py` | Thread resolution → retrieve → prompt → call model → persist message → persist citations with `quoted_text`. |
| `app/api/chat.py` | `POST /chat/query`, `GET /chat/history`, `GET /chat/threads`. |
| `app/ai/eval/dataset.yaml` | 20–30 questions with expected answers and expected source documents. |
| `app/ai/eval/run_eval.py` | Runs the set, reports retrieval hit-rate and answer overlap. Runs in CI as a non-blocking job. |
| `app/tests/test_retrieval.py` | Hybrid beats vector-only on an exact-term query; retrieval never crosses workspaces. |
| `app/tests/test_chat.py` | Answer carries citations; citations survive deletion of the source document. |

**Migration:** `0005_chat.py`

**Done when:** a question over your uploaded documents returns a grounded answer with working citations, and `run_eval.py` prints a hit-rate.

---

## Step 6 — The agent and the approval gate

**Goal:** the security centrepiece. Nothing here sends anything yet.

| File | Responsibility |
| --- | --- |
| `app/database/models/pending_action.py` | `pending_actions`, including `payload_hash`. |
| `app/database/models/audit_log.py` | `audit_log` |
| `app/ai/agent/state.py` | The graph state type. |
| `app/ai/agent/checkpointer.py` | `AsyncPostgresSaver` against the same Neon database; table setup on startup. |
| `app/ai/agent/graph.py` | `create_agent(...)` with `HumanInTheLoopMiddleware(interrupt_on={...})`. Read-only tools auto-approve; every side-effecting tool interrupts. |
| `app/ai/tools/search.py` | Wraps the hybrid retriever as a tool. The only auto-approved tool. |
| `app/ai/tools/resolve.py` | **Resolves member names to email addresses server-side.** The model may name a person; it may never supply an address. Anything unresolvable raises. |
| `app/ai/tools/email.py` | Declares the `send_email` tool signature — recipients as member references, not addresses. |
| `app/ai/tools/calendar.py` | Declares `create_event`. |
| `app/ai/tools/tasks.py` | Declares `create_tasks`. |
| `app/schemas/approval.py` | `PendingActionOut`, `ApprovalDecision` (`approve` / `edit` / `reject`), each carrying the `payload_hash` the user was shown. |
| `app/services/agent_service.py` | Invokes the graph with `thread_id`, catches the interrupt, writes a `pending_actions` row with the payload hash. |
| `app/services/approval_service.py` | On decision: **re-hash the payload and compare**; mismatch is rejected. Then resume with `Command(resume=...)`, execute, and write to `audit_log`. |
| `app/services/audit_service.py` | Append-only writer. Never updates or deletes. |
| `app/api/approvals.py` | `GET /workspaces/{id}/pending-actions`, `POST .../{action_id}/decide`. |
| `app/tests/test_agent_approval.py` | Interrupt creates a pending action; approve executes exactly once; reject executes nothing; **a mutated payload is refused**; approval by a non-admin is refused. |
| `app/tests/test_prompt_injection.py` | A poisoned document instructing the agent to email an outside address — assert the resolver refuses and no pending action with an external recipient is ever created. |

**Migration:** `0006_agent.py`

**Done when:** asking the agent to email the team produces a pending action you must approve, approving it twice executes once, and the injection test passes.

---

## Step 7 — The side effects

**Goal:** approved actions actually do something.

| File | Responsibility |
| --- | --- |
| `app/database/models/task.py` | `tasks` |
| `app/database/models/calendar_event.py` | `calendar_events` |
| `app/database/models/oauth_credential.py` | `oauth_credentials` — refresh tokens encrypted via `utils/crypto`. |
| `app/services/email_service.py` | `EmailProvider` protocol + Resend implementation. `From:` no-reply, `Reply-To:` the requesting user. Gmail is a stub class documenting the restricted-scope blocker. |
| `app/services/calendar_service.py` | Builds an `.ics` invitation; optional Google Calendar path behind the same interface. |
| `app/services/task_service.py` | Task CRUD, plus the bulk create the agent tool calls. |
| `app/auth/oauth.py` | Google OAuth start and callback. Optional — build only if you want the Calendar path. |
| `app/schemas/task.py`, `email.py`, `calendar.py` | Request and response models. |
| `app/api/tasks.py` | Task CRUD routes. |
| `app/api/email.py` | Email status and manual send (still approval-gated). |
| `app/api/calendar.py` | Event creation and `.ics` download. |
| `app/tests/test_tasks.py` | CRUD, assignment restricted to workspace members, cross-tenant 404. |

**Migration:** `0007_side_effects.py`

**Done when:** an approved email arrives in a real inbox and an approved event downloads as a valid `.ics`.

---

## Step 8 — Streaming, limits, and admin

**Goal:** it feels like a product and cannot bankrupt you.

| File | Responsibility |
| --- | --- |
| `app/database/models/usage_event.py` | `usage_events` |
| `app/services/usage_service.py` | Records tokens per call; checks the per-workspace daily budget **before** the model is invoked. |
| `app/middleware/rate_limit.py` | Per-user and per-IP request limits, Postgres-backed. Returns 429 with `Retry-After`. |
| `app/middleware/request_context.py` | Attaches `request_id`, `user_id`, `workspace_id` to every log line. |
| `app/api/chat.py` (extended) | `GET /chat/stream` using `EventSourceResponse` from **`sse_starlette.sse`** — not `starlette.responses`. |
| `app/api/admin.py` | `GET /admin/usage`, `GET /admin/users`. Reads `usage_events`. |
| `app/schemas/admin.py` | Usage and metrics response models. |
| `app/tests/test_rate_limit.py` | Limit trips at the right count; budget exhaustion blocks before the LLM call, not after. |

**Migration:** `0008_usage.py`

**Done when:** tokens stream into the UI, and a scripted burst of requests gets 429 rather than a Gemini bill.

---

## Step 9 — Frontend and the README

**Goal:** the part a reviewer actually looks at.

| Area | Files | Responsibility |
| --- | --- | --- |
| Shell | `src/main.tsx`, `App.tsx`, `router.tsx` | Routes and providers. |
| API layer | `src/api/client.ts` | Fetch wrapper. Access token in a module variable; on 401, silently refresh via the httpOnly cookie and retry once. |
| Auth | `src/features/auth/` | Login, register, accept-invite pages; auth context. |
| Workspaces | `src/features/workspaces/` | Switcher, member list, invite dialog. |
| Documents | `src/features/documents/` | Drag-drop upload, status polling, delete. |
| Chat | `src/features/chat/` | Message list, SSE stream consumer, **citation chips that open the quoted text**. |
| Approvals | `src/features/approvals/` | The pending-action card with Approve / Edit / Reject. **Make this the screenshot in your README.** |
| Tasks | `src/features/tasks/` | Board or list view. |

**And the README**, which is 30% of the value of a portfolio project:

- One-paragraph description, live URL, and the cold-start warning.
- The architecture diagram.
- A 30-second screen recording of the approval flow.
- A short "Security model" section — untrusted documents, server-side recipient
  resolution, payload-hash-bound approval. Link the two tests that prove it.
- The non-goals list from `SPEC-v2.md`.
- Local setup in four commands.

---

## Table reference (corrected)

Seventeen tables. `WS` marks tenant-scoped tables carrying `workspace_id`.
All primary keys are `UUID` with a `uuidv7()` server default.

| # | Table | WS | Columns | Created in |
| --- | --- | :---: | --- | --- |
| 1 | `users` | — | `id`, `email` citext unique, `password_hash`, `name`, `is_active`, `created_at`, `updated_at` | 0002 |
| 2 | `workspaces` | — | `id`, `name`, `owner_id`→users, `created_at` | 0002 |
| 3 | `workspace_members` | ✓ | `id`, `workspace_id`, `user_id`, `role` enum(`admin`,`member`,`viewer`), `joined_at` · unique `(workspace_id, user_id)` | 0002 |
| 4 | `workspace_invites` | ✓ | `id`, `workspace_id`, `email`, **`token_hash`**, `invited_by`, `role`, `status` enum, `expires_at`, `created_at` · partial unique `(workspace_id, email)` where `status='pending'` | 0002 |
| 5 | `documents` | ✓ | `id`, `workspace_id`, `name`, `storage_key`, **`content_hash`**, **`mime_type`**, **`size_bytes`**, `uploaded_by`, `uploaded_at`, `status` enum(`pending`,`processing`,`ready`,`failed`), **`chunk_count`**, **`error_message`**, **`embedding_model`** · unique `(workspace_id, content_hash)` | 0003 |
| 6 | `document_chunks` | ✓ | `id`, `document_id`, `workspace_id`, `text`, `page_no`, `chunk_index`, **`embedding halfvec(768)`**, **`tsv tsvector`** · HNSW + GIN indexes | 0004 |
| 7 | `ingestion_jobs` | ✓ | `id`, `workspace_id`, `document_id`, `status`, `attempts`, `lease_until`, `last_error`, `created_at` · index `(status, lease_until)` | 0004 |
| 8 | `chat_threads` | ✓ | `id`, `workspace_id`, `user_id`, `title`, `created_at` | 0005 |
| 9 | `chat_messages` | ✓ | `id`, `thread_id`, `workspace_id`, `user_id` nullable, **`role` enum(`user`,`assistant`,`tool`)**, `content`, `created_at` · index `(thread_id, created_at DESC)` | 0005 |
| 10 | `message_citations` | ✓ | `id`, `message_id`, `chunk_id` nullable `ON DELETE SET NULL`, `document_name`, **`quoted_text`**, `score` | 0005 |
| 11 | `pending_actions` | ✓ | `id`, `workspace_id`, `thread_id`, `type`, `payload` jsonb, **`payload_hash`**, `status` enum(`pending`,`approved`,`rejected`,`executed`), `initiated_by`, `decided_by`, `decided_at`, `created_at` | 0006 |
| 12 | `audit_log` | ✓ | `id`, `workspace_id`, `user_id`, `action`, `details` **jsonb**, `created_at` | 0006 |
| 13 | `tasks` | ✓ | `id`, `workspace_id`, `title`, `description`, **`assigned_to`→users**, **`source_message_id`** nullable, `status` enum, `due_date`, `created_at` | 0007 |
| 14 | `calendar_events` | ✓ | `id`, `workspace_id`, `title`, `start_time`, `end_time`, `created_by`, `ics_uid`, `external_event_id` nullable | 0007 |
| 15 | `oauth_credentials` | — | `id`, `user_id`, `service`, `refresh_token_enc`, `scopes`, `expires_at` · unique `(user_id, service)` | 0007 |
| 16 | `usage_events` | ✓ | `id`, `workspace_id`, `user_id`, `kind`, `tokens_in`, `tokens_out`, `created_at` | 0008 |
| 17 | *LangGraph checkpoint tables* | — | Created by `AsyncPostgresSaver.setup()` — do not hand-write these | 0006 (runtime) |

**Gone from v1:** the `embeddings` table (folded into `document_chunks`) and
`chat_msg_chunks` (replaced by `message_citations`).

---

## Rules that hold at every step

1. A route never imports from `ai/` or `database/models/` directly — it goes through `services/`.
2. Every tenant-scoped query goes through `get_workspace_context`, never a hand-written `workspace_id` filter.
3. Cross-tenant access returns **404**, never 403.
4. Every new route gets a case in `test_tenant_isolation.py` in the same commit.
5. Every schema change is an Alembic migration. Never `create_all()` outside tests.
6. No LLM output reaches an external API without a human decision bound to a payload hash.
7. Commit at the end of each step, with the step's tests passing.
