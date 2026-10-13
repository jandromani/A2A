import logging
import os
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("agentic")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def configure_tracing(app: FastAPI) -> None:
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
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            if endpoint:
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        logger.info(
            "OpenTelemetry instrumentation package not installed; request IDs remain enabled."
        )
