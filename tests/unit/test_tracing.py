from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidecar.observability.tracing import shutdown_tracing


def test_shutdown_tracing_exports_the_pending_batch():
    # The batch processor exports on a timer; at shutdown the last batch must not wait
    # for it (and the SDK's own atexit flush does not run on SIGTERM).
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))
    with provider.get_tracer("test").start_as_current_span("pending"):
        pass
    assert exporter.get_finished_spans() == ()

    shutdown_tracing(provider)

    assert [span.name for span in exporter.get_finished_spans()] == ["pending"]
