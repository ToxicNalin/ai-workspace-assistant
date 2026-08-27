/**
 * The wire format, mirroring the parts of `app/schemas/` this app handles.
 *
 * A subset, deliberately: the admin, audit and calendar responses have no
 * screen here yet, so they have no type here either.
 *
 * Hand-written rather than generated from the OpenAPI document. A generator
 * would be the right answer on a team; here it would add a build step and a
 * checked-in artefact to keep in sync, for a surface small enough to read in
 * one sitting. The trade is that these can drift, so anything that changes on
 * the server changes here in the same commit.
 */

export type Uuid = string;
/** ISO 8601, as FastAPI serialises `datetime`. */
export type Timestamp = string;

export type WorkspaceRole = "admin" | "member" | "viewer";
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";
export type ChatRole = "user" | "assistant" | "tool";
export type TaskStatus = "todo" | "in_progress" | "done" | "cancelled";
export type PendingActionType = "send_email" | "create_event" | "create_tasks";
export type PendingActionOrigin = "agent" | "manual";
export type PendingActionStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "executed"
  | "refused"
  | "failed";

/** `SessionOut`. No refresh token: it arrives as an httpOnly cookie (D19). */
export interface Session {
  access_token: string;
  token_type: string;
  csrf_token: string;
  expires_in: number;
}

export interface User {
  id: Uuid;
  email: string;
  name: string;
  is_active: boolean;
}

export interface Workspace {
  id: Uuid;
  name: string;
  owner_id: Uuid;
  created_at: Timestamp;
}

export interface Member {
  id: Uuid;
  user: User;
  role: WorkspaceRole;
  joined_at: Timestamp;
}

export interface Invite {
  id: Uuid;
  email: string;
  role: WorkspaceRole;
  status: string;
  expires_at: Timestamp;
  /** Returned exactly once, by the call that created the invite. */
  token: string | null;
}

export interface Document {
  id: Uuid;
  name: string;
  mime_type: string;
  size_bytes: number;
  status: DocumentStatus;
  chunk_count: number;
  error_message: string | null;
  uploaded_by: Uuid;
  uploaded_at: Timestamp;
}

export interface DocumentUpload {
  document: Document;
  /** The content hash matched a document already here; nothing was stored. */
  deduplicated: boolean;
}

export interface Citation {
  id: Uuid;
  /** Null once the source chunk is gone. The snapshot below outlives it (D5). */
  chunk_id: Uuid | null;
  document_name: string;
  quoted_text: string;
  page_no: number | null;
  score: number;
}

export interface Message {
  id: Uuid;
  role: ChatRole;
  content: string;
  created_at: Timestamp;
  citations: Citation[];
}

export interface Thread {
  id: Uuid;
  title: string;
  created_at: Timestamp;
}

/**
 * What `citations` carries on the SSE stream.
 *
 * A narrower object than {@link Citation}: the stream sends the evidence
 * before the row ids would be useful to anyone, so it carries the snapshot
 * fields only.
 */
export interface StreamCitation {
  document_name: string;
  quoted_text: string;
  page_no: number | null;
  score: number;
}

/** One person, resolved server-side from `workspace_members` (D21). */
export interface ResolvedPerson {
  user_id: Uuid;
  name: string;
  email: string;
}

export interface EmailPayload {
  type: "send_email";
  recipients: ResolvedPerson[];
  subject: string;
  body: string;
}

export interface EventPayload {
  type: "create_event";
  title: string;
  description: string;
  start_time: Timestamp;
  end_time: Timestamp;
  guests: ResolvedPerson[];
}

export interface TasksPayload {
  type: "create_tasks";
  tasks: { title: string; description: string; assignee: ResolvedPerson | null }[];
}

export type ActionPayload = EmailPayload | EventPayload | TasksPayload;

export interface PendingAction {
  id: Uuid;
  thread_id: Uuid | null;
  origin: PendingActionOrigin;
  type: PendingActionType;
  payload: ActionPayload;
  /**
   * SHA-256 of the payload above, as the server computed it. Echoed back with
   * the decision so the server can prove the reviewer and the executor were
   * looking at the same object (D20).
   */
  payload_hash: string;
  status: PendingActionStatus;
  initiated_by: Uuid;
  decided_by: Uuid | null;
  decided_at: Timestamp | null;
  refusal_reason: string | null;
  created_at: Timestamp;
}

export interface AgentTurn {
  thread_id: Uuid;
  reply: string;
  pending_actions: PendingAction[];
  /** Never offered for approval: a name the resolver would not hand out. */
  refused_actions: PendingAction[];
}

export interface Task {
  id: Uuid;
  title: string;
  description: string;
  assigned_to: Uuid | null;
  source_message_id: Uuid | null;
  status: TaskStatus;
  due_date: Timestamp | null;
  created_by: Uuid | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}
