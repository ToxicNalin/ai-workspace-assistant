from enum import StrEnum


class WorkspaceRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class IngestionJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


# SPEC-v2 §7: upload size cap.
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024

# SPEC-v2 §4: ~8-10 KB per chunk against Neon's 0.5 GB free tier puts the
# ceiling at roughly 500-800 typical documents; 750 is the midpoint.
MAX_DOCUMENTS_PER_WORKSPACE = 750


# SPEC-v2 §4/§5: 768-dim halfvec rather than 1536-dim vector -- a 4x storage
# reduction for negligible recall difference, which is what keeps the corpus
# inside Neon's 0.5 GB free tier.
EMBEDDING_DIMENSIONS = 768

# ~1 KB of text per chunk is what SPEC-v2 §4's storage budget assumes. The
# overlap exists so a sentence straddling a chunk boundary is still
# retrievable from at least one side of it.
CHUNK_SIZE_CHARS = 1000
CHUNK_OVERLAP_CHARS = 150

EMBEDDING_BATCH_SIZE = 32
MAX_EMBEDDING_ATTEMPTS = 5

# A claimed job is leased for this long and the runner extends it while it
# works. A job whose lease expires is reclaimed by the next process to poll --
# this is what makes the free tier killing the process mid-job survivable.
INGESTION_LEASE_SECONDS = 300
INGESTION_HEARTBEAT_SECONDS = 60

# After this many attempts the document is marked failed with error_message
# set, rather than retried forever (SPEC-v2 §3).
MAX_INGESTION_ATTEMPTS = 3

# How long the runner waits before polling again when it found no work, and
# the ceiling that wait backs off to. A fixed short poll would keep Neon awake
# permanently and burn the 100 CU-hours/month the free tier allows -- SPEC-v2
# §7 warns against pinging the *database* even once a minute, and scale-to-zero
# is what keeps the project inside the budget.
INGESTION_POLL_SECONDS = 5.0
INGESTION_MAX_POLL_SECONDS = 30.0


# SPEC-v2 §5: the useful range for hnsw.ef_search is 40-200, and values much
# above that can flip the planner to a sequential scan.
HNSW_EF_SEARCH = 60

# Each retriever returns this many candidates before fusion; the fused list is
# then cut to RETRIEVAL_TOP_K.
RETRIEVAL_CANDIDATES = 20
RETRIEVAL_TOP_K = 6

# Reciprocal Rank Fusion's damping constant. 60 is the value from the original
# RRF paper and the one every implementation since has used -- it keeps a
# result ranked first in one retriever from dominating a result ranked
# consistently well in both.
RRF_K = 60

# How much of a chunk is snapshotted onto a citation. The snapshot is what
# survives the source document being deleted (SPEC-v2 D5), so it has to be
# long enough to stand as evidence on its own.
CITATION_QUOTE_CHARS = 400

CHAT_TITLE_MAX_CHARS = 80


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class PendingActionType(StrEnum):
    SEND_EMAIL = "send_email"
    CREATE_EVENT = "create_event"
    CREATE_TASKS = "create_tasks"


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    # The server refused to even offer this action for approval, because the
    # model named a recipient that is not a member of the workspace (D21).
    # Recorded rather than discarded: a refusal is the interesting event.
    REFUSED = "refused"
    # Approved by a human, but the side effect itself did not happen -- the
    # email provider was down, the payload would not parse. Distinct from
    # `executed` on purpose: "approved and sent" and "approved and lost" are
    # not the same fact, and a status that conflates them makes the audit log
    # unable to answer the only question anyone will ask of it.
    FAILED = "failed"


class AuditAction(StrEnum):
    ACTION_PROPOSED = "action.proposed"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_EXECUTED = "action.executed"
    ACTION_REFUSED = "action.refused"
    APPROVAL_HASH_MISMATCH = "action.hash_mismatch"
    # Step 7. Approved, attempted, and it did not work.
    ACTION_FAILED = "action.failed"
    # The side effects themselves, recorded separately from the approval that
    # authorised them so "what actually left this workspace" is one query.
    EMAIL_SENT = "email.sent"
    EVENT_CREATED = "event.created"
    TASKS_CREATED = "tasks.created"


# The read-only tool the agent may call without asking anyone (SPEC-v2 §5).
AUTO_APPROVED_TOOL = "search_documents"

# How many agent turns may run before we stop. A model that loops through
# tool calls is a bill, and on a free tier it is the whole bill.
MAX_AGENT_STEPS = 8


# --- Step 7: the side effects -------------------------------------------


class PendingActionOrigin(StrEnum):
    """Who proposed this action.

    The agent's proposals are attached to a paused LangGraph run that has to be
    resumed once a human decides. A proposal made directly through the API has
    no graph waiting on it, and trying to resume one that does not exist would
    invoke the model against an empty checkpoint. The distinction is a column
    rather than an inference from `thread_id` because guessing at it is exactly
    the kind of thing that works until it doesn't.
    """

    AGENT = "agent"
    MANUAL = "manual"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class OAuthService(StrEnum):
    """Services a user could connect. Neither is shipped -- see SPEC-v2 D16/D17
    and app/services/calendar_service.py for why both are behind an interface
    rather than in production."""

    GOOGLE_CALENDAR = "google_calendar"
    GMAIL = "gmail"


# A batch the agent proposes in one go. Bounded because "create tasks for
# every line in this document" against a poisoned file is otherwise a way to
# fill a 0.5 GB database from a chat message.
MAX_TASKS_PER_ACTION = 25

# Likewise for an invitation: a workspace on the free tier will not have more
# members than this, so a larger guest list means something has gone wrong.
MAX_EVENT_GUESTS = 50

# RFC 5545 identifies the software that produced a calendar object.
ICS_PRODID = "-//AI Workspace Assistant//Agent//EN"

# RFC 5545 §3.1: content lines are folded at 75 octets, excluding the CRLF.
ICS_FOLD_OCTETS = 75

# Resend's REST endpoint. Named here rather than in the service so the one
# outbound URL this application posts to is visible next to everything else
# that is configured (SPEC-v2 D16).
RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 15.0
