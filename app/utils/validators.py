import re
from pathlib import Path

from app.exceptions import PayloadTooLarge, UnsupportedMediaType

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Extension is checked against what the content actually sniffs as, never
# trusted on its own -- a renamed .exe claiming to be a .pdf must fail here.
_ALLOWED_EXTENSIONS: dict[str, set[str]] = {
    "application/pdf": {".pdf"},
    _DOCX_MIME: {".docx"},
    "text/plain": {".txt"},
    "text/markdown": {".md"},
}


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _sniff_mime_type(data: bytes, extension: str) -> str:
    if data.startswith(_PDF_MAGIC):
        return "application/pdf"
    if data.startswith(_ZIP_MAGIC):
        return _DOCX_MIME
    if _looks_like_text(data):
        return "text/markdown" if extension == ".md" else "text/plain"
    raise UnsupportedMediaType("Could not identify the file's type from its content")


def validate_upload(*, filename: str, data: bytes, max_size_bytes: int) -> str:
    """Validates a file by its actual bytes, not its declared name or
    Content-Type header, and returns the trusted MIME type.

    Raises PayloadTooLarge or UnsupportedMediaType on any failure.
    """
    if not data:
        raise UnsupportedMediaType("Empty file")

    if len(data) > max_size_bytes:
        raise PayloadTooLarge

    extension = Path(filename).suffix.lower()
    mime_type = _sniff_mime_type(data, extension)

    if extension not in _ALLOWED_EXTENSIONS.get(mime_type, set()):
        raise UnsupportedMediaType(
            f"'{extension or '(no extension)'}' does not match the file's actual content"
        )

    return mime_type


# Anything outside this set is replaced. Deliberately strict rather than
# "remove the dangerous characters": an allowlist stays correct when a new
# provider turns out to dislike something nobody thought of.
_UNSAFE_IN_KEY = re.compile(r"[^A-Za-z0-9._-]+")

# Split on both separators explicitly rather than using Path().name, which
# is platform-dependent: on Linux a backslash is an ordinary character, so a
# Windows path pasted in by a browser would keep its drive and folders in the
# key on CI while being stripped on a developer's laptop. Same input, two
# different keys, depending on where the code happens to run.
_ANY_SEPARATOR = re.compile(r"[\\/]")

# S3 keys are capped at 1024 bytes, and the workspace id and a uuid already
# take about 80 of them. This leaves plenty of room and keeps keys readable in
# a bucket listing.
MAX_KEY_COMPONENT_CHARS = 120


def safe_key_component(filename: str) -> str:
    """Turn an uploaded filename into something safe to put in a storage key.

    The original is kept on `documents.name` for display -- this is only for
    the key, which is a different job with different rules.

    Left unsanitised, the same filename behaves differently on each backend,
    which is the worst possible outcome: `../../x.txt` is caught by
    LocalObjectStore's root check and raises, but S3 treats it as a perfectly
    ordinary literal key and stores it. A whitespace or control character
    breaks request signing on one provider and not another. Deciding this here,
    once, means every backend sees the same key.
    """
    stem = _ANY_SEPARATOR.split(filename)[-1]
    cleaned = _UNSAFE_IN_KEY.sub("-", stem).strip("-.")

    if len(cleaned) > MAX_KEY_COMPONENT_CHARS:
        # Keep the extension: it is the part a human scanning a bucket uses.
        suffix = Path(cleaned).suffix[:12]
        cleaned = cleaned[: MAX_KEY_COMPONENT_CHARS - len(suffix)] + suffix

    # A name made entirely of separators sanitises to nothing, and an empty
    # component would produce a key ending in the uuid's dash.
    return cleaned or "upload"
