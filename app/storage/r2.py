from typing import Any

import aioboto3
from botocore.config import Config

from app.config import get_settings


class R2ObjectStore:
    """Cloudflare R2 over the S3 API. Production only — see app/storage/local.py
    for the implementation tests and local development actually run against.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._bucket = settings.r2_bucket
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.r2_endpoint_url,
            "aws_access_key_id": settings.r2_access_key_id,
            "aws_secret_access_key": settings.r2_secret_access_key,
            "config": Config(signature_version="s3v4"),
            "region_name": "auto",
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
