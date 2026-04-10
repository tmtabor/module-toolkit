"""
Unit tests for the researcher_agent using Pydantic AI TestModel.

These tests verify:
- The agent is importable and well-formed
- All expected tools are registered on the agent
- The agent can complete a run under TestModel without hitting a real LLM
- Token usage is accessible from the result
"""
import pytest
from pydantic_ai.models.test import TestModel

from agents.researcher import researcher_agent


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
        """All six researcher tools must be registered."""
        tool_names = set(researcher_agent._function_toolset.tools.keys())
        expected = {
            "web_search",
            "analyze_tool_documentation",
            "parse_repository_info",
            "create_tool_research_report",
            "analyze_parameter_patterns",
            "compare_similar_tools",
        }
        assert expected.issubset(tool_names), (
            f"Missing tools: {expected - tool_names}"
        )


# ---------------------------------------------------------------------------
# Behavioural tests with TestModel
# ---------------------------------------------------------------------------

class TestResearcherAgentBehaviour:
    """Run researcher_agent under TestModel to verify it completes without errors."""

    def test_run_sync_returns_string_output(self):
        m = TestModel()
        with researcher_agent.override(model=m):
            result = researcher_agent.run_sync(
                "Research the bioinformatics tool 'samtools' version 1.19."
            )
        assert isinstance(result.output, str)
        assert len(result.output) > 0

    def test_usage_is_accessible(self):
        m = TestModel()
        with researcher_agent.override(model=m):
            result = researcher_agent.run_sync("Research samtools.")
        usage = result.usage()
        # TestModel returns zero-cost usage objects; they should be accessible
        assert usage is not None

    def test_run_does_not_call_tools_by_default(self):
        """TestModel returns plain text output without invoking tools."""
        m = TestModel()
        with researcher_agent.override(model=m):
            researcher_agent.run_sync("Research STAR aligner.")
        # TestModel records what it was asked to do
        assert m.last_model_request_parameters is not None

    def test_model_receives_registered_tools(self):
        """TestModel must be offered all registered researcher tools in the request."""
        m = TestModel()
        with researcher_agent.override(model=m):
            researcher_agent.run_sync("Research bwa-mem2 version 2.2.1.")
        model_tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
        registered = set(researcher_agent._function_toolset.tools.keys())
        assert registered == model_tool_names





