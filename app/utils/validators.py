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
