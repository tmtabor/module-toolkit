"""
Unit tests for agents/config.py's telemetry gating (temporal/PHASE5.md
Workstream F). No real Logfire/OTel network calls -- logfire.configure and
logfire.instrument_pydantic_ai are patched so these only assert *whether* and
*with what* configuration is attempted.
"""
from unittest.mock import patch

from agents import config


class TestOtlpCollectorTarget:

    def test_defaults_to_localhost_4318_when_unset(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert config._otlp_collector_target() == ("localhost", 4318)

    def test_parses_traces_endpoint_host_and_port(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://otel-collector:4319/v1/traces")
        assert config._otlp_collector_target() == ("otel-collector", 4319)

    def test_falls_back_to_generic_otlp_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4319")
        assert config._otlp_collector_target() == ("otel-collector", 4319)

    def test_defaults_port_when_endpoint_omits_it(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://otel-collector")
        assert config._otlp_collector_target() == ("otel-collector", 4318)


class TestConfigureTelemetry:

    def test_skips_configuration_when_no_token_and_no_collector(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        with patch.object(config, "enable_telemetry", return_value=False) as mock_enable, \
             patch.object(config.logfire, "configure") as mock_configure:
            config.configure_telemetry()
        mock_enable.assert_called_once()
        mock_configure.assert_not_called()

    def test_configures_when_local_collector_reachable(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        with patch.object(config, "enable_telemetry", return_value=True), \
             patch.object(config.logfire, "configure") as mock_configure, \
             patch.object(config.logfire, "instrument_pydantic_ai") as mock_instrument:
            config.configure_telemetry()
        mock_configure.assert_called_once_with(send_to_logfire="if-token-present", service_name="module-toolkit")
        mock_instrument.assert_called_once()

    def test_configures_without_probing_collector_when_token_present(self, monkeypatch):
        """A LOGFIRE_TOKEN means "send to the real dashboard" regardless of
        whether a local collector happens to be reachable -- the reachability
        probe exists only to gate the local-only fallback path."""
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        with patch.object(config, "enable_telemetry") as mock_enable, \
             patch.object(config.logfire, "configure") as mock_configure, \
             patch.object(config.logfire, "instrument_pydantic_ai"):
            config.configure_telemetry()
        mock_enable.assert_not_called()
        mock_configure.assert_called_once_with(send_to_logfire="if-token-present", service_name="module-toolkit")
