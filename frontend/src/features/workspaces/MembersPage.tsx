import { useState } from "react";
import type { FormEvent } from "react";

import * as api from "../../api/endpoints";
import type { Invite, WorkspaceRole } from "../../api/types";
import { Badge, Banner, Field, Modal, Spinner } from "../../components/ui";
import { formatDate } from "../../lib/format";
import { errorMessage } from "../../lib/useAsync";
import { useAuth } from "../auth/AuthContext";
import { useWorkspace } from "./WorkspaceContext";

const ROLES: { value: WorkspaceRole; description: string }[] = [
  { value: "admin", description: "Everything, including approving what the agent may do" },
  { value: "member", description: "Upload, chat, propose actions — but not approve them" },
  { value: "viewer", description: "Read only" },
];

export function MembersPage() {
  const { workspace, members, isAdmin } = useWorkspace();
  const { user } = useAuth();
  const [inviting, setInviting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const list = members.data ?? [];

  async function changeRole(userId: string, role: WorkspaceRole) {
    setError(null);
    try {
      await api.changeMemberRole(workspace.id, userId, role);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      members.reload();
    }
  }

  async function remove(userId: string) {
    const self = userId === user?.id;
    const message = self
      ? "Leave this workspace?"
      : "Remove this person from the workspace?";
    if (!window.confirm(message)) return;

    setError(null);
    try {
      await api.removeMember(workspace.id, userId);
      if (self) window.location.assign("/");
      else members.reload();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <section className="page">
      <header className="page__head">
        <div>
          <h1 className="page__title">Members</h1>
          <p className="page__lede">
            This list is the only source of email addresses in the application. When the
            agent names a person, the server resolves the name here — the model never
            supplies an address, which is what closes the exfiltration route a poisoned
            document would otherwise have.
          </p>
        </div>
        {isAdmin ? (
          <button type="button" className="button button--primary" onClick={() => setInviting(true)}>
            Invite someone
          </button>
        ) : null}
      </header>

      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      {members.error ? <Banner>{members.error}</Banner> : null}
      {members.loading && members.data === null ? <Spinner /> : null}

      {list.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Joined</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.map((member) => {
                const self = member.user.id === user?.id;
                return (
                  <tr key={member.id}>
                    <td>
                      {member.user.name}
                      {self ? <Badge>you</Badge> : null}
                    </td>
                    <td className="muted">{member.user.email}</td>
                    <td>
                      {isAdmin ? (
                        <select
                          value={member.role}
                          onChange={(event) =>
                            void changeRole(member.user.id, event.target.value as WorkspaceRole)
                          }
                          aria-label={`Role of ${member.user.name}`}
                        >
                          {ROLES.map((role) => (
                            <option key={role.value} value={role.value}>
                              {role.value}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Badge tone={member.role === "admin" ? "green" : "neutral"}>
                          {member.role}
                        </Badge>
                      )}
                    </td>
                    <td>{formatDate(member.joined_at)}</td>
                    <td className="table__actions">
                      {isAdmin || self ? (
                        <button
                          type="button"
                          className="button button--ghost button--small"
                          onClick={() => void remove(member.user.id)}
                        >
                          {self ? "Leave" : "Remove"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      <InviteDialog open={inviting} onClose={() => setInviting(false)} />
    </section>
  );
}

/**
 * Creating an invite, and showing the token exactly once.
 *
 * The server stores only a SHA-256 of it (SPEC-v2 D6), so this response is
 * genuinely the only time the raw token exists anywhere — which the dialogue
 * has to say, because a reviewer who closes it expecting to find the token in
 * a list later will not.
 */
function InviteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { workspace } = useWorkspace();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("member");
  const [created, setCreated] = useState<Invite | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function close() {
    setCreated(null);
    setEmail("");
    setError(null);
    onClose();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.createInvite(workspace.id, email.trim(), role));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  const link =
    created?.token != null ? `${window.location.origin}/join?token=${created.token}` : null;

  return (
    <Modal open={open} title="Invite someone" onClose={close}>
      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}

      {created !== null && link !== null ? (
        <div className="stack">
          <Banner tone="success">
            Invite created for <strong>{created.email}</strong>. It expires{" "}
            {formatDate(created.expires_at)}.
          </Banner>
          <Field
            label="Invite link"
            hint="Copy it now. Only a hash of this token is stored, so it cannot be shown again."
          >
            <input value={link} readOnly onFocus={(event) => event.target.select()} />
          </Field>
          <div className="row row--end">
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                // Rejects in a non-secure context, and where the browser
                // declines. The field beside it is selectable either way.
                navigator.clipboard.writeText(link).catch(() => undefined);
              }}
            >
              Copy link
            </button>
            <button type="button" className="button button--primary" onClick={close}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="stack">
          <Field label="Email">
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </Field>
          <Field label="Role">
            <select value={role} onChange={(event) => setRole(event.target.value as WorkspaceRole)}>
              {ROLES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.value} — {option.description}
                </option>
              ))}
            </select>
          </Field>
          <div className="row row--end">
            <button type="button" className="button button--ghost" onClick={close}>
              Cancel
            </button>
            <button
              type="submit"
              className="button button--primary"
              disabled={busy || email.trim() === ""}
            >
              {busy ? <Spinner label="Creating invite" /> : "Create invite"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
