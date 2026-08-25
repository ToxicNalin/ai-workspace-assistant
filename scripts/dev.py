"""Run the API locally.

Exists because of one Windows-specific incompatibility. psycopg's async mode --
which the LangGraph checkpointer uses -- cannot run on ProactorEventLoop, and
uvicorn chooses its loop with a `loop_factory` that hardcodes exactly that on
Windows. Setting the event loop policy has no effect on a factory, so the loop
has to be built here instead.

Linux, and therefore Render, already uses SelectorEventLoop; deployments run
`uvicorn app.main:app` directly and never touch this file.

    python scripts/dev.py
"""

import asyncio
import sys

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
