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
