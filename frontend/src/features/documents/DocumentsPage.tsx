import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";

import { ApiError } from "../../api/client";
import * as api from "../../api/endpoints";
import type { Document, DocumentStatus } from "../../api/types";
import { Badge, Banner, Empty, Spinner } from "../../components/ui";
import { formatBytes, formatRelative } from "../../lib/format";
import { errorMessage, useAsync } from "../../lib/useAsync";
import { useWorkspace } from "../workspaces/WorkspaceContext";

const STATUS_TONE: Record<DocumentStatus, "neutral" | "amber" | "green" | "red"> = {
  pending: "neutral",
  processing: "amber",
  ready: "green",
  failed: "red",
};

/** How often to re-check while anything is still being ingested. */
const POLL_MS = 2500;

export function DocumentsPage() {
  const { workspace, canWrite } = useWorkspace();
  const documents = useAsync(() => api.listDocuments(workspace.id), [workspace.id]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const filePicker = useRef<HTMLInputElement>(null);

  const list = documents.data ?? [];
  const settling = list.some(
    (document) => document.status === "pending" || document.status === "processing",
  );

  /**
   * Poll while anything is mid-ingestion, and stop the moment nothing is.
   *
   * Ingestion is a background job drained inside the API process, so there is
   * nothing to push an update. Polling only while something is actually
   * moving is what keeps this from being a request every few seconds for as
   * long as the tab is open — which on Neon's 100 CU-hours a month is a real
   * cost, not a tidiness point.
   */
  const reload = documents.reload;
  useEffect(() => {
    if (!settling) return;
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
    // `reload` rather than the whole `documents` object: useAsync returns a
    // fresh object every render, so depending on it would tear the interval
    // down and rebuild it before it ever fired.
  }, [settling, reload]);

  async function upload(files: FileList | File[]) {
    const chosen = Array.from(files);
    if (chosen.length === 0) return;

    setError(null);
    setNotice(null);
    setUploading(chosen.map((file) => file.name));

    const duplicates: string[] = [];
    try {
      for (const file of chosen) {
        const result = await api.uploadDocument(workspace.id, file);
        if (result.deduplicated) duplicates.push(file.name);
      }
      if (duplicates.length > 0) {
        setNotice(
          `${duplicates.join(", ")} ${duplicates.length === 1 ? "was" : "were"} already here — ` +
            "matched by content hash, so nothing new was stored.",
        );
      }
    } catch (caught) {
      // 415 and 413 are the two worth explaining rather than echoing. The
      // server checks magic bytes, not the extension, so "it is a PDF" and
      // "it is named .pdf" are different claims.
      if (caught instanceof ApiError && caught.status === 415) {
        setError(
          "That file was refused. Type is checked by reading the file's magic bytes, not " +
            "its extension — a renamed file will not pass.",
        );
      } else if (caught instanceof ApiError && caught.status === 413) {
        setError("That file is over the 5 MB cap.");
      } else {
        setError(errorMessage(caught));
      }
    } finally {
      setUploading([]);
      documents.reload();
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    if (!canWrite) return;
    void upload(event.dataTransfer.files);
  }

  async function remove(document: Document) {
    if (!window.confirm(`Delete “${document.name}”? Its chunks go with it.`)) return;
    setError(null);
    try {
      await api.deleteDocument(workspace.id, document.id);
      documents.set(list.filter((item) => item.id !== document.id));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <section className="page">
      <header className="page__head">
        <div>
          <h1 className="page__title">Documents</h1>
          <p className="page__lede">
            Uploaded files are chunked, embedded and indexed for retrieval. Their text is
            treated as untrusted input throughout — it reaches the model inside delimiters in
            a user-role message, never in the system prompt.
          </p>
        </div>
      </header>

      {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}
      {notice ? (
        <Banner tone="info" onDismiss={() => setNotice(null)}>
          {notice}
        </Banner>
      ) : null}
      {documents.error ? <Banner>{documents.error}</Banner> : null}

      {canWrite ? (
        <div
          className={`dropzone${dragging ? " dropzone--active" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          onClick={() => filePicker.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") filePicker.current?.click();
          }}
        >
          <input
            ref={filePicker}
            type="file"
            multiple
            hidden
            accept=".pdf,.docx,.txt,.md"
            onChange={(event) => {
              if (event.target.files !== null) void upload(event.target.files);
              event.target.value = "";
            }}
          />
          {uploading.length > 0 ? (
            <p>
              <Spinner label="Uploading" /> Uploading {uploading.join(", ")}…
            </p>
          ) : (
            <>
              <p className="dropzone__title">Drop files here, or click to choose</p>
              <p className="dropzone__hint">
                PDF, DOCX, TXT or Markdown · up to 5 MB each · re-uploading the same content
                is detected by hash and stored once
              </p>
            </>
          )}
        </div>
      ) : (
        <Banner tone="info">Viewers can read documents but not add or remove them.</Banner>
      )}

      {documents.loading && documents.data === null ? <Spinner /> : null}

      {!documents.loading && list.length === 0 ? (
        <Empty title="No documents yet">
          <p>Upload something and the chat will have sources to cite.</p>
        </Empty>
      ) : null}

      {list.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th className="numeric">Chunks</th>
                <th className="numeric">Size</th>
                <th>Uploaded</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.map((document) => (
                <tr key={document.id}>
                  <td>
                    <span className="doc__name">{document.name}</span>
                    {document.error_message !== null ? (
                      <span className="doc__error">{document.error_message}</span>
                    ) : null}
                  </td>
                  <td>
                    <Badge tone={STATUS_TONE[document.status]}>{document.status}</Badge>
                  </td>
                  <td className="numeric">{document.chunk_count}</td>
                  <td className="numeric">{formatBytes(document.size_bytes)}</td>
                  <td>{formatRelative(document.uploaded_at)}</td>
                  <td className="table__actions">
                    {canWrite ? (
                      <button
                        type="button"
                        className="button button--ghost button--small"
                        onClick={() => void remove(document)}
                      >
                        Delete
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {settling ? (
        <p className="page__foot">
          <Spinner label="Ingesting" /> Ingestion runs in the background and this list is
          polling. A job killed part-way through is reclaimed and retried, so leaving this
          page is safe.
        </p>
      ) : null}
    </section>
  );
}
