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


def _error_code(exc: Exception) -> str:
    """Pull the S3 error code out of a botocore exception.

    Not from str(exc), which is where the obvious version of this looks and
    where the code is frequently absent: botocore renders
    "An error occurred () when calling the PutObject operation:" when the
    provider returns the code outside the Error element, which is exactly what
    Supabase does for SignatureDoesNotMatch. A diagnostic that misses the one
    error it was written to explain is worse than no diagnostic.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return type(exc).__name__

    error = response.get("Error")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("Code") or "")

    return code or str(response.get("Code") or "") or type(exc).__name__


def _client(session: Any, region: str | None = None) -> Any:
    from botocore.config import Config

    settings = get_settings()
    return session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=region or settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
            connect_timeout=15,
            read_timeout=15,
        ),
    )


async def _buckets_that_do_exist() -> list[str] | None:
    """Best effort: what buckets can these credentials actually see?

    Turns a bare NoSuchBucket into a useful answer. Returns None if the
    credentials cannot list, which is normal for a scoped key.
    """
    import aioboto3

    try:
        async with _client(aioboto3.Session()) as client:
            response: dict[str, Any] = await client.list_buckets()
            return [b["Name"] for b in response.get("Buckets", [])]
    except Exception:  # noqa: BLE001
        return None


async def _is_the_region_wrong() -> str | None:
    """A signature failure is either the region or the secret. Find out which.

    The region is signed into every request, so a wrong one and a wrong secret
    produce the identical SignatureDoesNotMatch. They are trivially told apart
    by trying other regions: if one works, that is the answer; if none does,
    the region was never the problem. Returns a working region, or None.

    Only ever runs after a failure, and only against list_buckets, which
    carries no payload -- so this isolates the signature from anything to do
    with content encoding.
    """
    import aioboto3

    settings = get_settings()
    candidates = [
        r
        for r in (
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            "ap-south-1", "ap-southeast-1", "ap-southeast-2",
            "ap-northeast-1", "ca-central-1",
            "eu-west-1", "eu-west-2", "eu-central-1", "sa-east-1",
        )
        if r != settings.s3_region
    ]

    session = aioboto3.Session()
    for region in candidates:
        try:
            async with _client(session, region) as client:
                await client.list_buckets()
                return region
        except Exception:  # noqa: BLE001
            continue

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
        code = _error_code(exc)
        print(f"\n  FAILED at the step above: {code}")

        if code == "NoSuchBucket":
            print(f"\n  The bucket '{settings.s3_bucket}' does not exist at this endpoint.")
            existing = await _buckets_that_do_exist()
            if existing:
                print(f"  Buckets these credentials can see: {', '.join(existing)}")
                print("  Set S3_BUCKET to one of those, or create the one you meant.")
            else:
                print("  Create it in the dashboard, or check S3_BUCKET for a typo.")

        elif code == "SignatureDoesNotMatch":
            print("\n  The endpoint answered, so the URL is right. A signature failure")
            print("  is either the region or the secret -- they are indistinguishable")
            print("  from one request. Checking other regions to find out which...")

            working = await _is_the_region_wrong()
            if working:
                print(f"\n  It is the region. '{working}' signs correctly.")
                print(f"  Set S3_REGION={working} (currently '{settings.s3_region}').")
            else:
                print("\n  No region works, so the region was never the problem:")
                print("  S3_SECRET_ACCESS_KEY is wrong.")
                print("  Supabase shows the secret once, so a partial copy is the usual")
                print("  cause. Generate a fresh key pair and replace BOTH values --")
                print("  the id and the secret only work as a pair.")
                print("  Also check the key belongs to this project: the endpoint above")
                print("  names the project ref it expects.")

        elif code == "InvalidAccessKeyId":
            print("\n  The provider does not recognise S3_ACCESS_KEY_ID at all -- so this")
            print("  is the id, not the secret. Usually the key was deleted and replaced,")
            print("  or it belongs to a different project than the endpoint above.")
            print("  Worth knowing: this is a different answer from SignatureDoesNotMatch,")
            print("  which means the id IS recognised and only the secret is wrong.")

        elif code in {"AccessDenied", "403"}:
            print("\n  The credentials are valid but not allowed to do this. Check the")
            print("  key's scope in the dashboard -- a key restricted to one bucket")
            print("  fails this way against any other.")

        return 1

    print("\nStorage is working. Nothing was left behind.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
