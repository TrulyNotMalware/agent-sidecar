from typing import Any


def configure_tracing(app: Any, *, service_name: str) -> None:
    """Wire OpenTelemetry tracing onto the FastAPI app.

    Lazy imports keep otel optional at import time. Endpoint, headers, and
    sampling come from standard `OTEL_*` env variables (see otel docs); we only
    pin the service name and exporter type here.
    """
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)


def get_tracer(name: str):
    from opentelemetry import trace
    return trace.get_tracer(name)
