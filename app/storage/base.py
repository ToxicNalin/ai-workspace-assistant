from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def signed_url(self, key: str, *, expires_in: int = 3600) -> str: ...


@lru_cache
def get_object_store() -> ObjectStore:
    settings = get_settings()

    if settings.storage_backend == "s3":
        from app.storage.s3 import S3ObjectStore

        return S3ObjectStore()

    from app.storage.local import LocalObjectStore

    return LocalObjectStore(Path(settings.local_storage_dir))
