import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";

import * as api from "../../api/endpoints";
import type { Message, PendingAction, Uuid } from "../../api/types";
import { Badge, Banner, Empty, Spinner } from "../../components/ui";
import { formatRelative } from "../../lib/format";
import { errorMessage, useAsync } from "../../lib/useAsync";
import { useWorkspace } from "../workspaces/WorkspaceContext";
import { Citations } from "./Citations";
import { useChatStream } from "./useChatStream";

type Mode = "ask" | "act";

/**
 * One conversation surface, two things you can do with it.
 *
 * **Ask** streams a retrieval-augmented answer with citations and touches
 * nothing. **Act** runs the agent, which may propose an action — and a
 * proposal is all it can produce here, because every side-effecting tool
 * interrupts for a human (SPEC-v2 D20). Both write to the same `chat_threads`,
 * so they share one thread list rather than splitting a conversation across
 * two screens.
 *
 * Two thread ids, deliberately. `opened` is what the history below was loaded
 * for; `active` is where the conversation is. They differ for exactly as long
 * as a new thread is being streamed into, and keeping them apart is what stops
 * the `meta` event — which arrives before the server has committed anything —
 * from triggering a reload that would make the question vanish mid-answer and
 * reappear at the end.
 */
export function ChatPage() {
  const { workspace, canWrite } = useWorkspace();
  const [mode, setMode] = useState<Mode>("ask");
  const [opened, setOpened] = useState<Uuid | null>(null);
  const [active, setActive] = useState<Uuid | null>(null);
  const [draft, setDraft] = useState("");
  /** The question, shown at once rather than waiting to be echoed back. */
  const [asking, setAsking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [proposed, setProposed] = useState<PendingAction[]>([]);
  const [refused, setRefused] = useState<PendingAction[]>([]);
  const [thinking, setThinking] = useState(false);

  const threads = useAsync(() => api.listThreads(workspace.id), [workspace.id]);
  const history = useAsync<Message[]>(
    () => (opened === null ? Promise.resolve([]) : api.threadHistory(workspace.id, opened)),
    [workspace.id, opened],
  );
  const stream = useChatStream(workspace.id);

  // The server's copy of the turn has landed, so the local stand-ins can go.
  // Doing this here rather than at `done` means there is never a frame with
  // neither: the optimistic pair survives until the real pair replaces it.
  const clearStream = stream.clear;
  useEffect(() => {
    if (history.data === null) return;
    setAsking(null);
    clearStream();
  }, [history.data, clearStream]);

  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [history.data, asking, stream.answer?.text, proposed]);

  function reset() {
    stream.cancel();
    stream.clear();
    setAsking(null);
    setProposed([]);
    setRefused([]);
    setError(null);
  }

  function startNewThread() {
    reset();
    setOpened(null);
    setActive(null);
  }

  function openThread(id: Uuid) {
    reset();
    setOpened(id);
    setActive(id);
  }

  /** Both paths end in a load from the server, once everything is committed. */
  function settle(threadId: Uuid) {
    setActive(threadId);
    threads.reload();
    if (opened === null) setOpened(threadId);
    else history.reload();
  }

  async function send() {
    const text = draft.trim();
    if (text === "") return;

    setDraft("");
    setError(null);
    setProposed([]);
    setRefused([]);
    setAsking(text);

    if (mode === "ask") {
      await stream.ask(text, active, {
        onThread: setActive,
        onFinished: settle,
      });
      return;
    }

    setThinking(true);
    try {
      const turn = await api.runAgent(workspace.id, text, active);
      setProposed(turn.pending_actions);
      setRefused(turn.refused_actions);
      settle(turn.thread_id);
    } catch (caught) {
      setError(errorMessage(caught));
      setAsking(null);
    } finally {
      setThinking(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  const busy = stream.streaming || thinking;
  const messages = history.data ?? [];
  const nothingYet =
    messages.length === 0 && asking === null && stream.answer === null && !busy;

  return (
    <div className="chat">
      <div className="chat__threads">
        <div className="chat__threads-head">
          <h2 className="section__title">Conversations</h2>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={startNewThread}
          >
            New
          </button>
        </div>
        {threads.loading && threads.data === null ? <Spinner /> : null}
        <ul className="thread-list">
          {(threads.data ?? []).map((thread) => (
            <li key={thread.id}>
              <button
                type="button"
                className={`thread${thread.id === active ? " thread--active" : ""}`}
                onClick={() => openThread(thread.id)}
              >
                <span className="thread__title">{thread.title}</span>
                <span className="thread__when">{formatRelative(thread.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="chat__main">
        <header className="chat__head">
          <div className="modes" role="group" aria-label="What to do with this message">
            <button
              type="button"
              className={`mode${mode === "ask" ? " mode--active" : ""}`}
              onClick={() => setMode("ask")}
            >
              Ask
              <small>Answer from documents, with citations</small>
            </button>
            <button
              type="button"
              className={`mode${mode === "act" ? " mode--active" : ""}`}
              onClick={() => setMode("act")}
            >
              Act
              <small>Let the agent propose an action</small>
            </button>
          </div>
        </header>

        <div className="chat__scroll" ref={scroller}>
          {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
          {stream.error ? <Banner>{stream.error}</Banner> : null}
          {history.error ? <Banner>{history.error}</Banner> : null}

          {nothingYet ? (
            <Empty title="Nothing here yet">
              <p>
                In <strong>Ask</strong>, questions are answered from this workspace&rsquo;s
                documents and every claim carries a citation you can open.
              </p>
              <p>
                In <strong>Act</strong>, the agent may propose sending an email, creating a
                calendar event or creating tasks — and proposing is all it can do. Nothing
                leaves this application until an admin approves it on the{" "}
                <Link to={`/w/${workspace.id}/approvals`}>Approvals</Link> screen.
              </p>
            </Empty>
          ) : null}

          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}

          {asking !== null ? (
            <article className="bubble bubble--user">
              <div className="bubble__body">{asking}</div>
            </article>
          ) : null}

          {stream.answer !== null ? (
            <article className="bubble bubble--assistant">
              <div className="bubble__body">
                {stream.answer.text}
                {!stream.answer.done ? <span className="caret" aria-hidden="true" /> : null}
              </div>
              <Citations citations={stream.answer.citations} />
            </article>
          ) : null}

          {thinking ? (
            <article className="bubble bubble--assistant">
              <div className="bubble__body bubble__body--muted">
                <Spinner label="The agent is working" /> Working…
              </div>
            </article>
          ) : null}

          {refused.length > 0 ? (
            <Banner tone="warning">
              <strong>
                {refused.length} action{refused.length === 1 ? "" : "s"} refused before anyone
                saw {refused.length === 1 ? "it" : "them"}.
              </strong>{" "}
              The agent named someone who is not a member of this workspace. Recipients are
              resolved server-side from the member list, so there was no address to offer for
              approval.
              <ul className="reasons">
                {refused.map((action) => (
                  <li key={action.id}>{action.refusal_reason ?? "Unresolvable recipient."}</li>
                ))}
              </ul>
            </Banner>
          ) : null}

          {proposed.length > 0 ? (
            <Banner tone="info">
              <strong>
                {proposed.length} action{proposed.length === 1 ? "" : "s"} waiting for approval.
              </strong>{" "}
              Nothing has happened yet.{" "}
              <Link to={`/w/${workspace.id}/approvals`}>
                Review {proposed.length === 1 ? "it" : "them"}
              </Link>
              .
            </Banner>
          ) : null}
        </div>

        <form className="composer" onSubmit={submit}>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter breaks the line. A textarea rather
              // than an input because these get long.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder={
              mode === "ask"
                ? "Ask something about the documents in this workspace…"
                : "Ask the agent to do something — “email the team a summary of the handbook”"
            }
            rows={2}
            disabled={!canWrite || busy}
          />
          {stream.streaming ? (
            <button type="button" className="button button--ghost" onClick={stream.cancel}>
              Stop
            </button>
          ) : (
            <button
              type="submit"
              className="button button--primary"
              disabled={!canWrite || busy || draft.trim() === ""}
            >
              {busy ? <Spinner label="Working" /> : mode === "ask" ? "Ask" : "Propose"}
            </button>
          )}
        </form>

        {!canWrite ? (
          <p className="composer__note">
            You are a viewer in this workspace, so you can read conversations but not start
            one.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  if (message.role === "tool") {
    return (
      <article className="bubble bubble--tool">
        <Badge tone="violet">tool</Badge>
        <div className="bubble__body">{message.content}</div>
      </article>
    );
  }

  return (
    <article className={`bubble bubble--${message.role}`}>
      <div className="bubble__body">{message.content}</div>
      <Citations citations={message.citations} />
    </article>
  );
}
