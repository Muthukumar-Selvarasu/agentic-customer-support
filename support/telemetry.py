"""Send traces to the Phoenix process on this machine."""

import json
import urllib.error
import urllib.request

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

PHOENIX_ENDPOINT = "http://127.0.0.1:6006/v1/traces"
PHOENIX_PROJECTS = "http://127.0.0.1:6006/v1/projects"
PHOENIX_PROJECT = "support"

_ready = False
_project_id = ""


def _project_node_id() -> str:
    """Phoenix pages use the project node id, not the project name."""
    global _project_id
    if _project_id:
        return _project_id
    try:
        with urllib.request.urlopen(PHOENIX_PROJECTS, timeout=3) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError) as exc:
        raise SystemExit(f"Phoenix is unreachable at {PHOENIX_PROJECTS}: {exc}") from exc
    for project in payload.get("data", []):
        if project.get("name") == PHOENIX_PROJECT:
            _project_id = project["id"]
            return _project_id
    request = urllib.request.Request(
        PHOENIX_PROJECTS,
        data=json.dumps({"name": PHOENIX_PROJECT}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        created = json.load(response)
    _project_id = created["data"]["id"]
    return _project_id


def setup_telemetry() -> None:
    """Install the global tracer and Gemini instrumentation before any agent is built."""
    global _ready
    if _ready:
        return
    _project_node_id()
    provider = TracerProvider(
        resource=Resource.create({"openinference.project.name": PHOENIX_PROJECT})
    )
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=PHOENIX_ENDPOINT))
    )
    trace.set_tracer_provider(provider)
    GoogleGenAIInstrumentor().instrument()
    _ready = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("support")


def trace_url(trace_id: int) -> str:
    return (
        "http://localhost:6006/projects/"
        f"{_project_node_id()}/traces/{trace_id:032x}"
    )
