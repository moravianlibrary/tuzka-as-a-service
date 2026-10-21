"""Tests for app.telemetry: a job's trace continues across the Redis hop, and
telemetry stays off without an OTLP endpoint.

Pure (no collector, no redis): spans go to the SDK's in-memory exporter.
"""

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import propagate, trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from app import telemetry  # noqa: E402
from app.telemetry import inject_trace_context, job_span, setup_telemetry  # noqa: E402

_exporter = InMemorySpanExporter()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    # The global tracer provider can be set only once per process, so every test
    # shares this one and starts from an empty exporter.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
    _exporter.clear()
    return _exporter


def test_job_span_continues_the_trace_stored_at_submit(spans) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("submit_job") as submit:
        meta = {"username": "alice", "domain": "kramarky", **inject_trace_context()}
    assert "traceparent" in meta

    # Another process later: no current span, only the job's hash.
    with job_span("dispatch", "job-1", meta, backend_id=3):
        pass

    dispatch = next(s for s in spans.get_finished_spans() if s.name == "dispatch")
    assert dispatch.context.trace_id == submit.get_span_context().trace_id
    assert dispatch.parent is not None
    assert dispatch.parent.span_id == submit.get_span_context().span_id
    assert dispatch.attributes["taas.job_id"] == "job-1"
    assert dispatch.attributes["taas.domain"] == "kramarky"
    assert dispatch.attributes["taas.backend_id"] == 3


def test_job_without_trace_context_gets_a_root_span(spans) -> None:
    with job_span("harvest", "job-2", {"username": "bob"}):
        pass

    (harvest,) = spans.get_finished_spans()
    assert harvest.parent is None
    assert harvest.attributes["taas.domain"] == "default"


def test_worker_sampler_keeps_only_job_traces() -> None:
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(root=ALWAYS_OFF))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("redis GET"):  # a worker's per-tick call
        pass
    assert exporter.get_finished_spans() == ()

    parent = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    with (
        tracer.start_as_current_span("poll", context=propagate.extract(parent)),
        tracer.start_as_current_span("GET /api/v1/status"),  # a call inside a job
    ):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /api/v1/status", "poll"]


def test_off_without_an_otlp_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_enabled", False)
    before = trace.get_tracer_provider()

    assert setup_telemetry("taas-test") is False
    assert trace.get_tracer_provider() is before
