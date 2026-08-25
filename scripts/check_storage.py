"""Prove the configured object store actually works, before deploying.

    python -m scripts.check_storage

Reads whatever `STORAGE_BACKEND` and the `S3_*` settings say and does a real
round trip: write an object, read it back, sign a URL for it, delete it. Every
step prints, so a failure names the step rather than arriving as a stack trace
at upload time.

This exists because bad object-storage credentials do not fail loudly. A wrong
region or the wrong addressing style comes back as a SigV4 signature mismatch
that mentions neither, and the first place you would otherwise notice is a
document stuck at `pending` with an unhelpful `error_message`.
"""

import asyncio
import sys
import uuid
from urllib.parse import urlsplit

from app.config import get_settings
from app.storage.base import get_object_store

PAYLOAD = b"ai-workspace-assistant storage check\n"


def _describe() -> None:
    settings = get_settings()
    print(f"backend      : {settings.storage_backend}")

    if settings.storage_backend == "local":
        print(f"directory    : {settings.local_storage_dir}")
        print("\nThis is the filesystem backend. It is correct for local development")
        print("and tests, but Render throws the container's disk away on every")
        print("restart -- set STORAGE_BACKEND=s3 for anything deployed.")
        return

    endpoint = settings.s3_endpoint_url
    print(f"bucket       : {settings.s3_bucket}")
    print(f"endpoint     : {endpoint or '(not set)'}")
    print(f"region       : {settings.s3_region}")
    print(f"access key   : {'set' if settings.s3_access_key_id else 'MISSING'}")
    print(f"secret key   : {'set' if settings.s3_secret_access_key else 'MISSING'}")

    host = urlsplit(endpoint).hostname or ""
    if "supabase" in host:
        print("provider     : Supabase Storage")
        if settings.s3_region == "auto":
            print("\n  WARNING: region is 'auto', which is Cloudflare R2's convention.")
            print("  Supabase signs requests with the real region and will reject this.")
            print("  Copy the region from the Supabase S3 connection panel.")
    elif "r2.cloudflarestorage" in host:
        print("provider     : Cloudflare R2")


async def main() -> int:
    settings = get_settings()
    _describe()

    if settings.storage_backend == "s3" and not settings.s3_endpoint_url:
        print("\nFAIL: STORAGE_BACKEND=s3 but S3_ENDPOINT_URL is empty.")
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
        print(f"\n  FAILED at the step above: {type(exc).__name__}: {exc}")
        print("\n  A SignatureDoesNotMatch here almost always means the region is")
        print("  wrong, not the keys. Check it against the provider's dashboard.")
        return 1

    print("\nStorage is working. Nothing was left behind.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
