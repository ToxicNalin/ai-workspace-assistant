import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";

import * as api from "../../api/endpoints";
import type { Member, Workspace, WorkspaceRole } from "../../api/types";
import { useAsync } from "../../lib/useAsync";
import type { Async } from "../../lib/useAsync";

interface WorkspaceValue {
  workspace: Workspace;
  /** The signed-in user's role *here*. Roles are per workspace, not global. */
  role: WorkspaceRole;
  members: Async<Member[]>;
  /** Members by user id, for turning an `assigned_to` into a name. */
  memberById: Map<string, Member>;
  isAdmin: boolean;
  canWrite: boolean;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

export function WorkspaceProvider({
  workspace,
  currentUserId,
  children,
}: {
  workspace: Workspace;
  currentUserId: string;
  children: ReactNode;
}) {
  const members = useAsync(() => api.listMembers(workspace.id), [workspace.id]);

  const value = useMemo<WorkspaceValue>(() => {
    const list = members.data ?? [];
    const memberById = new Map(list.map((member) => [member.user.id, member]));
    // Until the member list arrives, assume the least. Rendering an Approve
    // button that then 403s is worse than one that appears a moment late.
    const role = memberById.get(currentUserId)?.role ?? "viewer";

    return {
      workspace,
      role,
      members,
      memberById,
      isAdmin: role === "admin",
      canWrite: role === "admin" || role === "member",
    };
  }, [workspace, currentUserId, members]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (value === null) throw new Error("useWorkspace must be used inside a WorkspaceProvider");
  return value;
}
