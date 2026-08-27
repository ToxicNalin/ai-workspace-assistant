import { useCallback, useEffect, useRef, useState } from "react";

import * as api from "../../api/endpoints";
import { readEvents } from "../../api/sse";
import type { StreamCitation, Uuid } from "../../api/types";
import { errorMessage } from "../../lib/useAsync";

export interface StreamingAnswer {
  text: string;
  citations: StreamCitation[];
  done: boolean;
}

interface State {
  answer: StreamingAnswer | null;
  error: string | null;
  streaming: boolean;
}

/**
 * Consumes `GET /chat/stream`.
 *
 * The event order is a contract the server documents and this relies on:
 * `meta` once with the thread id, then `token` deltas, then `citations`
 * after the last token, then `done` with the persisted message id. Citations
 * arrive last because sending them earlier would claim an answer cites
 * something it had not finished saying.
 *
 * Two consequences shape the code. `meta` is used to attach to a new thread
 * before the answer finishes, so the thread list can update immediately. And
 * nothing here persists anything — the server writes the message when the
 * model finishes, so an abandoned stream leaves no truncated answer behind.
 */
export function useChatStream(workspaceId: Uuid) {
  const [state, setState] = useState<State>({ answer: null, error: null, streaming: false });
  const abort = useRef<AbortController | null>(null);

  // Abandon an in-flight stream when the workspace changes or the page is
  // left. Without this the generator keeps writing into a component that has
  // gone, and the request keeps running on a metered free tier.
  useEffect(
    () => () => {
      abort.current?.abort();
      abort.current = null;
    },
    [workspaceId],
  );

  const cancel = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    setState((current) => ({ ...current, streaming: false }));
  }, []);

  const ask = useCallback(
    async (
      question: string,
      threadId: Uuid | null,
      handlers: {
        onThread: (id: Uuid) => void;
        onFinished: (id: Uuid) => void;
      },
    ): Promise<void> => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;

      setState({ answer: { text: "", citations: [], done: false }, error: null, streaming: true });

      try {
        const response = await api.openChatStream(
          workspaceId,
          question,
          threadId,
          controller.signal,
        );

        for await (const event of readEvents(response)) {
          if (controller.signal.aborted) return;
          const data: unknown = JSON.parse(event.data);

          if (event.event === "meta") {
            handlers.onThread((data as { thread_id: Uuid }).thread_id);
          } else if (event.event === "token") {
            const piece = (data as { text: string }).text;
            setState((current) =>
              current.answer === null
                ? current
                : { ...current, answer: { ...current.answer, text: current.answer.text + piece } },
            );
          } else if (event.event === "citations") {
            const citations = (data as { citations: StreamCitation[] }).citations;
            setState((current) =>
              current.answer === null
                ? current
                : { ...current, answer: { ...current.answer, citations } },
            );
          } else if (event.event === "done") {
            const finished = data as { thread_id: Uuid; message_id: Uuid };
            setState((current) =>
              current.answer === null
                ? current
                : { ...current, answer: { ...current.answer, done: true }, streaming: false },
            );
            handlers.onFinished(finished.thread_id);
          }
        }
      } catch (caught) {
        // An abort is this component asking, not a failure to report.
        if (controller.signal.aborted) return;
        setState({ answer: null, error: errorMessage(caught), streaming: false });
      } finally {
        if (abort.current === controller) abort.current = null;
        setState((current) => (current.streaming ? { ...current, streaming: false } : current));
      }
    },
    [workspaceId],
  );

  const clear = useCallback(
    () => setState({ answer: null, error: null, streaming: false }),
    [],
  );

  return { ...state, ask, cancel, clear };
}
