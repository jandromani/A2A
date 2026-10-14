"""Standalone durable outbox worker entry point for scaled deployments."""

import asyncio
import logging

from app.config import settings
from app.database import initialize_database, make_engine, make_session_factory
from app.event_worker import run_event_worker
from app.secrets import resolve_secret

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    database_url = resolve_secret(settings, settings.database_url)
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    initialize_database(engine, factory, settings)
    try:
        await run_event_worker(factory, settings.model_copy(update={"event_worker_enabled": True}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
