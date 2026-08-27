import { useCallback, useState } from "react";
import type { ReactElement } from "react";
import {
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useOutletContext,
  useParams,
} from "react-router-dom";

import * as api from "./api/endpoints";
import type { Workspace } from "./api/types";
import { Layout } from "./components/Layout";
import { Banner, Empty, Spinner } from "./components/ui";
import { ApprovalsPage } from "./features/approvals/ApprovalsPage";
import { AcceptInvitePage } from "./features/auth/AcceptInvitePage";
import { useAuth } from "./features/auth/AuthContext";
import { LoginPage } from "./features/auth/LoginPage";
import { RegisterPage } from "./features/auth/RegisterPage";
import { ChatPage } from "./features/chat/ChatPage";
import { DocumentsPage } from "./features/documents/DocumentsPage";
import { TasksPage } from "./features/tasks/TasksPage";
import { MembersPage } from "./features/workspaces/MembersPage";
import { WorkspaceProvider } from "./features/workspaces/WorkspaceContext";
import { WorkspacePicker } from "./features/workspaces/WorkspacePicker";
import { useAsync } from "./lib/useAsync";

export interface WorkspaceScopeValue {
  workspaces: Workspace[];
  loading: boolean;
  error: string | null;
  add: (workspace: Workspace) => void;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<AnonymousOnly element={<LoginPage />} />} />
      <Route path="/register" element={<AnonymousOnly element={<RegisterPage />} />} />
      {/* Not wrapped: it has something useful to say to a signed-out visitor
          holding an invite link, rather than bouncing them to /login and
          losing the token on the way. */}
      <Route path="/join" element={<AcceptInvitePage />} />

      <Route element={<RequireAuth />}>
        <Route path="/" element={<PickerRoute />} />
        <Route path="/w/:workspaceId" element={<WorkspaceShell />}>
          <Route index element={<Navigate to="chat" replace />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="members" element={<MembersPage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function AnonymousOnly({ element }: { element: ReactElement }) {
  const { status } = useAuth();
  if (status === "restoring") return <FullPageSpinner />;
  if (status === "authenticated") return <Navigate to="/" replace />;
  return element;
}

/**
 * The gate, and the reason it has three states rather than two.
 *
 * On a reload the access token is gone — it lives in a module variable by
 * design (SPEC-v2 D19) — and the refresh cookie is still being exchanged for a
 * new one. Treating "not yet authenticated" as "anonymous" would bounce a
 * signed-in person to the login page for the length of that round trip and
 * then bounce them back.
 */
function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "restoring") return <FullPageSpinner />;
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: location }} />;
  return <WorkspaceScope />;
}

/**
 * Loads the workspace list once for everything below it.
 *
 * Both the picker and the shell need it, and loading it in each would mean two
 * requests and two chances for them to disagree about which workspaces exist.
 */
function WorkspaceScope() {
  const workspaces = useAsync(() => api.listWorkspaces(), []);
  const [extra, setExtra] = useState<Workspace[]>([]);

  // Held alongside the fetched list rather than merged into it, so a workspace
  // created a moment ago is selectable without waiting for a refetch.
  const add = useCallback((workspace: Workspace) => {
    setExtra((current) => [...current, workspace]);
  }, []);

  const known = workspaces.data ?? [];
  const merged = [...known, ...extra.filter((one) => !known.some((other) => other.id === one.id))];

  const value: WorkspaceScopeValue = {
    workspaces: merged,
    loading: workspaces.loading && workspaces.data === null,
    error: workspaces.error,
    add,
  };

  return <Outlet context={value} />;
}

function useWorkspaceScope(): WorkspaceScopeValue {
  return useOutletContext<WorkspaceScopeValue>();
}

function PickerRoute() {
  const scope = useWorkspaceScope();
  return (
    <>
      {scope.error !== null ? <Banner>{scope.error}</Banner> : null}
      <WorkspacePicker
        workspaces={scope.workspaces}
        loading={scope.loading}
        onCreated={scope.add}
      />
    </>
  );
}

function WorkspaceShell() {
  const { workspaceId } = useParams();
  const scope = useWorkspaceScope();
  const { user } = useAuth();
  const location = useLocation();

  const workspace = scope.workspaces.find((candidate) => candidate.id === workspaceId);

  // Re-read on every navigation inside the workspace. Cheap, and it means the
  // sidebar badge is right when you arrive at Approvals rather than one visit
  // behind. A failure here is swallowed: a count is not worth an error screen.
  const pending = useAsync(
    () =>
      workspace === undefined
        ? Promise.resolve([])
        : api.listPendingActions(workspace.id).catch(() => []),
    [workspace?.id, location.pathname],
  );

  if (scope.loading) return <FullPageSpinner />;

  if (workspace === undefined || user === null) {
    return (
      <div className="picker">
        <div className="picker__inner">
          <Empty title="No such workspace">
            <p>
              It does not exist, or you are not a member of it — which are deliberately
              indistinguishable from out here. A resource in a workspace you cannot reach
              returns 404 rather than 403, because a 403 would confirm it exists.
            </p>
            <Link className="button button--primary" to="/">
              Your workspaces
            </Link>
          </Empty>
        </div>
      </div>
    );
  }

  return (
    <WorkspaceProvider workspace={workspace} currentUserId={user.id}>
      <Layout
        workspaces={scope.workspaces}
        onWorkspaceCreated={scope.add}
        pendingCount={(pending.data ?? []).length}
      />
    </WorkspaceProvider>
  );
}

function FullPageSpinner() {
  return (
    <div className="full-page">
      <Spinner label="Loading" />
    </div>
  );
}
