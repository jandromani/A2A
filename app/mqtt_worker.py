"""Standalone MQTT subscriber for deployments that scale the web API horizontally."""

import asyncio
import logging
import signal

from app.config import settings
from app.database import initialize_database, make_engine, make_session_factory
from app.mqtt_ingress import MQTTIngress
from app.secrets import resolve_secret

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    if not settings.mqtt_broker_url:
        raise RuntimeError("MQTT_BROKER_URL must be configured for app.mqtt_worker.")
    database_url = resolve_secret(settings, settings.database_url)
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    initialize_database(engine, factory, settings)
    ingress = MQTTIngress(factory, settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stopped.set)
    try:
        ingress.start()
        await stopped.wait()
    finally:
        ingress.stop()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
