import { useState } from "react";

import type { Citation, StreamCitation } from "../../api/types";

/**
 * The evidence behind an answer, as chips that open what was actually quoted.
 *
 * `quoted_text` is a snapshot taken when the answer was written, not a live
 * read of the chunk (SPEC-v2 D5) — which is why a citation stays readable
 * after its source document has been deleted. When that has happened the chip
 * says so rather than quietly showing text with no provenance.
 */
export function Citations({ citations }: { citations: (Citation | StreamCitation)[] }) {
  const [open, setOpen] = useState<number | null>(null);

  if (citations.length === 0) return null;
  const shown = open === null ? null : citations[open];

  return (
    <div className="citations">
      <div className="citations__chips">
        {citations.map((citation, index) => {
          const orphaned = "chunk_id" in citation && citation.chunk_id === null;
          return (
            <button
              key={index}
              type="button"
              className={`chip${open === index ? " chip--open" : ""}${
                orphaned ? " chip--orphaned" : ""
              }`}
              onClick={() => setOpen(open === index ? null : index)}
              aria-expanded={open === index}
              title={orphaned ? "The source document has since been deleted" : undefined}
            >
              <span className="chip__index">{index + 1}</span>
              <span className="chip__doc">{citation.document_name}</span>
              {citation.page_no !== null ? (
                <span className="chip__page">p.{citation.page_no}</span>
              ) : null}
            </button>
          );
        })}
      </div>

      {shown !== undefined && shown !== null ? (
        <figure className="quote">
          <blockquote className="quote__text">{shown.quoted_text}</blockquote>
          <figcaption className="quote__source">
            {shown.document_name}
            {shown.page_no !== null ? `, page ${shown.page_no}` : ""}
            {" · "}
            <span title="Reciprocal Rank Fusion score across the vector and full-text retrievers">
              score {shown.score.toFixed(3)}
            </span>
            {"chunk_id" in shown && shown.chunk_id === null ? (
              <span className="quote__orphaned">
                {" · "}source document deleted — this quotation is the snapshot kept with
                the answer
              </span>
            ) : null}
          </figcaption>
        </figure>
      ) : null}
    </div>
  );
}
