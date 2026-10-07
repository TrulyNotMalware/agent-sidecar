from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_tracing(app: FastAPI, *, service_name: str) -> TracerProvider:
    """Wire OpenTelemetry tracing onto the FastAPI app; returns the provider to shut down.

    Endpoint, headers, and sampling come from standard `OTEL_*` env variables (see
    otel docs); only the service name and exporter type are pinned here.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    return provider


def shutdown_tracing(provider: TracerProvider) -> None:
    """Export the spans the batch processor still holds and stop it (blocking).

    The SDK registers the same with atexit, which never runs on SIGTERM: uvicorn
    re-raises the signal once its graceful shutdown is done, so without this the
    last batch of a pod's spans is lost.
    """
    provider.shutdown()


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
