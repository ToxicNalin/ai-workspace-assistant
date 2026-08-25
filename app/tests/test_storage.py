"""The object store, both implementations.

The S3 cases below sign real requests and assert what comes out, without ever
opening a socket. That matters more than it sounds: the two settings that
differ between S3-compatible providers -- the addressing style and the region
-- are both inputs to the SigV4 signature, and getting either wrong fails at
runtime as a signature mismatch that never mentions addressing or regions.
Cloudflare R2 hides both mistakes (it ignores the region and accepts
path-style), so a bucket that works on R2 can fail on Supabase and the error
tells you nothing. These tests are how that gets caught here instead.
"""

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.config import Settings
from app.storage.local import LocalObjectStore
from app.storage.s3 import S3ObjectStore
from app.utils.validators import MAX_KEY_COMPONENT_CHARS, safe_key_component

SUPABASE_ENDPOINT = "https://abcdefghijklm.supabase.co/storage/v1/s3"
R2_ENDPOINT = "https://0123456789abcdef.r2.cloudflarestorage.com"


def _store(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> S3ObjectStore:
    """An S3ObjectStore built against fixed settings, with no network at all.

    Creating a botocore client and signing a URL are both local operations, so
    the credentials here never leave the process.
    """
    settings = Settings(
        s3_bucket="documents",
        s3_access_key_id="AKIAEXAMPLE",
        s3_secret_access_key="secret",
        **overrides,
    )
    monkeypatch.setattr("app.storage.s3.get_settings", lambda: settings)
    return S3ObjectStore()


# --------------------------------------------------------------------------
# S3, as Supabase needs it.
# --------------------------------------------------------------------------


async def test_the_bucket_goes_in_the_path_not_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supabase serves every bucket from one host under a path.

    Virtual-host addressing -- botocore's default -- would sign a request for
    `documents.abcdefghijklm.supabase.co`, a hostname that does not exist.
    """
    store = _store(
        monkeypatch, s3_endpoint_url=SUPABASE_ENDPOINT, s3_region="ap-south-1"
    )

    url = await store.signed_url("workspace/report.pdf")
    parts = urlsplit(url)

    assert parts.hostname == "abcdefghijklm.supabase.co"
    assert "documents" not in (parts.hostname or ""), "bucket must not be a subdomain"
    assert parts.path == "/storage/v1/s3/documents/workspace/report.pdf"


async def test_the_configured_region_reaches_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The region is part of the SigV4 credential scope, not decoration.

    R2 ignores it and takes the literal `auto`; Supabase and AWS verify it. A
    wrong one is rejected as a signature error that says nothing about regions,
    which is why it is a setting rather than a constant.
    """
    store = _store(
        monkeypatch, s3_endpoint_url=SUPABASE_ENDPOINT, s3_region="ap-south-1"
    )

    url = await store.signed_url("workspace/report.pdf")

    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "%2Fap-south-1%2Fs3%2Faws4_request" in url


async def test_the_same_client_still_signs_correctly_for_r2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving back to Cloudflare R2 is four values in a dashboard, not a code
    change. This is the assertion that keeps that claim true."""
    store = _store(monkeypatch, s3_endpoint_url=R2_ENDPOINT, s3_region="auto")

    url = await store.signed_url("workspace/report.pdf")
    parts = urlsplit(url)

    assert parts.hostname == "0123456789abcdef.r2.cloudflarestorage.com"
    assert parts.path == "/documents/workspace/report.pdf"
    assert "%2Fauto%2Fs3%2Faws4_request" in url


async def test_a_signed_url_carries_the_requested_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(
        monkeypatch, s3_endpoint_url=SUPABASE_ENDPOINT, s3_region="ap-south-1"
    )

    url = await store.signed_url("workspace/report.pdf", expires_in=120)

    assert "X-Amz-Expires=120" in url


# --------------------------------------------------------------------------
# Local, which is what tests and a fresh clone actually run against.
# --------------------------------------------------------------------------


async def test_the_local_store_round_trips(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    key = f"{uuid.uuid4()}/notes.txt"

    await store.put(key, b"hello world", content_type="text/plain")
    assert await store.get(key) == b"hello world"

    await store.delete(key)
    with pytest.raises(FileNotFoundError):
        await store.get(key)


async def test_deleting_something_that_is_not_there_is_not_an_error(
    tmp_path: Path,
) -> None:
    """Ingestion retries and document deletion both have to be safe to repeat --
    the free tier can kill the process at any point (SPEC-v2 section 3)."""
    store = LocalObjectStore(tmp_path)

    await store.delete("never/existed.txt")


async def test_a_key_cannot_escape_the_storage_root(tmp_path: Path) -> None:
    """Storage keys are built from a workspace id and a filename, and the
    filename came from an upload. A key that climbs out of the root would let
    one workspace read or overwrite another's files -- or anything else the
    process can reach."""
    store = LocalObjectStore(tmp_path / "uploads")

    with pytest.raises(ValueError):
        await store.put("../escaped.txt", b"x", content_type="text/plain")


# --------------------------------------------------------------------------
# Choosing between them.
# --------------------------------------------------------------------------


def test_the_backend_setting_picks_the_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.storage import base

    monkeypatch.setattr(base, "get_settings", lambda: Settings(storage_backend="local"))
    base.get_object_store.cache_clear()
    assert isinstance(base.get_object_store(), LocalObjectStore)

    monkeypatch.setattr(
        base,
        "get_settings",
        lambda: Settings(storage_backend="s3", s3_endpoint_url=SUPABASE_ENDPOINT),
    )
    base.get_object_store.cache_clear()
    assert isinstance(base.get_object_store(), S3ObjectStore)

    # The rest of the suite shares this cache, and it is keyed on nothing.
    base.get_object_store.cache_clear()


# --------------------------------------------------------------------------
# Turning a filename into a storage key.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", "report.pdf"),
        # Directory parts are stripped rather than escaped. On S3 a key is
        # flat, so `../../x` is a legal literal key and would be stored
        # happily -- while LocalObjectStore's root check rejects it. Same
        # input, different behaviour per backend, which is the thing to avoid.
        ("../../escape.txt", "escape.txt"),
        (r"C:\Users\nalin\notes.md", "notes.md"),
        # Whitespace and control characters break request signing on some
        # providers and not others.
        ("quarterly report.pdf", "quarterly-report.pdf"),
        ("odd\tname\nhere.txt", "odd-name-here.txt"),
        # Non-ASCII is replaced, not transliterated: the display name on the
        # documents row keeps the original.
        ("rapport-financiér.pdf", "rapport-financi-r.pdf"),
        # A name that sanitises away entirely still has to produce something.
        ("..", "upload"),
        ("///", "upload"),
    ],
)
def test_a_filename_becomes_a_safe_key_component(filename: str, expected: str) -> None:
    assert safe_key_component(filename) == expected


def test_a_very_long_filename_is_truncated_but_keeps_its_extension() -> None:
    """S3 caps a key at 1024 bytes and the workspace id plus a uuid already
    spend about 80. The extension survives because it is what a human scanning
    a bucket listing actually reads."""
    component = safe_key_component("x" * 400 + ".pdf")

    assert len(component) <= MAX_KEY_COMPONENT_CHARS
    assert component.endswith(".pdf")


async def test_an_awkward_filename_round_trips_through_the_local_store(
    tmp_path: Path,
) -> None:
    """The end of the chain: a sanitised component is a key both backends
    accept. Before this, a document called `../../x.txt` raised ValueError out
    of LocalObjectStore in the middle of an upload."""
    store = LocalObjectStore(tmp_path)
    key = f"{uuid.uuid4()}/{uuid.uuid4()}-{safe_key_component('../../escape.txt')}"

    await store.put(key, b"contained", content_type="text/plain")

    assert await store.get(key) == b"contained"
