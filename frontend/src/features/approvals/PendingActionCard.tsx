import { useState } from "react";

import type { Decision } from "../../api/endpoints";
import type {
  ActionPayload,
  EmailPayload,
  EventPayload,
  PendingAction,
  PendingActionStatus,
  ResolvedPerson,
  TasksPayload,
} from "../../api/types";
import { Badge, Banner, Spinner } from "../../components/ui";
import { formatDateTime, formatRelative, shortHash } from "../../lib/format";
import { errorMessage } from "../../lib/useAsync";

const TYPE_LABEL: Record<PendingAction["type"], string> = {
  send_email: "Send an email",
  create_event: "Create a calendar event",
  create_tasks: "Create tasks",
};

const STATUS_TONE: Record<PendingActionStatus, "neutral" | "green" | "amber" | "red" | "blue"> = {
  pending: "amber",
  approved: "blue",
  executed: "green",
  rejected: "neutral",
  refused: "red",
  failed: "red",
};

/**
 * One proposed action, and the decision a human owes it.
 *
 * Two things on this card are load-bearing rather than decorative.
 *
 * The **payload hash** is shown because the approval is bound to it
 * (SPEC-v2 D20): the reviewer's decision carries the hash of exactly what
 * this card rendered, and the server re-hashes what it holds before executing
 * and refuses on a mismatch. Displaying it is what makes that visible rather
 * than merely true.
 *
 * The **recipients are not editable**. An edit exists so a subject line can be
 * fixed, not so an action can be redirected — the server enforces that too
 * (`_validate_edit`), and an interface that offered the field and then refused
 * the save would be teaching the wrong thing about why it is refused.
 */
export function PendingActionCard({
  action,
  canDecide,
  onDecide,
}: {
  action: PendingAction;
  canDecide: boolean;
  onDecide: (decision: Decision) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ActionPayload>(action.payload);
  const [busy, setBusy] = useState<Decision["decision"] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decided = action.status !== "pending";
  const editable = action.type !== "create_tasks";

  async function decide(decision: Decision["decision"]) {
    setBusy(decision);
    setError(null);
    try {
      const body: Decision = { decision, payload_hash: action.payload_hash };
      if (decision === "edit") body.edited_payload = draft as unknown as Record<string, unknown>;
      await onDecide(body);
      setEditing(false);
    } catch (caught) {
      // The interesting refusals land here: a payload that changed since it
      // was displayed, or an edit that tried to redirect the action.
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={`action action--${action.status}`}>
      <header className="action__head">
        <div>
          <h3 className="action__title">{TYPE_LABEL[action.type]}</h3>
          <p className="action__meta">
            proposed by the {action.origin === "agent" ? "agent" : "a person"} ·{" "}
            {formatRelative(action.created_at)}
          </p>
        </div>
        <Badge tone={STATUS_TONE[action.status]}>{action.status}</Badge>
      </header>

      {action.status === "refused" ? (
        <Banner tone="warning">
          <strong>Never offered for approval.</strong>{" "}
          {action.refusal_reason ??
            "The model named someone who is not a member of this workspace."}{" "}
          Recipients are resolved server-side from the member list, so there was no address
          for this to be sent to.
        </Banner>
      ) : null}

      {action.status === "failed" ? (
        <Banner tone="error">
          <strong>Approved, but it did not happen.</strong>{" "}
          {action.refusal_reason ?? "The provider refused it."} Nothing was half-done — the
          side effect was rolled back.
        </Banner>
      ) : null}

      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}

      <div className="action__body">
        <PayloadView
          payload={editing ? draft : action.payload}
          editing={editing}
          onChange={setDraft}
        />
      </div>

      <footer className="action__foot">
        <p className="action__hash" title={action.payload_hash}>
          <span className="action__hash-label">bound to payload</span>
          <code>{shortHash(action.payload_hash)}</code>
          <span className="action__hash-note">
            Your decision carries this hash. The server re-hashes what it holds before acting
            and refuses if they differ.
          </span>
        </p>

        {decided ? (
          <p className="action__decided">
            {action.decided_at !== null ? `Decided ${formatDateTime(action.decided_at)}` : null}
          </p>
        ) : canDecide ? (
          <div className="action__buttons">
            {editing ? (
              <>
                <button
                  type="button"
                  className="button button--ghost"
                  onClick={() => {
                    setDraft(action.payload);
                    setEditing(false);
                  }}
                  disabled={busy !== null}
                >
                  Cancel edit
                </button>
                <button
                  type="button"
                  className="button button--primary"
                  onClick={() => void decide("edit")}
                  disabled={busy !== null}
                >
                  {busy === "edit" ? <Spinner label="Saving" /> : "Approve edited"}
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  className="button button--danger"
                  onClick={() => void decide("reject")}
                  disabled={busy !== null}
                >
                  {busy === "reject" ? <Spinner label="Rejecting" /> : "Reject"}
                </button>
                {editable ? (
                  <button
                    type="button"
                    className="button button--ghost"
                    onClick={() => setEditing(true)}
                    disabled={busy !== null}
                  >
                    Edit
                  </button>
                ) : null}
                <button
                  type="button"
                  className="button button--primary"
                  onClick={() => void decide("approve")}
                  disabled={busy !== null}
                >
                  {busy === "approve" ? <Spinner label="Approving" /> : "Approve"}
                </button>
              </>
            )}
          </div>
        ) : (
          <p className="action__decided">Only an admin can decide this.</p>
        )}
      </footer>
    </article>
  );
}

function PayloadView({
  payload,
  editing,
  onChange,
}: {
  payload: ActionPayload;
  editing: boolean;
  onChange: (next: ActionPayload) => void;
}) {
  if (payload.type === "send_email") {
    return <EmailView payload={payload} editing={editing} onChange={onChange} />;
  }
  if (payload.type === "create_event") {
    return <EventView payload={payload} editing={editing} onChange={onChange} />;
  }
  return <TasksView payload={payload} />;
}

/** Resolved from `workspace_members` by the server. Never model output (D21). */
function People({ label, people }: { label: string; people: ResolvedPerson[] }) {
  return (
    <div className="people">
      <span className="people__label">{label}</span>
      <div className="people__list">
        {people.map((person) => (
          <span key={person.user_id} className="person" title="Resolved from workspace members">
            <strong>{person.name}</strong>
            <span className="person__email">{person.email}</span>
          </span>
        ))}
      </div>
      <span className="people__locked">
        Not editable — an edit may change what is said, never who it goes to.
      </span>
    </div>
  );
}

function EmailView({
  payload,
  editing,
  onChange,
}: {
  payload: EmailPayload;
  editing: boolean;
  onChange: (next: ActionPayload) => void;
}) {
  return (
    <>
      <People label="To" people={payload.recipients} />
      <dl className="kv">
        <dt>Subject</dt>
        <dd>
          {editing ? (
            <input
              value={payload.subject}
              onChange={(event) => onChange({ ...payload, subject: event.target.value })}
            />
          ) : (
            payload.subject
          )}
        </dd>
        <dt>Body</dt>
        <dd>
          {editing ? (
            <textarea
              rows={8}
              value={payload.body}
              onChange={(event) => onChange({ ...payload, body: event.target.value })}
            />
          ) : (
            <pre className="prose">{payload.body}</pre>
          )}
        </dd>
      </dl>
    </>
  );
}

function EventView({
  payload,
  editing,
  onChange,
}: {
  payload: EventPayload;
  editing: boolean;
  onChange: (next: ActionPayload) => void;
}) {
  return (
    <>
      <People label="Guests" people={payload.guests} />
      <dl className="kv">
        <dt>Title</dt>
        <dd>
          {editing ? (
            <input
              value={payload.title}
              onChange={(event) => onChange({ ...payload, title: event.target.value })}
            />
          ) : (
            payload.title
          )}
        </dd>
        <dt>When</dt>
        <dd>
          {formatDateTime(payload.start_time)} — {formatDateTime(payload.end_time)}
        </dd>
        {payload.description !== "" || editing ? (
          <>
            <dt>Description</dt>
            <dd>
              {editing ? (
                <textarea
                  rows={5}
                  value={payload.description}
                  onChange={(event) => onChange({ ...payload, description: event.target.value })}
                />
              ) : (
                <pre className="prose">{payload.description}</pre>
              )}
            </dd>
          </>
        ) : null}
      </dl>
    </>
  );
}

function TasksView({ payload }: { payload: TasksPayload }) {
  return (
    <>
      <p className="action__note">
        Approve or reject only — a batch of tasks has no wording to fix, so the server does
        not offer an edit for it.
      </p>
      <ul className="task-preview">
        {payload.tasks.map((task, index) => (
          <li key={index}>
            <strong>{task.title}</strong>
            {task.assignee !== null ? (
              <span className="task-preview__who"> → {task.assignee.name}</span>
            ) : (
              <span className="task-preview__who task-preview__who--none"> → unassigned</span>
            )}
            {task.description !== "" ? <p className="task-preview__desc">{task.description}</p> : null}
          </li>
        ))}
      </ul>
    </>
  );
}
