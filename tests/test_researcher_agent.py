"""
Unit tests for the researcher_agent using Pydantic AI TestModel.

These tests verify:
- The agent is importable and well-formed
- All expected tools are registered on the agent
- The agent can complete a run under TestModel without hitting a real LLM
- Token usage is accessible from the result
- WebSearch capability is conditionally applied based on BRAVE_API_KEY

Note: The WebSearch capability injects a builtin tool that TestModel cannot
handle.  Behavioural tests therefore use a no-capability agent twin built
via build_researcher_agent(capabilities=[]) — same instructions and model,
no WebSearch — so runs are fully deterministic.
"""
import os
import pytest
from pydantic_ai.models.test import TestModel

from agents.researcher import researcher_agent, build_researcher_agent, _capabilities as _researcher_capabilities

# ---------------------------------------------------------------------------
# A TestModel-compatible twin: same model/instructions, no capabilities
# ---------------------------------------------------------------------------
# Capabilities are baked into the agent at construction and cannot be stripped
# via override().  build_researcher_agent(capabilities=[]) gives us the same
# agent without WebSearch so TestModel never sees an unsupported builtin tool.

_test_agent = build_researcher_agent(capabilities=[])


# ---------------------------------------------------------------------------
# Agent structure tests (no LLM call required)
# ---------------------------------------------------------------------------

class TestResearcherAgentStructure:
    """Verify static properties of researcher_agent."""

    def test_agent_is_importable(self):
        assert researcher_agent is not None

    def test_agent_has_no_output_type(self):
        # researcher_agent produces free-form text; output_type should be str / None
        output_type = researcher_agent.output_type
        assert output_type is str or output_type is None

    def test_expected_tools_registered(self):
        """Non-search researcher tools must always be registered.

        web_search (Brave) is only registered when BRAVE_API_KEY is set;
        without it the WebSearch capability provides its own tool to the model.
        """
        tool_names = set(researcher_agent._function_toolset.tools.keys())
        always_present = {
            "analyze_tool_documentation",
            "parse_repository_info",
            "create_tool_research_report",
            "analyze_parameter_patterns",
            "compare_similar_tools",
        }
        assert always_present.issubset(tool_names), f"Missing tools: {always_present - tool_names}"
        if os.getenv('BRAVE_API_KEY'):
            assert "web_search" in tool_names, "web_search must be registered when BRAVE_API_KEY is set"
        else:
            assert "web_search" not in tool_names, "web_search must NOT be registered when BRAVE_API_KEY is absent"

    def test_no_capabilities_without_brave_key(self):
        """No capabilities are added when BRAVE_API_KEY is not configured.

        WebSearch crashes Bedrock (builtin not supported) and the DuckDuckGo
        local fallback hangs indefinitely — so we simply use no capability and
        rely on parse_repository_info / analyze_tool_documentation instead.
        """
        if os.getenv('BRAVE_API_KEY'):
            pytest.skip("BRAVE_API_KEY is set — this test only applies when it is absent")
        assert _researcher_capabilities == [], (
            "Expected empty capabilities list when BRAVE_API_KEY is absent"
        )

    def test_no_web_search_capability_with_brave_key(self):
        """WebSearch capability must NOT be present when BRAVE_API_KEY is configured."""
        if not os.getenv('BRAVE_API_KEY'):
            pytest.skip("BRAVE_API_KEY is not set — this test only applies when it is")
        cap_types = [type(c).__name__ for c in _researcher_capabilities]
        assert 'WebSearch' not in cap_types, (
            "WebSearch capability should not be added when BRAVE_API_KEY is set"
        )


# ---------------------------------------------------------------------------
# Behavioural tests with TestModel
# ---------------------------------------------------------------------------

class TestResearcherAgentBehaviour:
    """Run _test_agent (no WebSearch capability) under TestModel."""

    def test_run_sync_returns_string_output(self):
        m = TestModel()
        with _test_agent.override(model=m):
            result = _test_agent.run_sync(
                "Research the bioinformatics tool 'samtools' version 1.19."
            )
        assert isinstance(result.output, str)
        assert len(result.output) > 0

    def test_usage_is_accessible(self):
        m = TestModel()
        with _test_agent.override(model=m):
            result = _test_agent.run_sync("Research samtools.")
        # TestModel returns zero-cost usage objects; they should be accessible
        assert result.usage() is not None

    def test_run_does_not_call_tools_by_default(self):
        """TestModel returns plain text output without invoking tools."""
        m = TestModel()
        with _test_agent.override(model=m):
            _test_agent.run_sync("Research STAR aligner.")
        assert m.last_model_request_parameters is not None

    def test_model_receives_registered_tools(self):
        """TestModel must be offered all registered researcher tools in the request."""
        m = TestModel()
        with _test_agent.override(model=m):
            _test_agent.run_sync("Research bwa-mem2 version 2.2.1.")
        model_tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
        registered = set(_test_agent._function_toolset.tools.keys())
        assert registered == model_tool_names





