"""
Status tracking for the module generation pipeline.

ModuleGenerationStatus is the central state object threaded through the
entire pipeline.  It holds research/planning results, per-artifact progress,
token usage, and escalation history, and can be serialised to / from JSON
via Pydantic's model_dump() / model_validate().

ArtifactResult is the structured return value from artifact_creation_loop.
"""
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agents.example_data import ExampleDataItem

# Token cost configuration (cost per 1000 tokens)
INPUT_TOKEN_COST_PER_1000 = float(os.getenv('INPUT_TOKEN_COST_PER_1000', '0.003'))
OUTPUT_TOKEN_COST_PER_1000 = float(os.getenv('OUTPUT_TOKEN_COST_PER_1000', '0.015'))


class ArtifactResult(BaseModel):
    """Structured result from an artifact_creation_loop call."""
    success: bool
    artifact_name: str
    error_text: str = ""
    # populated when classification is available; typed Any to avoid hard import
    root_cause: Optional[Any] = None

    model_config = {"arbitrary_types_allowed": True}


class ModuleGenerationStatus(BaseModel):
    """Track the status of module generation process."""
    tool_name: str
    module_directory: str
    research_data: Dict[str, Any] = Field(default_factory=dict)
    # ModulePlan is set at runtime; typed as Any to avoid a hard import cycle
    planning_data: Optional[Any] = None
    artifacts_status: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    error_messages: List[str] = Field(default_factory=list)
    example_data: List[ExampleDataItem] = Field(default_factory=list)
    # Token tracking fields
    input_tokens: int = 0
    output_tokens: int = 0
    # Cross-artifact escalation tracking: artifact_name -> count
    escalation_counts: Dict[str, int] = Field(default_factory=dict)
    # Log of escalation events for debugging / reporting
    escalation_log: List[Dict[str, str]] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def research_complete(self) -> bool:
        """Return True if research data is present."""
        return bool(self.research_data)

    @property
    def planning_complete(self) -> bool:
        """Return True if planning data is present."""
        return self.planning_data is not None

    @property
    def parameters(self):
        """Return parameters from planning_data if available."""
        return self.planning_data.parameters if self.planning_data else []

    def add_usage(self, result) -> None:
        """Add token usage from an agent result to the running totals."""
        try:
            usage = result.usage()
            if usage:
                self.input_tokens += usage.input_tokens or 0
                self.output_tokens += usage.output_tokens or 0
        except Exception:
            pass

    def get_estimated_cost(self) -> float:
        """Calculate estimated cost based on token usage."""
        input_cost = (self.input_tokens / 1000) * INPUT_TOKEN_COST_PER_1000
        output_cost = (self.output_tokens / 1000) * OUTPUT_TOKEN_COST_PER_1000
        return input_cost + output_cost

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise status to a JSON-serialisable dictionary.
        Delegates to model_dump() and adds derived/computed fields.
        """
        data = self.model_dump(
            mode='json',
            exclude={'planning_data', 'example_data'},
        )
        data['research_complete'] = self.research_complete
        data['planning_complete'] = self.planning_complete
        data['estimated_cost'] = self.get_estimated_cost()
        data['example_data'] = [item.to_dict() for item in (self.example_data or [])]
        if self.planning_data:
            data['planning_data'] = self.planning_data.model_dump(mode='json')
        else:
            data['planning_data'] = {}
        return data

