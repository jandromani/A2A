import base64
import logging
import os
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.secrets import resolve_config_secret

logger = logging.getLogger("agentic")


def build_langfuse_exporter(config: Settings):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if not (config.langfuse_host and config.langfuse_public_key and config.langfuse_secret_key):
        return None
    public_key = resolve_config_secret(config, "langfuse_public_key")
    secret_key = resolve_config_secret(config, "langfuse_secret_key")
    if not public_key or not secret_key:
        return None
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return OTLPSpanExporter(
        endpoint=f"{config.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {credentials}",
            "x-langfuse-ingestion-version": "4",
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def configure_tracing(app: FastAPI, config: Settings | None = None) -> None:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if not isinstance(trace.get_tracer_provider(), TracerProvider):
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": "agentic-atlas-platform",
                        "deployment.environment": os.getenv("ENVIRONMENT", "development"),
                    }
                )
            )
            exporter = build_langfuse_exporter(config) if config else None
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            if exporter:
                provider.add_span_processor(BatchSpanProcessor(exporter))
            elif endpoint:
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        logger.info(
            "OpenTelemetry instrumentation package not installed; request IDs remain enabled."
        )
