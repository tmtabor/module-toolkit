"""
Unit tests for the six artifact agents using Pydantic AI TestModel.

Agents under test:
  wrapper_agent, manifest_agent, paramgroups_agent,
  gpunit_agent, documentation_agent, dockerfile_agent

These tests verify:
- Each agent is importable and has an identifiable system prompt / instructions
- Each agent registers its expected validation tool
- Each agent can complete a run under TestModel
- The result carries a non-empty output
- Tool call assertions match the agent's registered tools
"""
import pytest
from pydantic_ai.models.test import TestModel

from agents.models import ArtifactModel
from manifest.models import ManifestModel
from paramgroups.models import ParamgroupsModel

from wrapper.agent import wrapper_agent
from manifest.agent import manifest_agent
from paramgroups.agent import paramgroups_agent
from gpunit.agent import gpunit_agent
from documentation.agent import documentation_agent
from dockerfile.agent import dockerfile_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_names(agent):
    # Pydantic AI stores registered @agent.tool functions in
    # _function_toolset.tools — a dict keyed by tool name.
    return set(agent._function_toolset.tools.keys())


# ---------------------------------------------------------------------------
# Parametrized agent catalogue
# ---------------------------------------------------------------------------

ARTIFACT_AGENTS = [
    pytest.param(
        wrapper_agent,
        ArtifactModel,
        {"validate_wrapper", "create_wrapper"},
        id="wrapper",
    ),
    pytest.param(
        manifest_agent,
        ManifestModel,
        {"validate_manifest", "create_manifest"},
        id="manifest",
    ),
    pytest.param(
        paramgroups_agent,
        ParamgroupsModel,
        {"validate_paramgroups", "create_paramgroups"},
        id="paramgroups",
    ),
    pytest.param(
        gpunit_agent,
        ArtifactModel,
        {"validate_gpunit", "create_gpunit"},
        id="gpunit",
    ),
    pytest.param(
        documentation_agent,
        ArtifactModel,
        {"validate_documentation", "create_documentation"},
        id="documentation",
    ),
    pytest.param(
        dockerfile_agent,
        ArtifactModel,
        {"validate_dockerfile", "create_dockerfile"},
        id="dockerfile",
    ),
]


# ---------------------------------------------------------------------------
# Structure tests (no LLM call required)
# ---------------------------------------------------------------------------

class TestArtifactAgentStructure:

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_agent_is_importable(self, agent, model_class, expected_tools):
        assert agent is not None

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_expected_tools_registered(self, agent, model_class, expected_tools):
        """Every artifact agent must expose at least its create + validate tool."""
        registered = _tool_names(agent)
        missing = expected_tools - registered
        assert not missing, (
            f"{agent.__class__.__name__}: missing tools {missing}. "
            f"Registered: {registered}"
        )

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_output_type_declared(self, agent, model_class, expected_tools):
        """
        Each artifact agent must declare its output_type at construction time
        (Refactor #2). The type must match the expected model class for that agent.
        """
        assert agent.output_type is model_class, (
            f"{agent} has output_type={agent.output_type!r}, "
            f"expected {model_class.__name__}."
        )


# ---------------------------------------------------------------------------
# Behavioural tests with TestModel
# ---------------------------------------------------------------------------

class TestArtifactAgentBehaviour:

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_run_sync_completes(self, agent, model_class, expected_tools, sample_deps_context):
        """Each agent should complete a run without raising."""
        m = TestModel()
        prompt = (
            f"Generate the artifact for GenePattern module 'TestTool'. "
            f"Call the create tool with the provided parameters."
        )
        with agent.override(model=m):
            result = agent.run_sync(
                prompt,
                deps=sample_deps_context,
            )
        assert result is not None

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_run_sync_output_is_non_empty(self, agent, model_class, expected_tools, sample_deps_context):
        """Output must be a non-None object (TestModel synthesises the declared type)."""
        m = TestModel()
        with agent.override(model=m):
            result = agent.run_sync(
                "Generate artifact for TestTool.",
                deps=sample_deps_context,
            )
        assert result.output is not None

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_usage_is_accessible(self, agent, model_class, expected_tools, sample_deps_context):
        """result.usage() must not raise and must return a usage object."""
        m = TestModel()
        with agent.override(model=m):
            result = agent.run_sync(
                "Generate artifact.",
                deps=sample_deps_context,
            )
        assert result.usage() is not None

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_model_receives_registered_tools(self, agent, model_class, expected_tools, sample_deps_context):
        """
        TestModel should expose the agent's registered tools in the model request.
        ModelRequestParameters.function_tools contains the tool schemas passed to the model.
        """
        m = TestModel()
        with agent.override(model=m):
            agent.run_sync(
                "Generate artifact for TestTool.",
                deps=sample_deps_context,
            )
        registered_tool_names = set(agent._function_toolset.tools.keys())
        model_tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
        # Every registered tool must have been offered to the model
        assert registered_tool_names == model_tool_names, (
            f"Model saw tools {model_tool_names}, expected {registered_tool_names}"
        )

    @pytest.mark.parametrize("agent,model_class,expected_tools", ARTIFACT_AGENTS)
    def test_instructions_callback_injects_tool_name(self, agent, model_class, expected_tools, sample_deps_context):
        """
        The @agent.instructions callback must inject the tool name into the system
        instructions seen by the model (Refactor 5).
        instruction_parts on last_model_request_parameters holds InstructionPart
        objects whose .content is the assembled system prompt text.
        """
        m = TestModel()
        with agent.override(model=m):
            agent.run_sync(
                "Generate artifact for TestTool.",
                deps=sample_deps_context,
            )

        instruction_text = " ".join(
            part.content
            for part in m.last_model_request_parameters.instruction_parts
            if isinstance(getattr(part, "content", None), str)
        )

        assert "TestTool" in instruction_text, (
            f"Expected 'TestTool' in instruction_parts but got: {instruction_text[:500]!r}"
        )

