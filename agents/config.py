"""
Central configuration for the module-generation pipeline.

All environment-driven constants live here so they can be imported by any
agent or the orchestrator without re-reading the environment in multiple
places.
"""
import os
import socket
from urllib.parse import urlparse

import logfire

# ---------------------------------------------------------------------------
# Directory defaults
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = os.getenv('MODULE_OUTPUT_DIR', './generated-modules')

# ---------------------------------------------------------------------------
# Retry / loop limits
# ---------------------------------------------------------------------------
MAX_ARTIFACT_LOOPS = int(os.getenv('MAX_ARTIFACT_LOOPS', '5'))
MAX_ESCALATIONS = int(os.getenv('MAX_ESCALATIONS', '2'))

# pydantic-ai defaults UsageLimits.request_limit to 50 per agent.run() call.
# Local models re-validate their own output far more chattily than hosted
# frontier models (observed: the planner re-running validate_module_plan and
# every validate_parameter_name call twice before emitting a final plan), so
# 50 is too tight for those and silently aborts a normal, correct run.
MAX_AGENT_REQUESTS = int(os.getenv('MAX_AGENT_REQUESTS', '150'))


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def _otlp_collector_target() -> tuple[str, int]:
    """(host, port) to probe for a reachable local OTel collector.

    Reads OTEL_EXPORTER_OTLP_TRACES_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT if
    set -- logfire.configure() itself already forwards spans to that endpoint
    when present (see OTEL_EXPORTER_OTLP_TRACES_ENDPOINT in
    logfire._internal.config), but the reachability pre-check below used to be
    hardcoded to localhost:4318 regardless, so a non-default collector
    endpoint would be silently unreachable-checked at the wrong address and
    telemetry would never get configured at all. Falls back to the
    conventional local-collector default (localhost:4318) when unset.
    """
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') or os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
    if not endpoint:
        return "localhost", 4318
    parsed = urlparse(endpoint if '://' in endpoint else f'//{endpoint}')
    return parsed.hostname or "localhost", parsed.port or 4318


def enable_telemetry(host: str = "localhost", port: int = 4318) -> bool:
    """Return True if an OpenTelemetry collector is reachable at host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def configure_telemetry() -> None:
    """Configure Logfire telemetry.

    Two independent ways this turns on:
      - LOGFIRE_TOKEN is set -> spans are sent to the real Logfire cloud
        dashboard (send_to_logfire='if-token-present' never blocks waiting
        for a token that isn't there, and never sends without one either).
      - A local OTel collector is reachable (localhost:4318 by default, or
        wherever OTEL_EXPORTER_OTLP_TRACES_ENDPOINT points) -> spans are
        exported there instead, matching the previous local-only behavior.
    Neither present: telemetry stays off, matching the previous default.
    """
    has_token = bool(os.getenv('LOGFIRE_TOKEN'))
    if not has_token and not enable_telemetry(*_otlp_collector_target()):
        return
    logfire.configure(send_to_logfire='if-token-present', service_name="module-toolkit")
    logfire.instrument_pydantic_ai()

