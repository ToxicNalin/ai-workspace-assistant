import { useState } from "react";
import type { FormEvent } from "react";

import * as api from "../../api/endpoints";
import type { Task, TaskStatus } from "../../api/types";
import { Badge, Banner, Empty, Field, Modal, Spinner } from "../../components/ui";
import { formatRelative } from "../../lib/format";
import { errorMessage, useAsync } from "../../lib/useAsync";
import { useWorkspace } from "../workspaces/WorkspaceContext";

const COLUMNS: { status: TaskStatus; label: string }[] = [
  { status: "todo", label: "To do" },
  { status: "in_progress", label: "In progress" },
  { status: "done", label: "Done" },
  { status: "cancelled", label: "Cancelled" },
];

export function TasksPage() {
  const { workspace, canWrite, memberById } = useWorkspace();
  const tasks = useAsync(() => api.listTasks(workspace.id), [workspace.id]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const list = tasks.data ?? [];

  async function move(task: Task, status: TaskStatus) {
    setError(null);
    // Optimistic: the board should move under the cursor, not a round trip
    // later. Reconciled by the reload below either way.
    tasks.set(list.map((item) => (item.id === task.id ? { ...item, status } : item)));
    try {
      // Only the field that changed. The server applies this with
      // `exclude_unset`, so sending the whole task back would re-assert an
      // assignee that somebody else may have changed in the meantime.
      await api.updateTask(workspace.id, task.id, { status });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      tasks.reload();
    }
  }

  async function remove(task: Task) {
    setError(null);
    try {
      await api.deleteTask(workspace.id, task.id);
      tasks.set(list.filter((item) => item.id !== task.id));
    } catch (caught) {
      setError(errorMessage(caught));
      tasks.reload();
    }
  }

  return (
    <section className="page">
      <header className="page__head">
        <div>
          <h1 className="page__title">Tasks</h1>
          <p className="page__lede">
            Created here, or in a batch by the agent once somebody approved it. An assignee
            has to be a member of this workspace — the server checks, so a task cannot be
            addressed to a stranger.
          </p>
        </div>
        {canWrite ? (
          <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
            New task
          </button>
        ) : null}
      </header>

      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      {tasks.error ? <Banner>{tasks.error}</Banner> : null}
      {tasks.loading && tasks.data === null ? <Spinner /> : null}

      {!tasks.loading && list.length === 0 ? (
        <Empty title="No tasks yet">
          <p>
            Create one, or ask the agent to — &ldquo;create tasks from the action points in
            the meeting notes&rdquo; — and approve what it proposes.
          </p>
        </Empty>
      ) : null}

      {list.length > 0 ? (
        <div className="board">
          {COLUMNS.map((column) => {
            const inColumn = list.filter((task) => task.status === column.status);
            return (
              <div key={column.status} className="board__column">
                <h2 className="board__title">
                  {column.label} <span className="board__count">{inColumn.length}</span>
                </h2>
                <div className="board__cards">
                  {inColumn.map((task) => {
                    const assignee =
                      task.assigned_to === null ? null : memberById.get(task.assigned_to);
                    return (
                      <article key={task.id} className="card">
                        <h3 className="card__title">{task.title}</h3>
                        {task.description !== "" ? (
                          <p className="card__desc">{task.description}</p>
                        ) : null}
                        <div className="card__meta">
                          {assignee !== null && assignee !== undefined ? (
                            <Badge tone="blue">{assignee.user.name}</Badge>
                          ) : (
                            <Badge>unassigned</Badge>
                          )}
                          {task.source_message_id !== null ? (
                            <Badge tone="violet" >from the agent</Badge>
                          ) : null}
                          <span className="card__when">{formatRelative(task.created_at)}</span>
                        </div>
                        {canWrite ? (
                          <div className="card__actions">
                            <select
                              value={task.status}
                              onChange={(event) => void move(task, event.target.value as TaskStatus)}
                              aria-label={`Status of ${task.title}`}
                            >
                              {COLUMNS.map((option) => (
                                <option key={option.status} value={option.status}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              className="button button--ghost button--small"
                              onClick={() => void remove(task)}
                            >
                              Delete
                            </button>
                          </div>
                        ) : null}
                      </article>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      <NewTaskModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={() => {
          setCreating(false);
          tasks.reload();
        }}
      />
    </section>
  );
}

function NewTaskModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { workspace, members } = useWorkspace();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignedTo, setAssignedTo] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createTask(workspace.id, {
        title: title.trim(),
        description: description.trim(),
        assigned_to: assignedTo === "" ? null : assignedTo,
        // A date input gives a bare date; the API wants a timestamp.
        due_date: dueDate === "" ? null : new Date(`${dueDate}T00:00:00`).toISOString(),
        status: "todo",
      });
      setTitle("");
      setDescription("");
      setAssignedTo("");
      setDueDate("");
      onCreated();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} title="New task" onClose={onClose}>
      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      <form onSubmit={submit} className="stack">
        <Field label="Title">
          <input value={title} onChange={(event) => setTitle(event.target.value)} required />
        </Field>
        <Field label="Description">
          <textarea
            rows={4}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <Field label="Assign to" hint="Members of this workspace only.">
          <select value={assignedTo} onChange={(event) => setAssignedTo(event.target.value)}>
            <option value="">Unassigned</option>
            {(members.data ?? []).map((member) => (
              <option key={member.user.id} value={member.user.id}>
                {member.user.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Due date">
          <input type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} />
        </Field>
        <div className="row row--end">
          <button type="button" className="button button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button button--primary"
            disabled={busy || title.trim() === ""}
          >
            {busy ? <Spinner label="Creating" /> : "Create task"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
