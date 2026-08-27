import { NavLink, Outlet } from "react-router-dom";

import type { Workspace } from "../api/types";
import { useAuth } from "../features/auth/AuthContext";
import { useWorkspace } from "../features/workspaces/WorkspaceContext";
import { WorkspaceSwitcher } from "../features/workspaces/WorkspaceSwitcher";
import { Badge } from "./ui";

const SECTIONS = [
  { path: "chat", label: "Chat", hint: "Ask questions, or ask the agent to do something" },
  { path: "approvals", label: "Approvals", hint: "Decide what the agent may do" },
  { path: "documents", label: "Documents", hint: "Upload and manage sources" },
  { path: "tasks", label: "Tasks", hint: "What the workspace is working on" },
  { path: "members", label: "Members", hint: "Who is in this workspace" },
] as const;

export function Layout({
  workspaces,
  onWorkspaceCreated,
  pendingCount,
}: {
  workspaces: Workspace[];
  onWorkspaceCreated: (workspace: Workspace) => void;
  pendingCount: number;
}) {
  const { workspace, role, members } = useWorkspace();
  const { user, signOut } = useAuth();

  // The context assumes the least until the member list arrives, so that no
  // screen offers an action the server would then refuse. Saying "viewer" out
  // loud on that assumption would be a different thing — it would be wrong on
  // screen for anyone who is not one.
  const standing = members.data === null ? workspace.name : `${role} in ${workspace.name}`;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <span className="sidebar__mark" aria-hidden="true" />
          <span className="sidebar__name">Workspace Assistant</span>
        </div>

        <WorkspaceSwitcher
          workspaces={workspaces}
          current={workspace}
          onCreated={onWorkspaceCreated}
        />

        <nav className="nav" aria-label="Workspace sections">
          {SECTIONS.map((section) => (
            <NavLink
              key={section.path}
              to={`/w/${workspace.id}/${section.path}`}
              className={({ isActive }) => `nav__item${isActive ? " nav__item--active" : ""}`}
              title={section.hint}
            >
              <span>{section.label}</span>
              {section.path === "approvals" && pendingCount > 0 ? (
                <Badge tone="amber">{pendingCount}</Badge>
              ) : null}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar__foot">
          <div className="sidebar__user">
            <span className="sidebar__user-name">{user?.name}</span>
            <span className="sidebar__user-role">{standing}</span>
          </div>
          <button type="button" className="button button--ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
