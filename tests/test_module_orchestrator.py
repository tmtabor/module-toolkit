"""
Unit tests for the ModuleAgent orchestrator (agents/module.py).

These tests stub out the sub-agents with TestModel so that no real LLM calls
are made.  They verify:
- do_research() calls researcher_agent and updates status
- do_planning() calls planner_agent and returns a ModulePlan
- artifact_creation_loop() writes the artifact to disk and returns ArtifactResult
- save_status() / load_status() round-trip correctly
- generate_all_artifacts() marks an artifact as validated when the loop succeeds
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic_ai.models.test import TestModel

from agents.models import ArtifactModel, ModulePlan
from agents.module import ModuleAgent
from agents.status import ArtifactResult, ModuleGenerationStatus
from agents.logger import Logger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent(tmp_path):
    """Return a ModuleAgent writing to a temp directory."""
    return ModuleAgent(logger=Logger(), output_dir=str(tmp_path))


@pytest.fixture
def module_path(tmp_path):
    p = tmp_path / "testtool_20260101_000000"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# do_research()
# ---------------------------------------------------------------------------

class TestDoResearch:

    def test_returns_true_on_success(self, agent, sample_tool_info, sample_status):
        with patch("agents.module.researcher_agent") as mock_ra:
            mock_result = MagicMock()
            mock_result.output = "TestTool is a great bioinformatics tool."
            mock_result.usage.return_value = MagicMock(input_tokens=10, output_tokens=20)
            mock_ra.run_sync.return_value = mock_result

            success, data = agent.do_research(sample_tool_info, sample_status)

        assert success is True
        assert "research" in data
        assert "TestTool" in data["research"]

    def test_updates_status_tokens(self, agent, sample_tool_info, sample_status):
        with patch("agents.module.researcher_agent") as mock_ra:
            mock_result = MagicMock()
            mock_result.output = "Research output."
            mock_usage = MagicMock()
            mock_usage.input_tokens = 100
            mock_usage.output_tokens = 50
            mock_result.usage.return_value = mock_usage
            mock_ra.run_sync.return_value = mock_result

            agent.do_research(sample_tool_info, sample_status)

        assert sample_status.input_tokens == 100
        assert sample_status.output_tokens == 50

    def test_returns_false_on_exception(self, agent, sample_tool_info, sample_status):
        with patch("agents.module.researcher_agent") as mock_ra:
            mock_ra.run_sync.side_effect = RuntimeError("API down")

            success, data = agent.do_research(sample_tool_info, sample_status)

        assert success is False
        assert "error" in data

    def test_tool_name_appears_in_prompt(self, agent, sample_tool_info, sample_status):
        """The prompt passed to researcher_agent must include the tool name."""
        with patch("agents.module.researcher_agent") as mock_ra:
            mock_result = MagicMock()
            mock_result.output = "done"
            mock_result.usage.return_value = MagicMock(input_tokens=0, output_tokens=0)
            mock_ra.run_sync.return_value = mock_result

            agent.do_research(sample_tool_info, sample_status)

        call_args = mock_ra.run_sync.call_args
        prompt = call_args[0][0]
        assert "TestTool" in prompt


# ---------------------------------------------------------------------------
# do_planning()
# ---------------------------------------------------------------------------

class TestDoPlanning:

    def test_returns_true_and_plan_on_success(self, agent, sample_tool_info, sample_plan, sample_status, module_path):
        with patch("agents.module.planner_agent") as mock_pa:
            mock_result = MagicMock()
            mock_result.output = sample_plan
            mock_result.usage.return_value = MagicMock(input_tokens=10, output_tokens=20)
            mock_pa.run_sync.return_value = mock_result

            success, plan = agent.do_planning(
                sample_tool_info,
                {"research": "tool research data"},
                sample_status,
                module_path=module_path,
            )

        assert success is True
        assert isinstance(plan, ModulePlan)
        assert plan.module_name == "TestTool"

    def test_returns_false_on_exception(self, agent, sample_tool_info, sample_status, module_path):
        with patch("agents.module.planner_agent") as mock_pa:
            mock_pa.run_sync.side_effect = ValueError("bad plan")

            success, plan = agent.do_planning(
                sample_tool_info,
                {},
                sample_status,
                module_path=module_path,
            )

        assert success is False
        assert plan is None

    def test_updates_status_tokens(self, agent, sample_tool_info, sample_plan, sample_status, module_path):
        with patch("agents.module.planner_agent") as mock_pa:
            mock_result = MagicMock()
            mock_result.output = sample_plan
            mock_usage = MagicMock()
            mock_usage.input_tokens = 200
            mock_usage.output_tokens = 80
            mock_result.usage.return_value = mock_usage
            mock_pa.run_sync.return_value = mock_result

            agent.do_planning(
                sample_tool_info, {}, sample_status, module_path=module_path
            )

        assert sample_status.input_tokens == 200
        assert sample_status.output_tokens == 80

    def test_saves_plan_jsonl(self, agent, sample_tool_info, sample_plan, sample_status, module_path):
        """do_planning should write plan.jsonl when module_path is provided."""
        with patch("agents.module.planner_agent") as mock_pa:
            mock_result = MagicMock()
            mock_result.output = sample_plan
            mock_result.usage.return_value = MagicMock(input_tokens=0, output_tokens=0)
            mock_pa.run_sync.return_value = mock_result

            agent.do_planning(
                sample_tool_info, {}, sample_status, module_path=module_path
            )

        assert (module_path / "plan.jsonl").exists()


# ---------------------------------------------------------------------------
# artifact_creation_loop()
# ---------------------------------------------------------------------------

class TestArtifactCreationLoop:

    def _make_mock_result(self, artifact_model):
        mock_result = MagicMock()
        mock_result.output = artifact_model
        mock_result.usage.return_value = MagicMock(input_tokens=5, output_tokens=10)
        return mock_result

    def test_success_path_writes_file(
        self, agent, sample_tool_info, sample_plan, sample_status, sample_artifact_model, module_path
    ):
        """A successful agent run should write the artifact file to disk."""
        mock_result = self._make_mock_result(sample_artifact_model)

        with patch.object(
            agent.artifact_agents["documentation"]["agent"],
            "run_sync",
            return_value=mock_result,
        ):
            # Stub out the validator so we don't run the real linter
            with patch.object(agent, "validate_artifact", return_value={"success": True}):
                result = agent.artifact_creation_loop(
                    "documentation",
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    max_loops=1,
                )

        assert result.success is True
        assert result.artifact_name == "documentation"
        assert (module_path / "README.md").exists()

    def test_validation_failure_returns_failed_result(
        self, agent, sample_tool_info, sample_plan, sample_status, sample_artifact_model, module_path
    ):
        """When validation fails every attempt, the loop returns success=False."""
        mock_result = self._make_mock_result(sample_artifact_model)

        with patch.object(
            agent.artifact_agents["documentation"]["agent"],
            "run_sync",
            return_value=mock_result,
        ):
            with patch.object(
                agent,
                "validate_artifact",
                return_value={"success": False, "error": "linter error"},
            ):
                result = agent.artifact_creation_loop(
                    "documentation",
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    max_loops=2,
                )

        assert result.success is False
        assert result.artifact_name == "documentation"

    def test_exception_in_agent_returns_failed_result(
        self, agent, sample_tool_info, sample_plan, sample_status, module_path
    ):
        with patch.object(
            agent.artifact_agents["gpunit"]["agent"],
            "run_sync",
            side_effect=RuntimeError("model error"),
        ):
            result = agent.artifact_creation_loop(
                "gpunit",
                sample_tool_info,
                sample_plan,
                module_path,
                sample_status,
                max_loops=1,
            )

        assert result.success is False
        assert "model error" in result.error_text

    def test_status_tracks_attempts(
        self, agent, sample_tool_info, sample_plan, sample_status, sample_artifact_model, module_path
    ):
        mock_result = self._make_mock_result(sample_artifact_model)

        with patch.object(
            agent.artifact_agents["gpunit"]["agent"],
            "run_sync",
            return_value=mock_result,
        ):
            with patch.object(agent, "validate_artifact", return_value={"success": True}):
                agent.artifact_creation_loop(
                    "gpunit",
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    max_loops=3,
                )

        assert sample_status.artifacts_status["gpunit"]["attempts"] == 1

    def test_error_history_accumulated_across_attempts(
        self, agent, sample_tool_info, sample_plan, sample_status, sample_artifact_model, module_path
    ):
        """Validation errors must accumulate in status.artifacts_status[name]['errors']."""
        mock_result = self._make_mock_result(sample_artifact_model)

        with patch.object(
            agent.artifact_agents["documentation"]["agent"],
            "run_sync",
            return_value=mock_result,
        ):
            with patch.object(
                agent,
                "validate_artifact",
                return_value={"success": False, "error": "lint fail"},
            ):
                agent.artifact_creation_loop(
                    "documentation",
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    max_loops=2,
                )

        errors = sample_status.artifacts_status["documentation"]["errors"]
        assert len(errors) == 2  # one per failed attempt


# ---------------------------------------------------------------------------
# save_status() / load_status()
# ---------------------------------------------------------------------------

class TestStatusPersistence:

    def test_save_and_load_round_trip(self, agent, sample_status, module_path):
        sample_status.module_directory = str(module_path)
        agent.save_status(sample_status)

        loaded = agent.load_status(str(module_path))

        assert loaded.tool_name == sample_status.tool_name
        assert loaded.module_directory == sample_status.module_directory

    def test_save_creates_status_json(self, agent, sample_status, module_path):
        sample_status.module_directory = str(module_path)
        agent.save_status(sample_status)
        assert (module_path / "status.json").exists()

    def test_load_missing_raises(self, agent, module_path):
        with pytest.raises(FileNotFoundError):
            agent.load_status(str(module_path))

    def test_loaded_status_preserves_planning_data(self, agent, sample_status, sample_plan, module_path):
        sample_status.module_directory = str(module_path)
        agent.save_status(sample_status)

        loaded = agent.load_status(str(module_path))

        assert loaded.planning_complete is True
        assert loaded.planning_data.module_name == "TestTool"

    def test_loaded_status_preserves_token_counts(self, agent, sample_status, module_path):
        sample_status.module_directory = str(module_path)
        sample_status.input_tokens = 1234
        sample_status.output_tokens = 567
        agent.save_status(sample_status)

        loaded = agent.load_status(str(module_path))

        assert loaded.input_tokens == 1234
        assert loaded.output_tokens == 567


# ---------------------------------------------------------------------------
# generate_all_artifacts() — high-level orchestration
# ---------------------------------------------------------------------------

class TestGenerateAllArtifacts:

    def _stub_artifact_loop(self, agent, success=True):
        """Patch artifact_creation_loop to always return success/failure."""
        return_val = ArtifactResult(success=success, artifact_name="stub")
        return patch.object(agent, "artifact_creation_loop", return_value=return_val)

    def test_returns_true_when_all_succeed(
        self, agent, sample_tool_info, sample_plan, sample_status, module_path
    ):
        sample_status.module_directory = str(module_path)
        with self._stub_artifact_loop(agent, success=True):
            # Also stub _run_install_artifact
            with patch.object(
                agent, "_run_install_artifact",
                return_value=ArtifactResult(success=True, artifact_name="install"),
            ):
                ok = agent.generate_all_artifacts(
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    no_zip=True,
                )
        assert ok is True

    def test_returns_false_and_aborts_on_failure(
        self, agent, sample_tool_info, sample_plan, sample_status, module_path
    ):
        sample_status.module_directory = str(module_path)
        with self._stub_artifact_loop(agent, success=False):
            ok = agent.generate_all_artifacts(
                sample_tool_info,
                sample_plan,
                module_path,
                sample_status,
                no_zip=True,
            )
        assert ok is False

    def test_skips_already_validated_artifacts(
        self, agent, sample_tool_info, sample_plan, sample_status, module_path
    ):
        """If an artifact is already validated in status, it must not be re-generated."""
        sample_status.module_directory = str(module_path)
        sample_status.artifacts_status["wrapper"] = {
            "generated": True, "validated": True, "attempts": 1, "errors": []
        }
        with self._stub_artifact_loop(agent, success=True) as mock_loop:
            with patch.object(
                agent, "_run_install_artifact",
                return_value=ArtifactResult(success=True, artifact_name="install"),
            ):
                agent.generate_all_artifacts(
                    sample_tool_info,
                    sample_plan,
                    module_path,
                    sample_status,
                    no_zip=True,
                )

        # Check that the wrapper artifact was NOT in any call
        called_artifact_names = [
            call.args[0] for call in mock_loop.call_args_list
        ]
        assert "wrapper" not in called_artifact_names

