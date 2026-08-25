from typing import Any

import aioboto3
from botocore.config import Config

from app.config import get_settings


class S3ObjectStore:
    """Any S3-compatible object store. Production only — see app/storage/local.py
    for the implementation tests and local development actually run against.

    One client, several possible providers. SPEC-v2 D12 picked Cloudflare R2
    for its permanently free 10 GB and zero egress; R2 asks for a card at
    signup, so this deployment points the same client at Supabase Storage,
    which does not. Neither name appears below, and that is the point: the
    endpoint and the region are configuration, so moving between providers is
    four values in a dashboard rather than a code change.

    The two things that differ between them are exactly the two that are
    settings. R2 wants the literal region `auto` and ignores it; Supabase and
    AWS want the project's real region, and SigV4 signing fails if it is wrong.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.s3_bucket
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.s3_endpoint_url,
            "aws_access_key_id": settings.s3_access_key_id,
            "aws_secret_access_key": settings.s3_secret_access_key,
            "config": Config(
                signature_version="s3v4",
                # Virtual-host addressing would resolve `bucket.<endpoint>`,
                # which only exists for providers that give each bucket its own
                # subdomain. Supabase serves every bucket from one host under a
                # path, so the bucket has to go in the path.
                s3={"addressing_style": "path"},
            ),
            "region_name": settings.s3_region,
        }

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        async with self._session.client("s3", **self._client_kwargs) as client:
            await client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )

    async def get(self, key: str) -> bytes:
        async with self._session.client("s3", **self._client_kwargs) as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = await response["Body"].read()
            return body

    async def delete(self, key: str) -> None:
        async with self._session.client("s3", **self._client_kwargs) as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def signed_url(self, key: str, *, expires_in: int = 3600) -> str:
        async with self._session.client("s3", **self._client_kwargs) as client:
            url: str = await client.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_in
            )
            return url
