"""
Tests for agents.models.guard_single_call (temporal/PHASE5.md Workstream C3):
the deterministic backstop against a model re-calling a "terminal" tool
(create_wrapper, create_manifest, ...) after it already returned its final
answer.

Uses a FunctionModel to force a real second tool call deterministically,
rather than hoping TestModel happens to reproduce the behavior.
"""
import pytest
from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agents.models import guard_single_call


def _find_retry_prompt(messages, tool_name: str) -> str | None:
    for message in messages:
        for part in getattr(message, 'parts', []):
            if type(part).__name__ == 'RetryPromptPart' and getattr(part, 'tool_name', None) == tool_name:
                return part.content if isinstance(part.content, str) else str(part.content)
    return None


class TestGuardSingleCall:

    def test_second_call_is_blocked_with_a_model_retry(self):
        """Two deliberate calls to the same guarded tool: the first succeeds,
        the second must raise ModelRetry (visible as a RetryPromptPart) and
        the run must recover by finishing with the model's next response,
        not by re-running the guarded tool a second time."""
        call_count = {'n': 0}

        def controller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            call_count['n'] += 1
            if call_count['n'] <= 2:
                return ModelResponse(parts=[ToolCallPart(tool_name='guarded_tool', args={})])
            return ModelResponse(parts=[TextPart(content='done')])

        agent = Agent(FunctionModel(controller), retries=3)
        tool_invocations = {'n': 0}

        @agent.tool
        @guard_single_call
        def guarded_tool(context: RunContext[None]) -> str:
            tool_invocations['n'] += 1
            return 'final content'

        with capture_run_messages() as messages:
            result = agent.run_sync('go')

        assert result.output == 'done'
        # The tool's actual body ran exactly once -- the second "call" was
        # intercepted by the guard before reaching it.
        assert tool_invocations['n'] == 1

        retry_content = _find_retry_prompt(messages, 'guarded_tool')
        assert retry_content is not None
        assert 'already called guarded_tool' in retry_content
        assert 'Do NOT call guarded_tool again' in retry_content

    def test_validation_retry_does_not_trip_the_guard(self):
        """A tool call that fails ARGUMENT validation and gets retried by
        pydantic-ai itself must not look like "already called" to the guard
        -- that was the actual bug the first version of this guard had (see
        agents/models.py's docstring): counting every ToolCallPart (attempts,
        including failed ones) instead of only ToolReturnPart (successes)
        made the guard fire on the very next attempt and exhaust the retry
        budget on tools that had never successfully returned even once."""
        call_count = {'n': 0}

        def controller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            call_count['n'] += 1
            if call_count['n'] == 1:
                # Bad args: `count` should be an int. Triggers a pydantic-ai
                # argument-validation retry, NOT the guard (the tool body
                # never runs, so no ToolReturnPart is ever recorded for it).
                return ModelResponse(parts=[ToolCallPart(tool_name='guarded_tool_2', args={'count': 'not-an-int'})])
            if call_count['n'] == 2:
                return ModelResponse(parts=[ToolCallPart(tool_name='guarded_tool_2', args={'count': 1})])
            return ModelResponse(parts=[TextPart(content='done')])

        agent = Agent(FunctionModel(controller), retries=3)
        tool_invocations = {'n': 0}

        @agent.tool
        @guard_single_call
        def guarded_tool_2(context: RunContext[None], count: int) -> str:
            tool_invocations['n'] += 1
            return f'final content {count}'

        result = agent.run_sync('go')

        assert result.output == 'done'
        # The first (invalid-args) attempt never reached the tool body; the
        # second (valid) attempt is the guard's first legitimate success.
        assert tool_invocations['n'] == 1
