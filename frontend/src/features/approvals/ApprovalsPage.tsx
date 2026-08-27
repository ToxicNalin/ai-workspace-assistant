import { useState } from "react";

import * as api from "../../api/endpoints";
import type { Decision } from "../../api/endpoints";
import { Banner, Empty, Spinner } from "../../components/ui";
import { errorMessage, useAsync } from "../../lib/useAsync";
import { useWorkspace } from "../workspaces/WorkspaceContext";
import { PendingActionCard } from "./PendingActionCard";

/**
 * The queue of things the agent wants to do and has not been allowed to.
 *
 * This screen is the project's centrepiece: an action reaching it means the
 * graph interrupted before the tool ran, and it stays here until an admin
 * decides. Approving is the only path by which anything this application does
 * reaches the outside world.
 */
export function ApprovalsPage() {
  const { workspace, isAdmin } = useWorkspace();
  const [showDecided, setShowDecided] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const actions = useAsync(
    () => api.listPendingActions(workspace.id, showDecided),
    [workspace.id, showDecided],
  );

  async function decide(actionId: string, decision: Decision) {
    setError(null);
    try {
      await api.decideAction(workspace.id, actionId, decision);
    } catch (caught) {
      // Shown on the page as well as on the card. A hash mismatch or an
      // already-decided conflict is the interesting outcome here, not a
      // footnote — and it means the list on screen is stale.
      setError(errorMessage(caught));
      throw caught;
    } finally {
      actions.reload();
    }
  }

  const list = actions.data ?? [];

  return (
    <section className="page">
      <header className="page__head">
        <div>
          <h1 className="page__title">Approvals</h1>
          <p className="page__lede">
            No output from the model reaches an external service directly. Every
            side-effecting action stops here, and the approval is bound to a hash of the
            exact payload shown below.
          </p>
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showDecided}
            onChange={(event) => setShowDecided(event.target.checked)}
          />
          Include decided
        </label>
      </header>

      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      {actions.error ? <Banner>{actions.error}</Banner> : null}

      {!isAdmin ? (
        <Banner tone="info">
          You can see what is waiting, but only an admin of this workspace can approve it.
          Approval is the moment the agent is allowed to touch anything outside this
          application.
        </Banner>
      ) : null}

      {actions.loading && actions.data === null ? <Spinner /> : null}

      {!actions.loading && list.length === 0 ? (
        <Empty title={showDecided ? "Nothing has been proposed yet" : "Nothing is waiting"}>
          <p>
            Ask the agent to do something on the Chat screen in <strong>Act</strong> mode —
            &ldquo;email the team a summary of the handbook&rdquo; — and whatever it wants to
            do will appear here for a decision.
          </p>
        </Empty>
      ) : null}

      <div className="action-list">
        {list.map((action) => (
          <PendingActionCard
            key={action.id}
            action={action}
            canDecide={isAdmin && action.status === "pending"}
            onDecide={(decision) => decide(action.id, decision)}
          />
        ))}
      </div>
    </section>
  );
}
