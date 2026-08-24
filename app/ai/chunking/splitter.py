import io
import re
from dataclasses import dataclass

from docx import Document as DocxDocument
from pypdf import PdfReader

from app.constants import CHUNK_OVERLAP_CHARS, CHUNK_SIZE_CHARS
from app.exceptions import UnsupportedMediaType

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Page:
    """One page of extracted text. `page_no` is None for formats that have no
    page concept until they are rendered."""

    text: str
    page_no: int | None


@dataclass(frozen=True)
class Chunk:
    text: str
    page_no: int | None
    chunk_index: int


def _extract_pdf(data: bytes) -> list[Page]:
    reader = PdfReader(io.BytesIO(data))
    return [
        Page(text=page.extract_text() or "", page_no=number)
        for number, page in enumerate(reader.pages, start=1)
    ]


def _extract_docx(data: bytes) -> list[Page]:
    document = DocxDocument(io.BytesIO(data))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    return [Page(text="\n\n".join(paragraphs), page_no=None)]


def extract_pages(data: bytes, *, mime_type: str) -> list[Page]:
    """Turn raw file bytes into text, preserving page numbers where the format
    actually has them. `mime_type` is the sniffed type recorded on the
    document row, never a client-supplied Content-Type."""
    if mime_type == "application/pdf":
        return _extract_pdf(data)
    if mime_type == _DOCX_MIME:
        return _extract_docx(data)
    if mime_type in {"text/plain", "text/markdown"}:
        return [Page(text=data.decode("utf-8", errors="replace"), page_no=None)]
    raise UnsupportedMediaType(f"No text extractor for '{mime_type}'")


def _hard_split(text: str, *, size: int, overlap: int) -> list[str]:
    """Last resort for a single paragraph longer than one chunk."""
    step = size - overlap
    pieces = (text[start : start + size] for start in range(0, len(text), step))
    return [piece.strip() for piece in pieces if piece.strip()]


def split_text(
    text: str, *, size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list[str]:
    """Greedy, paragraph-aware split. Paragraphs stay whole wherever they fit,
    because a chunk cut mid-sentence retrieves badly and reads worse when it
    is quoted back as a citation."""
    paragraphs = [
        paragraph.strip() for paragraph in _PARAGRAPH_BREAK.split(text) if paragraph.strip()
    ]

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(paragraph, size=size, overlap=overlap))
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= size:
            current = candidate
            continue

        chunks.append(current)
        # Carry the tail of the chunk just emitted into the next one, but only
        # when it still leaves room for the whole paragraph.
        tail = current[-overlap:].lstrip()
        current = f"{tail}\n\n{paragraph}" if len(tail) + len(paragraph) + 2 <= size else paragraph

    if current:
        chunks.append(current)

    return chunks


def split_pages(
    pages: list[Page], *, size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list[Chunk]:
    """Chunks never span a page boundary — merging across one would attach a
    page number to text that is not on that page, and the citation UI shows
    that number to the user."""
    chunks: list[Chunk] = []

    for page in pages:
        for piece in split_text(page.text, size=size, overlap=overlap):
            chunks.append(Chunk(text=piece, page_no=page.page_no, chunk_index=len(chunks)))

    return chunks
