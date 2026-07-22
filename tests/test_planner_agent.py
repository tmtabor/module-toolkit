"""
Unit tests for the planner_agent using Pydantic AI TestModel.

These tests verify:
- The agent declares output_type=ModulePlan at construction time
- All expected tools are registered
- A run under TestModel produces a ModulePlan-shaped result
- The generated plan satisfies basic structural invariants
"""
import pytest
from pydantic_ai.models.test import TestModel

from agents.models import ModulePlan
from agents.planner import planner_agent


# ---------------------------------------------------------------------------
# Agent structure tests (no LLM call required)
# ---------------------------------------------------------------------------

class TestPlannerAgentStructure:
    """Verify static properties of planner_agent."""

    def test_agent_is_importable(self):
        assert planner_agent is not None

    def test_output_type_is_module_plan(self):
        """planner_agent must declare output_type=ModulePlan at construction."""
        assert planner_agent.output_type is ModulePlan, (
            "planner_agent.output_type should be ModulePlan, not set at call-site."
        )

    def test_expected_tools_registered(self):
        tool_names = set(planner_agent._function_toolset.tools.keys())
        expected = {
            "create_structured_plan",
            "analyze_parameter_structure",
            "generate_command_line",
            "generate_lsid",
        }
        assert expected.issubset(tool_names), (
            f"Missing tools: {expected - tool_names}"
        )


# ---------------------------------------------------------------------------
# Behavioural tests with TestModel
# ---------------------------------------------------------------------------

class TestPlannerAgentBehaviour:
    """Run planner_agent under TestModel to verify it completes and returns a ModulePlan."""

    def test_run_sync_returns_module_plan(self):
        m = TestModel()
        with planner_agent.override(model=m):
            result = planner_agent.run_sync(
                "Create a plan for the GenePattern module 'TestTool'."
            )
        # TestModel synthesises a valid ModulePlan because output_type is declared
        assert isinstance(result.output, ModulePlan)

    def test_module_plan_has_required_fields(self):
        m = TestModel()
        with planner_agent.override(model=m):
            result = planner_agent.run_sync(
                "Create a plan for 'samtools' version 1.19."
            )
        plan = result.output
        assert isinstance(plan.module_name, str)
        assert isinstance(plan.description, str)
        assert isinstance(plan.docker_image_tag, str)
        assert isinstance(plan.wrapper_script, str)
        assert isinstance(plan.command_line, str)
        assert isinstance(plan.parameters, list)

    def test_usage_is_accessible(self):
        m = TestModel()
        with planner_agent.override(model=m):
            result = planner_agent.run_sync("Plan a module for bwa.")
        assert result.usage is not None

    def test_model_receives_registered_tools(self):
        """TestModel must be offered all registered planner tools in the request."""
        m = TestModel()
        with planner_agent.override(model=m):
            planner_agent.run_sync("Plan a module for STAR aligner version 2.7.")
        model_tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
        registered = set(planner_agent._function_toolset.tools.keys())
        assert registered == model_tool_names





