"""
Shared fixtures for the Pydantic AI agent test suite.

All fixtures here are available to every test file under tests/.
They provide canonical sample data, TestModel factories, and pre-built
status/planning objects so individual test files stay focused.
"""
import pytest
from pydantic_ai.models.test import TestModel

from agents.models import (
    ArtifactModel,
    ModulePlan,
    Parameter,
    ParameterType,
    ValueCount,
)
from agents.example_data import ExampleDataItem
from agents.status import ArtifactResult, ModuleGenerationStatus


# ---------------------------------------------------------------------------
# Canonical sample data
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_tool_info():
    """Minimal tool_info dict that mirrors what generate-module.py produces."""
    return {
        "name": "TestTool",
        "version": "1.0",
        "language": "python",
        "description": "A test bioinformatics tool",
        "repository_url": "https://github.com/example/testtool",
        "documentation_url": "https://example.com/docs",
        "instructions": "",
        "base_image": "",
        "example_data": [],
    }


@pytest.fixture
def sample_parameter():
    """A minimal Parameter model for use in plans and prompts."""
    return Parameter(
        name="input.file",
        description="Input file for analysis",
        required=True,
        type=ParameterType.FILE,
        value_count=ValueCount.ONE,
        default_value=None,
        file_formats=["bam", "sam"],
        choices=None,
        accept_user_values=None,
        prefix="--input.file",
        prefix_only_if_value=False,
    )


@pytest.fixture
def sample_plan(sample_parameter):
    """A minimal ModulePlan matching what planner_agent produces."""
    return ModulePlan(
        module_name="TestTool",
        description="A test bioinformatics tool for GenePattern",
        author="GenePattern Team",
        input_file_formats=["bam", "sam"],
        language="python",
        categories=["Sequence Analysis"],
        cpu_cores=4,
        memory="8GB",
        lsid="urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:12345:1",
        plan="Detailed plan for TestTool module",
        wrapper_script="wrapper.py",
        command_line="python <libdir>wrapper.py --input.file <input.file>",
        parameters=[sample_parameter],
        docker_image_tag="genepattern/testtool:1",
    )


@pytest.fixture
def sample_deps_context(sample_tool_info, sample_plan):
    """The deps dict that module.py passes to every artifact agent run_sync call."""
    return {
        "tool_info": sample_tool_info,
        "planning_data": sample_plan.model_dump(mode="json"),
        "error_report": "",
        "attempt": 1,
    }


@pytest.fixture
def sample_artifact_model():
    """A minimal ArtifactModel representing a successfully generated artifact."""
    return ArtifactModel(
        code="# generated code",
        artifact_report="All checks passed.",
        artifact_status="success",
        meta={},
    )


@pytest.fixture
def sample_status(sample_plan, tmp_path):
    """A ModuleGenerationStatus pre-populated through the planning phase."""
    status = ModuleGenerationStatus(
        tool_name="TestTool",
        module_directory=str(tmp_path),
        research_data={"research": "TestTool is a great tool."},
        planning_data=sample_plan,
        example_data=[],
    )
    return status


@pytest.fixture
def test_model():
    """Return a fresh TestModel instance."""
    return TestModel()

