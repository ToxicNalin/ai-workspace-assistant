import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import * as api from "../../api/endpoints";
import type { Workspace } from "../../api/types";
import { Banner, Field, Modal, Spinner } from "../../components/ui";
import { errorMessage } from "../../lib/useAsync";

export function WorkspaceSwitcher({
  workspaces,
  current,
  onCreated,
}: {
  workspaces: Workspace[];
  current: Workspace;
  onCreated: (workspace: Workspace) => void;
}) {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);

  return (
    <div className="switcher">
      <label className="switcher__label" htmlFor="workspace-select">
        Workspace
      </label>
      <div className="switcher__row">
        <select
          id="workspace-select"
          value={current.id}
          onChange={(event) => {
            // Keep the section the person is looking at. Switching workspace
            // to compare two documents lists should not drop you on chat.
            const section = window.location.pathname.split("/")[3] ?? "chat";
            navigate(`/w/${event.target.value}/${section}`);
          }}
        >
          {workspaces.map((workspace) => (
            <option key={workspace.id} value={workspace.id}>
              {workspace.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="button button--ghost button--square"
          onClick={() => setCreating(true)}
          title="New workspace"
          aria-label="New workspace"
        >
          +
        </button>
      </div>

      <CreateWorkspaceModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(workspace) => {
          setCreating(false);
          onCreated(workspace);
          navigate(`/w/${workspace.id}/documents`);
        }}
      />
    </div>
  );
}

export function CreateWorkspaceModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (workspace: Workspace) => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const workspace = await api.createWorkspace(name.trim());
      setName("");
      onCreated(workspace);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} title="New workspace" onClose={onClose}>
      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      <form onSubmit={submit} className="stack">
        <Field
          label="Name"
          hint="You become its admin. Documents, chats and tasks live inside one workspace and are invisible from any other."
        >
          <input value={name} onChange={(event) => setName(event.target.value)} required />
        </Field>
        <div className="row row--end">
          <button type="button" className="button button--ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button button--primary"
            disabled={busy || name.trim() === ""}
          >
            {busy ? <Spinner label="Creating" /> : "Create workspace"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
