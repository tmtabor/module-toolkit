"""
Live smoke test for the researcher agent's web-search behaviour.

Bedrock does not support Pydantic AI's native WebSearchTool builtin, and the
DuckDuckGo local fallback hangs indefinitely due to the primp HTTP client.
Neither WebSearch(builtin=True) nor WebSearch(local=False) is safe to use
with Bedrock — so the researcher agent runs with no WebSearch capability and
relies on its own tools (parse_repository_info, analyze_tool_documentation)
to fetch real content from known URLs.

What this suite verifies against the live model:
1. The researcher agent completes a run without hanging or erroring.
2. The output is a non-empty string.
3. Token usage is accessible after the run.

Run with:
    pytest tests/test_websearch_live.py -v -m live

Excluded from default runs — requires live AWS Bedrock credentials and
incurs API cost.
"""
import os
import pytest
from dotenv import load_dotenv

# Load .env before any model construction so DEFAULT_LLM_MODEL is resolved.
load_dotenv()

from agents.researcher import researcher_agent  # noqa: E402

pytestmark = pytest.mark.live   # opt-in marker — not run by default


class TestResearcherAgentLive:
    """Smoke tests that run researcher_agent against the real configured model."""

    def test_researcher_completes_without_error(self):
        """A basic research prompt must complete and return a string."""
        result = researcher_agent.run_sync(
            "Research the bioinformatics tool 'samtools'. "
            "Provide a brief summary of what it does."
        )
        assert isinstance(result.output, str), "Expected string output"
        assert len(result.output) > 20,        "Expected non-trivial response"
        print(f"\n✅ Researcher result (truncated):\n{result.output[:500]}")

    def test_researcher_usage_is_accessible(self):
        """Token usage statistics must be available after a live run."""
        result = researcher_agent.run_sync(
            "What file formats does samtools support?"
        )
        usage = result.usage()
        assert usage is not None
        print(f"\n✅ Usage stats: {usage}")

    def test_researcher_with_repository_url(self):
        """Researcher must handle a prompt that includes a real repository URL."""
        result = researcher_agent.run_sync(
            "Research the bioinformatics tool 'celltypist'. "
            "Repository: https://github.com/Teichlab/celltypist. "
            "Summarise its purpose and key parameters."
        )
        assert isinstance(result.output, str)
        assert len(result.output) > 20
        lowered = result.output.lower()
        assert any(term in lowered for term in ["cell", "type", "annotati", "classif"]), (
            f"Expected cell-type-related content in response, got:\n{result.output[:300]}"
        )
        print(f"\n✅ Repository research result (truncated):\n{result.output[:500]}")
