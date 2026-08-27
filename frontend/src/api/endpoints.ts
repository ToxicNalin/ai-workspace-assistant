/**
 * Every call this application makes, named after the thing it does.
 *
 * Components import from here, never from `client.ts` directly, so there is
 * one list of what the frontend actually uses and adding a call is a visible
 * change rather than a string appearing in a component.
 *
 * It is the calls this application makes, not every route the API serves.
 * `POST /chat/query` has no wrapper, for instance: it answers the same
 * question as `/chat/stream` in one blocking response, and the interface only
 * ever wants the streamed one. A wrapper nothing calls is a claim about the
 * frontend that is not true.
 */

import { clearSession, request, requestRaw, setSession } from "./client";
import type {
  AgentTurn,
  Document,
  DocumentUpload,
  Invite,
  Member,
  Message,
  PendingAction,
  Session,
  Task,
  TaskStatus,
  Thread,
  User,
  Uuid,
  Workspace,
  WorkspaceRole,
} from "./types";

// --- auth ----------------------------------------------------------------

export async function login(email: string, password: string): Promise<Session> {
  const session = await request<Session>("/auth/login", {
    method: "POST",
    body: { email, password },
    anonymous: true,
  });
  setSession(session);
  return session;
}

export function register(email: string, password: string, name: string): Promise<User> {
  return request<User>("/auth/register", {
    method: "POST",
    body: { email, password, name },
    anonymous: true,
  });
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" });
  } finally {
    // Even if the call failed. The local session is the thing the person
    // asked to be rid of, and leaving it behind because the network was down
    // would be the wrong way round.
    clearSession();
  }
}

export function me(): Promise<User> {
  return request<User>("/auth/me");
}

// --- workspaces ----------------------------------------------------------

export function listWorkspaces(): Promise<Workspace[]> {
  return request<Workspace[]>("/workspaces");
}

export function createWorkspace(name: string): Promise<Workspace> {
  return request<Workspace>("/workspaces", { method: "POST", body: { name } });
}

export function listMembers(workspaceId: Uuid): Promise<Member[]> {
  return request<Member[]>(`/workspaces/${workspaceId}/members`);
}

export function changeMemberRole(
  workspaceId: Uuid,
  userId: Uuid,
  role: WorkspaceRole,
): Promise<Member> {
  return request<Member>(`/workspaces/${workspaceId}/members/${userId}`, {
    method: "PATCH",
    body: { role },
  });
}

export function removeMember(workspaceId: Uuid, userId: Uuid): Promise<void> {
  return request<void>(`/workspaces/${workspaceId}/members/${userId}`, { method: "DELETE" });
}

export function createInvite(
  workspaceId: Uuid,
  email: string,
  role: WorkspaceRole,
): Promise<Invite> {
  return request<Invite>(`/workspaces/${workspaceId}/invite`, {
    method: "POST",
    body: { email, role },
  });
}

export function acceptInvite(token: string): Promise<Member> {
  return request<Member>("/workspaces/join", { method: "POST", body: { token } });
}

// --- documents -----------------------------------------------------------

export function listDocuments(workspaceId: Uuid): Promise<Document[]> {
  return request<Document[]>(`/workspaces/${workspaceId}/documents`);
}

export function uploadDocument(workspaceId: Uuid, file: File): Promise<DocumentUpload> {
  const form = new FormData();
  form.append("file", file);
  return request<DocumentUpload>(`/workspaces/${workspaceId}/documents/upload`, {
    method: "POST",
    body: form,
  });
}

export function deleteDocument(workspaceId: Uuid, documentId: Uuid): Promise<void> {
  return request<void>(`/workspaces/${workspaceId}/documents/${documentId}`, {
    method: "DELETE",
  });
}

// --- chat ----------------------------------------------------------------

export function listThreads(workspaceId: Uuid): Promise<Thread[]> {
  return request<Thread[]>(`/workspaces/${workspaceId}/chat/threads`);
}

export function threadHistory(workspaceId: Uuid, threadId: Uuid): Promise<Message[]> {
  return request<Message[]>(`/workspaces/${workspaceId}/chat/threads/${threadId}/history`);
}

/**
 * Opens the SSE response for a question.
 *
 * `requestRaw`, not `request`: the caller consumes the body as a stream. The
 * browser's own EventSource is unusable here because it cannot set an
 * Authorization header, and putting the access token in the query string
 * would write it into server logs and browser history.
 */
export function openChatStream(
  workspaceId: Uuid,
  question: string,
  threadId: Uuid | null,
  signal: AbortSignal,
): Promise<Response> {
  return requestRaw(`/workspaces/${workspaceId}/chat/stream`, {
    query: { question, thread_id: threadId },
    signal,
  });
}

// --- the agent and its approval gate -------------------------------------

export function runAgent(
  workspaceId: Uuid,
  message: string,
  threadId: Uuid | null,
): Promise<AgentTurn> {
  return request<AgentTurn>(`/workspaces/${workspaceId}/agent`, {
    method: "POST",
    body: { message, thread_id: threadId },
  });
}

export function listPendingActions(
  workspaceId: Uuid,
  includeDecided = false,
): Promise<PendingAction[]> {
  return request<PendingAction[]>(`/workspaces/${workspaceId}/pending-actions`, {
    query: { include_decided: includeDecided },
  });
}

export interface Decision {
  decision: "approve" | "edit" | "reject";
  /** The hash of the payload the reviewer was shown. Required on all three. */
  payload_hash: string;
  edited_payload?: Record<string, unknown>;
}

export function decideAction(
  workspaceId: Uuid,
  actionId: Uuid,
  decision: Decision,
): Promise<PendingAction> {
  return request<PendingAction>(
    `/workspaces/${workspaceId}/pending-actions/${actionId}/decide`,
    { method: "POST", body: decision },
  );
}

// --- tasks ---------------------------------------------------------------

export function listTasks(workspaceId: Uuid): Promise<Task[]> {
  return request<Task[]>(`/workspaces/${workspaceId}/tasks`);
}

export interface NewTask {
  title: string;
  description: string;
  assigned_to: Uuid | null;
  due_date: string | null;
  status: TaskStatus;
}

export function createTask(workspaceId: Uuid, task: NewTask): Promise<Task> {
  return request<Task>(`/workspaces/${workspaceId}/tasks`, { method: "POST", body: task });
}

/**
 * A partial update, where absence is meaningful.
 *
 * The server applies this with `exclude_unset`, so `assigned_to: null`
 * unassigns the task while omitting the key leaves whoever is on it. Passing
 * a whole task object back would silently reassign things.
 */
export type TaskPatch = Partial<Pick<Task, "title" | "description" | "assigned_to" | "status">>;

export function updateTask(workspaceId: Uuid, taskId: Uuid, patch: TaskPatch): Promise<Task> {
  return request<Task>(`/workspaces/${workspaceId}/tasks/${taskId}`, {
    method: "PATCH",
    body: patch,
  });
}

export function deleteTask(workspaceId: Uuid, taskId: Uuid): Promise<void> {
  return request<void>(`/workspaces/${workspaceId}/tasks/${taskId}`, { method: "DELETE" });
}
