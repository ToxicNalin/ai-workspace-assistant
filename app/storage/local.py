import asyncio
from pathlib import Path


class LocalObjectStore:
    """Filesystem-backed ObjectStore — tests and local development only."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        path = (self._base_dir / key).resolve()
        if path != self._base_dir and self._base_dir not in path.parents:
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path_for(key).read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def signed_url(self, key: str, *, expires_in: int = 3600) -> str:
        return self._path_for(key).as_uri()
