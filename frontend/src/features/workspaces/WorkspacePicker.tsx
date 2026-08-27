import { useState } from "react";
import { Link } from "react-router-dom";

import type { Workspace } from "../../api/types";
import { Empty, Spinner } from "../../components/ui";
import { formatDate } from "../../lib/format";
import { useAuth } from "../auth/AuthContext";
import { CreateWorkspaceModal } from "./WorkspaceSwitcher";

/**
 * Where someone lands with no workspace to be inside yet.
 *
 * Also the first-run screen, which is why it explains what a workspace *is*
 * rather than just offering a button: the tenancy boundary is the thing the
 * rest of the application is organised around.
 */
export function WorkspacePicker({
  workspaces,
  loading,
  onCreated,
}: {
  workspaces: Workspace[];
  loading: boolean;
  onCreated: (workspace: Workspace) => void;
}) {
  const { user, signOut } = useAuth();
  const [creating, setCreating] = useState(false);

  return (
    <div className="picker">
      <div className="picker__inner">
        <header className="picker__head">
          <div>
            <h1 className="page__title">Your workspaces</h1>
            <p className="page__lede">Signed in as {user?.name}.</p>
          </div>
          <button type="button" className="button button--ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </header>

        {loading ? <Spinner /> : null}

        {!loading && workspaces.length === 0 ? (
          <Empty title="You are not in a workspace yet">
            <p>
              A workspace holds its own documents, conversations, tasks and members. Nothing
              in one is reachable from another — that separation is enforced on every query
              and proved by the test suite, not left to the interface.
            </p>
          </Empty>
        ) : null}

        <ul className="picker__list">
          {workspaces.map((workspace) => (
            <li key={workspace.id}>
              <Link className="picker__item" to={`/w/${workspace.id}/chat`}>
                <span className="picker__name">{workspace.name}</span>
                <span className="picker__when">created {formatDate(workspace.created_at)}</span>
              </Link>
            </li>
          ))}
        </ul>

        <div className="row">
          <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
            New workspace
          </button>
          <Link className="button button--ghost" to="/join">
            Redeem an invite
          </Link>
        </div>
      </div>

      <CreateWorkspaceModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(workspace) => {
          setCreating(false);
          onCreated(workspace);
        }}
      />
    </div>
  );
}
