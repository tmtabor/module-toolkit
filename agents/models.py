import functools
import json
import os
from typing import Any, Dict, List, Optional, Literal
from enum import Enum
from json_repair import repair_json
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider


def _flatten_dict_list(obj: Any) -> Any:
    """Recursively flatten nested lists into one flat list, dropping non-dict noise.

    json_repair salvages a structurally-complete prefix, but when the source
    malformation was mis-nesting (not just truncation) that prefix can surface as
    a list of sublists / Nones instead of the flat list of dicts callers expect.
    Only lists are touched; dicts and scalars pass through untouched.
    """
    if not isinstance(obj, list):
        return obj
    flat: list = []
    for item in obj:
        if isinstance(item, list):
            flat.extend(_flatten_dict_list(item))
        elif isinstance(item, dict):
            flat.append(item)
    return flat if flat else obj


def coerce_stringified_json(v: Any) -> Any:
    """BeforeValidator for tool params typed List[Dict[...]] / Dict[str, ...].

    Some models -- observed reproducibly with a local Ollama model on large or
    deeply nested tool-call arguments -- emit a JSON-encoded STRING instead of
    a real array/object (e.g. {"research_findings": "[{...}, {...}]"} instead
    of {"research_findings": [{...}, {...}]}). pydantic-ai validates tool
    arguments against the function signature before the function body ever
    runs, so this rejected every attempt regardless of retry count (see
    temporal/PHASE4.md's parity-run notes). This transparently parses a
    string input back into the expected structure; well-formed calls (a real
    list/dict) pass through unchanged.

    Also observed: for very large stringified payloads the same local model
    sometimes truncates mid-generation, leaving unbalanced brackets that
    plain json.loads can't parse -- and every retry regenerates the whole
    blob, so it's just as likely to truncate again. json_repair salvages the
    structurally-complete prefix (closing whatever containers were left open)
    rather than losing the entire tool call to the retry budget.

    Usage: `Annotated[List[Dict[str, Any]], BeforeValidator(coerce_stringified_json)]`
    """
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            repaired = repair_json(v)
            parsed = json.loads(repaired)
            return _flatten_dict_list(parsed) if isinstance(parsed, list) else parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass  # let normal validation raise its own, more specific error
    return v


def guard_single_call(fn):
    """Decorator for a "terminal" @agent.tool -- one whose return value IS the
    artifact's finished content (create_wrapper, create_manifest,
    create_paramgroups, create_gpunit, create_documentation,
    create_tool_research_report), not an intermediate analysis step. If the
    model calls it again after already calling it once in this run, raise a
    sharp ModelRetry instead of silently regenerating the same content.

    A prompt-level "call this exactly once" instruction reduces but doesn't
    reliably prevent local models from looping on these tools -- observed
    three times independently in temporal/PHASE4.md's live debugging
    (gpunit, paramgroups, then researcher's create_tool_research_report),
    and recurring even on an already-fixed tool in a later run. Each loop
    burns real tokens/cost with no new information. This is the deterministic
    backstop temporal/PHASE5.md Workstream C3 calls for.

    Counts prior *successful* completions by scanning `context.messages` for
    ToolReturnPart entries matching this tool's name -- deliberately not
    ToolCallPart (every attempt, including ones that failed argument
    validation and were transparently retried by pydantic-ai itself, e.g. via
    coerce_stringified_json's retry path). Counting attempts instead of
    successes makes every retry-after-a-validation-failure look like "already
    called," so this guard would fire on the very next attempt and burn the
    whole retry budget -- exactly what broke `tests/test_researcher_agent.py`
    the first time this was written. Counting ToolReturnPart avoids that:
    only a call that actually finished counts. Doesn't rely on
    `context.deps` either (some agents, e.g. researcher_agent, don't have a
    deps object at all) -- `messages` is always populated by pydantic-ai
    regardless of deps_type, so this works uniformly across every agent
    running in-process (the --legacy path, and tests).

    Under Temporal, tools run in an *activity*, whose RunContext is
    reconstructed from a serialized dict that doesn't include `messages` by
    default -- accessing it raises `UserError` (found live: the very first
    Temporal-path run after this guard was added failed outright). Rather
    than ship the full message history across the activity boundary (which
    would reintroduce the payload-size problem Workstream A just fixed),
    `temporal/agents.py`'s `_GuardedRunContext` precomputes and serializes
    just `completed_tool_names` -- this checks for that first and only falls
    back to scanning `context.messages` when it isn't present.

    Usage: stack directly below the `@<agent>.tool` decorator so the tool
    registration sees this wrapper (functools.wraps keeps the original
    signature visible for schema generation):

        @wrapper_agent.tool
        @guard_single_call
        def create_wrapper(context: RunContext[ArtifactDeps]) -> str: ...
    """
    tool_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(context, *args, **kwargs):
        completed_tool_names = getattr(context, 'completed_tool_names', None)
        if completed_tool_names is not None:
            prior_calls = 1 if tool_name in completed_tool_names else 0
        else:
            prior_calls = sum(
                1
                for message in context.messages
                for part in getattr(message, 'parts', [])
                if type(part).__name__ == 'ToolReturnPart' and getattr(part, 'tool_name', None) == tool_name
            )
        if prior_calls >= 1:
            raise ModelRetry(
                f"You already called {tool_name} and received its output -- that output IS your "
                f"final answer. Do NOT call {tool_name} again to regenerate or double-check it; "
                f"respond with its previous return value instead of making another tool call."
            )
        return fn(context, *args, **kwargs)

    return wrapper


class ArtifactDeps(BaseModel):
    """Structured dependencies injected into every artifact agent via RunContext."""
    tool_info: Dict[str, Any] = Field(default_factory=dict)
    planning_data: Optional[Dict[str, Any]] = Field(default=None)
    error_report: str = Field(default="")
    attempt: int = Field(default=1)
    # Additional fields for @agent.instructions callbacks (Refactor 5)
    example_data: List[Dict[str, Any]] = Field(default_factory=list)
    downstream_error_context: str = Field(default="")
    error_history: List[str] = Field(default_factory=list)
    max_loops: int = Field(default=5)


def configured_llm_model():
    DEFAULT_LLM_MODEL = os.getenv('DEFAULT_LLM_MODEL', 'ollama:qwen3:8b')
    if DEFAULT_LLM_MODEL.startswith('ollama:'):
        OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434/v1')
        OLLAMA_MODEL_NAME = DEFAULT_LLM_MODEL[7:]
        ollama_provider = OllamaProvider(base_url=OLLAMA_BASE_URL)
        ollama_model = OpenAIChatModel(model_name=OLLAMA_MODEL_NAME, provider=ollama_provider)
        return ollama_model
    else: return DEFAULT_LLM_MODEL

# Pydantic models for structured planning output
class ParameterType(str, Enum):
    FILE = "file"
    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    CHOICE = "choice"


class ValueCount(str, Enum):
    ZERO_OR_ONE = "0..1"
    ONE = "1..1"
    ZERO_OR_MORE = "0+"
    ONE_OR_MORE = "1+"


class ChoiceOption(BaseModel):
    display: str = Field(description="Display value for the choice")
    value: str = Field(description="Actual value to use")


class Parameter(BaseModel):
    name: str = Field(description="Parameter name")
    description: str = Field(description="Parameter description")
    required: bool = Field(description="Whether the parameter is required")
    type: ParameterType = Field(description="Parameter type")
    value_count: ValueCount = Field(description="Number of values accepted")
    default_value: Optional[str] = Field(None, description="Default value if any")
    file_formats: Optional[List[str]] = Field(None, description="Accepted file formats for file parameters")
    choices: Optional[List[ChoiceOption]] = Field(None, description="Available choices for choice parameters")
    accept_user_values: Optional[bool] = Field(None, description="Whether to accept user supplied values for choice parameters")
    prefix: str = Field(description="Command line prefix")
    prefix_only_if_value: bool = Field(description="Only include prefix if a value is specified")


class ModulePlan(BaseModel):
    module_name: str = Field(description="Name of the module")
    description: str = Field(description="Description of what the module does")
    author: str = Field(description="Author or organization")
    input_file_formats: List[str] = Field(description="File formats accepted as input")
    language: str = Field(description="Programming language the tool is implemented in")
    categories: List[str] = Field(description="Categories for this module (preprocessing, clustering, etc.)")
    cpu_cores: int = Field(description="CPU core requirement for the tool")
    memory: str = Field(description="Memory requirement for the tool (e.g., '2GB')")
    lsid: str = Field(description="Life Science Identifier (LSID) for the module in format urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:<5-digit-id>:<version>")
    plan: str = Field(description="The full unstructured text of the plan")
    wrapper_script: str = Field(description="The name of the wrapper script")
    command_line: str = Field(description="Example command line for calling the wrapper script")
    parameters: List[Parameter] = Field(description="List of parameters to expose")
    docker_image_tag: str = Field(description="Docker image tag in format genepattern/<module_name>:<version> where module_name is lowercase alphanumeric only")


class ArtifactModel(BaseModel):
    """Model representing a generated artifact result"""
    code: str  # The generated artifact code
    artifact_report: str  # A report on the generated artifact's implementation and use
    artifact_status: Literal["success", "failure"]  # Status of the artifact generation
    meta: Dict[str, Any]  # Additional metadata about the generated artifact
