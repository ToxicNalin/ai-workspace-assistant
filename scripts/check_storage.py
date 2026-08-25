"""Prove the configured object store actually works, before deploying.

    python -m scripts.check_storage

Reads whatever `STORAGE_BACKEND` and the `S3_*` settings say and does a real
round trip: write an object, read it back, sign a URL for it, delete it. Every
step prints, so a failure names the step rather than arriving as a stack trace
at upload time.

This exists because bad object-storage configuration does not fail loudly. A
wrong region comes back as a SigV4 signature mismatch that never mentions
regions and reads exactly like bad credentials; a wrong bucket name is a bare
`NoSuchBucket`. Both are cheap to diagnose here and expensive to diagnose in a
deploy log, so this script turns each of them into a sentence that says what to
change.
"""

import asyncio
import sys
import uuid
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings
from app.storage.base import get_object_store

PAYLOAD = b"ai-workspace-assistant storage check\n"


def _describe() -> list[str]:
    """Print the configuration and return anything that is obviously wrong."""
    settings = get_settings()
    problems: list[str] = []

    print(f"backend      : {settings.storage_backend}")

    if settings.storage_backend == "local":
        print(f"directory    : {settings.local_storage_dir}")
        print("\nThis is the filesystem backend. Correct for local development and")
        print("tests, but Render throws the container's disk away on every restart --")
        print("set STORAGE_BACKEND=s3 for anything deployed.")
        return problems

    endpoint = settings.s3_endpoint_url
    print(f"bucket       : {settings.s3_bucket}")
    print(f"endpoint     : {endpoint or '(not set)'}")
    print(f"region       : {settings.s3_region}")
    print(f"access key   : {'set' if settings.s3_access_key_id else 'NOT SET'}")
    print(f"secret key   : {'set' if settings.s3_secret_access_key else 'NOT SET'}")

    if not endpoint:
        problems.append("S3_ENDPOINT_URL is empty.")
    if not settings.s3_access_key_id:
        problems.append("S3_ACCESS_KEY_ID is empty.")
    if not settings.s3_secret_access_key:
        problems.append("S3_SECRET_ACCESS_KEY is empty.")

    host = urlsplit(endpoint).hostname or ""
    if "supabase" in host:
        print("provider     : Supabase Storage")
        if settings.s3_region == "auto":
            problems.append(
                "S3_REGION is 'auto', which is Cloudflare R2's convention. Supabase "
                "signs requests with the real region and will reject this. Copy the "
                "region from the Supabase S3 connection panel."
            )
    elif "r2.cloudflarestorage" in host:
        print("provider     : Cloudflare R2")

    return problems


async def _buckets_that_do_exist() -> list[str] | None:
    """Best effort: what buckets can these credentials actually see?

    Turns a bare NoSuchBucket into a useful answer. Returns None if the
    credentials are not allowed to list, which is normal for a scoped key and
    not itself a problem.
    """
    import aioboto3
    from botocore.config import Config

    settings = get_settings()
    session = aioboto3.Session()
    try:
        async with session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        ) as client:
            response: dict[str, Any] = await client.list_buckets()
            return [b["Name"] for b in response.get("Buckets", [])]
    except Exception:  # noqa: BLE001
        return None


async def main() -> int:
    settings = get_settings()
    problems = _describe()

    if problems:
        print("\nNot ready yet:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    store = get_object_store()
    key = f"_storage-check/{uuid.uuid4()}.txt"
    print(f"\nkey          : {key}\n")

    try:
        await store.put(key, PAYLOAD, content_type="text/plain")
        print("  put         OK")

        fetched = await store.get(key)
        if fetched != PAYLOAD:
            print(f"  get         FAIL  read back {len(fetched)} bytes, not what was written")
            return 1
        print(f"  get         OK    {len(fetched)} bytes, identical")

        url = await store.signed_url(key, expires_in=60)
        print(f"  signed_url  OK    {url[:78]}...")

        await store.delete(key)
        print("  delete      OK")

    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        text = str(exc)
        print(f"\n  FAILED at the step above: {name}: {text}")

        if "NoSuchBucket" in text or "NoSuchBucket" in name:
            print(f"\n  The bucket '{settings.s3_bucket}' does not exist at this endpoint.")
            existing = await _buckets_that_do_exist()
            if existing:
                print(f"  Buckets these credentials can see: {', '.join(existing)}")
                print("  Set S3_BUCKET to one of those, or create the one you meant.")
            else:
                print("  Create it in the dashboard, or check S3_BUCKET for a typo.")

        elif "SignatureDoesNotMatch" in text:
            print("\n  A SignatureDoesNotMatch is almost always the region, not the keys.")
            print(f"  S3_REGION is currently '{settings.s3_region}'. Check it against")
            print("  the provider's dashboard -- the region is signed into the request.")

        elif "InvalidAccessKeyId" in text or "403" in text:
            print("\n  The endpoint answered but rejected the credentials. Re-copy the")
            print("  access key id and secret; the secret is only shown once, so a")
            print("  truncated paste is the usual cause.")

        return 1

    print("\nStorage is working. Nothing was left behind.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
