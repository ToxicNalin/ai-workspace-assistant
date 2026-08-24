"""Step 4: the Postgres job queue that replaced Celery.

The properties worth proving here are the ones that make running the worker
inside the API process survivable on a free tier that can kill it at any
moment: a job is claimed exactly once even under concurrency, a job whose
worker died is reclaimed, a job that keeps failing eventually gives up with a
message the user can see, and re-running a job never duplicates anything.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chunking.splitter import Page, extract_pages, split_pages, split_text
from app.ai.embeddings.embedder import FakeEmbedder
from app.constants import (
    EMBEDDING_DIMENSIONS,
    MAX_INGESTION_ATTEMPTS,
    DocumentStatus,
    IngestionJobStatus,
)
from app.database.models.chunk import DocumentChunk
from app.database.models.document import Document
from app.database.models.ingestion_job import IngestionJob
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.session import async_session_factory
from app.lifespan import run_migrations
from app.storage.base import get_object_store
from app.tests.factories import (
    auth_headers,
    make_pdf_bytes,
    make_user,
    make_workspace,
    random_email,
)
from app.workers import queue
from app.workers.jobs.ingest_document import NoExtractableText, ingest_document
from app.workers.runner import IngestionRunner

_TEXT = b"First paragraph of the document.\n\nSecond paragraph, with more words in it.\n"


async def _upload(
    client: AsyncClient,
    user: User,
    workspace: Workspace,
    *,
    name: str = "notes.txt",
    content: bytes = _TEXT,
) -> uuid.UUID:
    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": (name, content, "application/octet-stream")},
        headers=auth_headers(user),
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["document"]["id"])


async def _count_chunks(db: AsyncSession, document_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    )
    return count or 0


async def _count_jobs(db: AsyncSession, document_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(IngestionJob)
        .where(IngestionJob.document_id == document_id)
    )
    return count or 0


# --------------------------------------------------------------------------
# The splitter -- no database involved.
# --------------------------------------------------------------------------


def test_short_text_is_a_single_chunk() -> None:
    assert split_text("One short paragraph.", size=1000, overlap=100) == ["One short paragraph."]


def test_paragraphs_are_kept_whole_where_they_fit() -> None:
    joined = "\n\n".join(["A" * 40, "B" * 40, "C" * 40])

    chunks = split_text(joined, size=100, overlap=10)

    assert len(chunks) > 1
    # A chunk cut mid-sentence retrieves badly and reads worse quoted back as
    # a citation, so a paragraph that fits must stay contiguous.
    assert any("A" * 40 in chunk for chunk in chunks)
    assert any("C" * 40 in chunk for chunk in chunks)


def test_a_paragraph_longer_than_a_chunk_is_hard_split() -> None:
    chunks = split_text("X" * 250, size=100, overlap=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_chunks_never_span_a_page_boundary() -> None:
    """A chunk carrying page 1's number must not contain page 2's text -- the
    citation UI shows that number to the user."""
    pages = [Page(text="Alpha content.", page_no=1), Page(text="Beta content.", page_no=2)]

    chunks = split_pages(pages, size=1000, overlap=100)

    assert len(chunks) == 2
    assert chunks[0].page_no == 1
    assert "Beta" not in chunks[0].text
    assert chunks[1].page_no == 2
    assert "Alpha" not in chunks[1].text
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]


def test_pdf_page_numbers_survive_extraction() -> None:
    data = make_pdf_bytes(["Alpha page one.", "Beta page two.", "Gamma page three."])

    pages = extract_pages(data, mime_type="application/pdf")

    assert [page.page_no for page in pages] == [1, 2, 3]
    assert "Beta" in pages[1].text


def test_plain_text_has_no_page_number() -> None:
    pages = extract_pages(b"Just some text.", mime_type="text/plain")

    assert len(pages) == 1
    assert pages[0].page_no is None


async def test_fake_embedder_is_deterministic_and_normalised() -> None:
    embedder = FakeEmbedder()

    first, second = await embedder.embed_documents(["same text", "same text"])

    assert first == second
    assert len(first) == EMBEDDING_DIMENSIONS
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


# --------------------------------------------------------------------------
# Enqueue and ingest.
# --------------------------------------------------------------------------


async def test_upload_enqueues_a_pending_job(db_session: AsyncSession, client: AsyncClient) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)

    document_id = await _upload(client, user, workspace)

    job = await db_session.scalar(
        select(IngestionJob).where(IngestionJob.document_id == document_id)
    )
    assert job is not None
    assert job.status == IngestionJobStatus.PENDING
    assert job.attempts == 0
    assert job.workspace_id == workspace.id


async def test_enqueueing_twice_does_not_duplicate_a_live_job(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Duplicate jobs would embed the same text twice, and embedding quota is
    the scarcest thing in this project."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)

    await queue.enqueue(db_session, workspace_id=workspace.id, document_id=document_id)
    await db_session.commit()

    assert await _count_jobs(db_session, document_id) == 1


async def test_ingestion_chunks_embeds_and_marks_ready(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(
        client, user, workspace, name="report.pdf", content=make_pdf_bytes(["Some report text."])
    )
    embedder = FakeEmbedder()

    written = await ingest_document(
        db_session, get_object_store(), embedder, document_id=document_id
    )

    assert written == 1
    document = await db_session.get(Document, document_id)
    assert document is not None
    await db_session.refresh(document)
    assert document.status == DocumentStatus.READY
    assert document.chunk_count == 1
    assert document.error_message is None
    assert document.embedding_model == embedder.model_name

    chunk = await db_session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    assert chunk is not None
    assert "report text" in chunk.text
    assert chunk.page_no == 1
    assert len(chunk.embedding) == EMBEDDING_DIMENSIONS
    # Carried on the chunk itself, so retrieval filters by workspace without
    # joining back through documents.
    assert chunk.workspace_id == workspace.id


async def test_reingesting_is_idempotent(db_session: AsyncSession, client: AsyncClient) -> None:
    """A process killed mid-job leaves the document to be done again from the
    top. Twice through must not leave two copies of every chunk."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(
        client, user, workspace, name="long.txt", content=b"Para one.\n\nPara two.\n\nPara three."
    )

    first = await ingest_document(
        db_session, get_object_store(), FakeEmbedder(), document_id=document_id
    )
    second = await ingest_document(
        db_session, get_object_store(), FakeEmbedder(), document_id=document_id
    )

    assert first == second
    assert await _count_chunks(db_session, document_id) == second

    document = await db_session.get(Document, document_id)
    assert document is not None
    await db_session.refresh(document)
    assert document.chunk_count == second


async def test_tsvector_is_generated_by_postgres(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The full-text column is generated, so it cannot drift from the text it
    indexes -- Step 5's keyword retriever depends on that."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(
        client, user, workspace, name="tsv.txt", content=b"Quarterly revenue projections."
    )

    await ingest_document(db_session, get_object_store(), FakeEmbedder(), document_id=document_id)

    matched = await db_session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.tsv.op("@@")(func.plainto_tsquery("english", "revenue")),
        )
    )
    assert matched == 1


async def test_a_file_with_no_extractable_text_is_refused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    # A structurally valid PDF whose pages carry no text -- what a scan looks
    # like before OCR.
    document_id = await _upload(
        client, user, workspace, name="scan.pdf", content=make_pdf_bytes(["", ""])
    )

    with pytest.raises(NoExtractableText):
        await ingest_document(
            db_session, get_object_store(), FakeEmbedder(), document_id=document_id
        )


async def test_ingesting_a_deleted_document_is_not_an_error(db_session: AsyncSession) -> None:
    """The document can be deleted between enqueue and claim. That is a no-op,
    not a failure that burns an attempt."""
    written = await ingest_document(
        db_session, get_object_store(), FakeEmbedder(), document_id=uuid.uuid4()
    )

    assert written == 0


async def test_deleting_a_document_removes_its_chunks_and_job(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)
    await ingest_document(db_session, get_object_store(), FakeEmbedder(), document_id=document_id)
    assert await _count_chunks(db_session, document_id) > 0

    response = await client.delete(
        f"/workspaces/{workspace.id}/documents/{document_id}", headers=auth_headers(user)
    )
    assert response.status_code == 204

    assert await _count_chunks(db_session, document_id) == 0
    assert await _count_jobs(db_session, document_id) == 0


# --------------------------------------------------------------------------
# Claiming, leases and giving up.
# --------------------------------------------------------------------------


async def test_claiming_marks_the_job_running_and_counts_the_attempt(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)

    job = await queue.claim_next(db_session)

    assert job is not None
    assert job.document_id == document_id
    assert job.status == IngestionJobStatus.RUNNING
    assert job.attempts == 1
    assert job.lease_until is not None


async def test_a_claimed_job_is_not_claimed_again_while_its_lease_holds(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await _upload(client, user, workspace)

    first = await queue.claim_next(db_session)
    second = await queue.claim_next(db_session)

    assert first is not None
    assert second is None


async def test_an_expired_lease_is_reclaimed(db_session: AsyncSession, client: AsyncClient) -> None:
    """The crash-recovery property. A worker that died leaves a running job
    with a lease nobody is extending; the next process to poll takes it over."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await _upload(client, user, workspace)

    claimed = await queue.claim_next(db_session)
    assert claimed is not None

    # Simulate the worker dying: nothing heartbeats, so the lease lapses.
    await db_session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == claimed.id)
        .values(lease_until=text("now() - interval '1 hour'"))
    )
    await db_session.commit()

    reclaimed = await queue.claim_next(db_session)

    assert reclaimed is not None
    assert reclaimed.id == claimed.id
    assert reclaimed.attempts == 2


async def test_a_failure_short_of_the_limit_goes_back_to_pending(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)
    claimed = await queue.claim_next(db_session)
    assert claimed is not None

    given_up = await queue.fail(db_session, claimed.id, error="transient boom")

    assert given_up is False
    job = await db_session.get(IngestionJob, claimed.id)
    assert job is not None
    await db_session.refresh(job)
    assert job.status == IngestionJobStatus.PENDING
    assert job.last_error == "transient boom"
    assert job.lease_until is None

    document = await db_session.get(Document, document_id)
    assert document is not None
    await db_session.refresh(document)
    assert document.status != DocumentStatus.FAILED


async def test_the_final_failure_marks_the_document_failed_with_a_message(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """After the attempt budget is spent the document must carry an
    error_message -- that string is what the UI shows the user."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)
    claimed = await queue.claim_next(db_session)
    assert claimed is not None

    await db_session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == claimed.id)
        .values(attempts=MAX_INGESTION_ATTEMPTS)
    )
    await db_session.commit()

    given_up = await queue.fail(db_session, claimed.id, error="permanent boom")

    assert given_up is True
    job = await db_session.get(IngestionJob, claimed.id)
    assert job is not None
    await db_session.refresh(job)
    assert job.status == IngestionJobStatus.FAILED

    document = await db_session.get(Document, document_id)
    assert document is not None
    await db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    assert document.error_message == "permanent boom"


async def test_reuploading_a_failed_document_queues_a_fresh_attempt(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Re-uploading is the only retry handle a user has, and dedup by content
    hash would otherwise swallow it silently."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document_id = await _upload(client, user, workspace)

    claimed = await queue.claim_next(db_session)
    assert claimed is not None
    await db_session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == claimed.id)
        .values(attempts=MAX_INGESTION_ATTEMPTS)
    )
    await db_session.commit()
    assert await queue.fail(db_session, claimed.id, error="permanent boom") is True

    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("notes.txt", _TEXT, "application/octet-stream")},
        headers=auth_headers(user),
    )
    assert response.status_code == 200
    assert response.json()["deduplicated"] is True
    assert response.json()["document"]["status"] == "pending"

    job = await db_session.scalar(
        select(IngestionJob).where(
            IngestionJob.document_id == document_id,
            IngestionJob.status == IngestionJobStatus.PENDING,
        )
    )
    assert job is not None
    assert job.attempts == 0


async def test_completing_a_job_clears_its_lease(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await _upload(client, user, workspace)
    claimed = await queue.claim_next(db_session)
    assert claimed is not None

    await queue.complete(db_session, claimed.id)

    job = await db_session.get(IngestionJob, claimed.id)
    assert job is not None
    await db_session.refresh(job)
    assert job.status == IngestionJobStatus.DONE
    assert job.lease_until is None


async def test_releasing_a_job_returns_it_to_the_queue(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Graceful shutdown hands the job straight back rather than making the
    next process wait out a five-minute lease."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await _upload(client, user, workspace)
    claimed = await queue.claim_next(db_session)
    assert claimed is not None

    await queue.release(db_session, claimed.id)

    reclaimed = await queue.claim_next(db_session)
    assert reclaimed is not None
    assert reclaimed.id == claimed.id


# --------------------------------------------------------------------------
# Genuine concurrency. SKIP LOCKED cannot be exercised on one connection.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def committed_workspace() -> AsyncGenerator[tuple[Workspace, User], None]:
    """Rows that really are committed, on their own connections.

    Every other test here runs inside one connection's transaction and is
    rolled back at the end. That cannot demonstrate FOR UPDATE SKIP LOCKED,
    which is about two *separate* transactions racing -- so this fixture
    commits for real and cleans up afterwards. Deleting the workspace cascades
    to its documents, chunks and jobs; the user has to go separately.
    """
    async with async_session_factory() as db:
        user = await make_user(db, email=random_email())
        workspace = await make_workspace(db, owner=user)

    try:
        yield workspace, user
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


async def test_concurrent_workers_never_claim_the_same_job(
    committed_workspace: tuple[Workspace, User],
) -> None:
    """The property that lets this scale to several replicas unchanged.

    Asserted as "exactly one of the two claims took *our* job" rather than
    "the other got nothing", so an unrelated queued job sitting in the same
    database cannot make it flap.
    """
    workspace, user = committed_workspace

    async with async_session_factory() as db:
        document = Document(
            workspace_id=workspace.id,
            name="race.txt",
            storage_key=f"{workspace.id}/race.txt",
            content_hash=uuid.uuid4().hex,
            mime_type="text/plain",
            size_bytes=len(_TEXT),
            uploaded_by=user.id,
            status=DocumentStatus.PENDING,
        )
        db.add(document)
        await db.flush()
        await queue.enqueue(db, workspace_id=workspace.id, document_id=document.id)
        await db.commit()
        document_id = document.id

    async def claim() -> uuid.UUID | None:
        async with async_session_factory() as db:
            job = await queue.claim_next(db)
            return job.document_id if job is not None else None

    first, second = await asyncio.gather(claim(), claim())

    claims_on_our_job = [result for result in (first, second) if result == document_id]
    assert len(claims_on_our_job) == 1, (
        f"expected exactly one worker to claim the job, got {first!r} and {second!r}"
    )


# --------------------------------------------------------------------------
# The runner and the lifespan -- Step 4's actual acceptance criterion.
# --------------------------------------------------------------------------


async def _wait_until(
    check: Callable[[AsyncSession], Awaitable[bool]], description: str, *, timeout: float = 60.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with async_session_factory() as db:
            if await check(db):
                return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out after {timeout}s waiting for {description}")


async def test_the_runner_drives_a_document_from_pending_to_ready(
    committed_workspace: tuple[Workspace, User],
) -> None:
    """Step 4 is done when an uploaded document flips to ready on its own.

    Everything above tests the pieces; this drives the actual asyncio loop
    that the FastAPI lifespan starts, against really committed rows.
    """
    workspace, user = committed_workspace
    storage_key = f"{workspace.id}/runner-{uuid.uuid4()}.txt"
    await get_object_store().put(storage_key, _TEXT, content_type="text/plain")

    async with async_session_factory() as db:
        document = Document(
            workspace_id=workspace.id,
            name="runner.txt",
            storage_key=storage_key,
            content_hash=uuid.uuid4().hex,
            mime_type="text/plain",
            size_bytes=len(_TEXT),
            uploaded_by=user.id,
            status=DocumentStatus.PENDING,
        )
        db.add(document)
        await db.flush()
        await queue.enqueue(db, workspace_id=workspace.id, document_id=document.id)
        await db.commit()
        document_id = document.id

    async def is_ready(db: AsyncSession) -> bool:
        document = await db.get(Document, document_id)
        return document is not None and document.status is DocumentStatus.READY

    async def job_is_finished(db: AsyncSession) -> bool:
        return await _count_jobs(db, document_id) == 0

    runner = IngestionRunner(async_session_factory, poll_seconds=0.05)
    runner.start()
    try:
        await _wait_until(is_ready, "the document to reach ready")
        # Waited for separately and deliberately: the document is marked ready
        # in one transaction and the job retired in the next, so stopping the
        # runner the instant the document flips would cancel it in between --
        # at which point it correctly releases the job for retry, and there is
        # still a row here.
        await _wait_until(job_is_finished, "the job to be retired")
    finally:
        await runner.stop()

    async with async_session_factory() as db:
        ready = await db.get(Document, document_id)
        assert ready is not None
        assert ready.chunk_count > 0
        assert ready.embedding_model == FakeEmbedder().model_name
        assert await _count_chunks(db, document_id) == ready.chunk_count


async def test_the_runner_stops_cleanly_when_there_is_no_work() -> None:
    """stop() must not hang. On Render the shutdown path runs on every deploy
    and every spin-down, so a runner that blocks here would look like a hung
    process rather than a clean exit."""
    runner = IngestionRunner(async_session_factory, poll_seconds=30.0)
    runner.start()

    await asyncio.wait_for(runner.stop(), timeout=5.0)


async def test_boot_time_migrations_are_idempotent() -> None:
    """The lifespan runs `alembic upgrade head` on every boot behind an
    advisory lock. Already being at head must be a no-op, not an error -- and
    this is the only coverage of env.py's handed-in-connection path, which
    otherwise runs for the first time in production.
    """
    await run_migrations()
    await run_migrations()
