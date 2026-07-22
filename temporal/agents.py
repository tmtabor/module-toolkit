"""
TemporalAgent wrappers for the pipeline's plain Pydantic AI agents.

Wraps each of the 8 agents the workflow invokes durably (unchanged, defined in
their own modules) with `TemporalAgent`, giving each a stable, permanent
`name=` — Temporal keys each agent's model-request/tool-call activities by this
name, so once a workflow has run in production these names must not change.

Note the dockerfile *hint-mapping* agent (`dockerfile.runtime._hint_mapping_agent`)
is intentionally NOT wrapped here: its LLM call runs inside the coarse
`build_dockerfile_runtime_command` activity (temporal/activities.py) as a plain,
non-durable call, together with that function's manifest parsing and file reads.
That's an accepted tradeoff (see temporal/PHASE3.md Step 3.4 / PHASE3.5.md M5) —
wrapping it as a TemporalAgent here would only register activities nothing ever
invokes.

This module only *constructs* wrapper objects (no I/O, no event loop), so it
is safe to import from `temporal/workflow.py`, which Temporal's sandbox
re-imports to validate workflow determinism (see temporal/PHASE3.md Step 3.0).
"""
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.durable_exec.temporal import TemporalAgent
from pydantic_ai.durable_exec.temporal._run_context import TemporalRunContext

from agents.researcher import researcher_agent as _researcher_agent
from agents.planner import planner_agent as _planner_agent
from wrapper.agent import wrapper_agent as _wrapper_agent
from manifest.agent import manifest_agent as _manifest_agent
from paramgroups.agent import paramgroups_agent as _paramgroups_agent
from gpunit.agent import gpunit_agent as _gpunit_agent
from documentation.agent import documentation_agent as _documentation_agent
from dockerfile.agent import dockerfile_agent as _dockerfile_agent

class _GuardedRunContext(TemporalRunContext[Any]):
    """TemporalRunContext extended with `completed_tool_names`, for
    agents.models.guard_single_call (temporal/PHASE5.md Workstream C3).

    guard_single_call scans `context.messages` for prior successful calls to
    the current tool -- that works for the plain RunContext the --legacy path
    and tests use, but TemporalRunContext doesn't serialize `messages` across
    the activity boundary by default (tools run in a Temporal *activity*,
    reconstructed from a serialized dict, not the live in-workflow object),
    so accessing it there raises `UserError`. Shipping the *full* message
    history to fix that would reintroduce the payload-size problem Workstream
    A just fixed, so this computes and serializes only the small derived fact
    guard_single_call actually needs: which tool names have already returned
    successfully in this run.
    """

    @classmethod
    def serialize_run_context(cls, ctx: RunContext[Any]) -> dict[str, Any]:
        data = super().serialize_run_context(ctx)
        data['completed_tool_names'] = sorted({
            part.tool_name
            for message in ctx.messages
            for part in getattr(message, 'parts', [])
            if type(part).__name__ == 'ToolReturnPart'
        })
        return data


# Stable, permanent activity-naming contract -- do not rename once used in
# production; Temporal identifies each agent's activities by this name.
# run_context_type=_GuardedRunContext is only needed on agents whose tools use
# guard_single_call (researcher, wrapper, manifest, paramgroups, gpunit,
# documentation) -- planner and dockerfile don't use it.
researcher_agent = TemporalAgent(_researcher_agent, name='researcher', run_context_type=_GuardedRunContext)
planner_agent = TemporalAgent(_planner_agent, name='planner')
wrapper_agent = TemporalAgent(_wrapper_agent, name='wrapper', run_context_type=_GuardedRunContext)
manifest_agent = TemporalAgent(_manifest_agent, name='manifest', run_context_type=_GuardedRunContext)
paramgroups_agent = TemporalAgent(_paramgroups_agent, name='paramgroups', run_context_type=_GuardedRunContext)
gpunit_agent = TemporalAgent(_gpunit_agent, name='gpunit', run_context_type=_GuardedRunContext)
documentation_agent = TemporalAgent(_documentation_agent, name='documentation', run_context_type=_GuardedRunContext)
dockerfile_agent = TemporalAgent(_dockerfile_agent, name='dockerfile')

# All 8 durably-invoked agents -- the canonical list for __pydantic_ai_agents__
# on the workflow class and for registering activities on a worker.
ALL_TEMPORAL_AGENTS = [
    researcher_agent,
    planner_agent,
    wrapper_agent,
    manifest_agent,
    paramgroups_agent,
    gpunit_agent,
    documentation_agent,
    dockerfile_agent,
]

# Maps the artifact_agents key used by ModuleAgent (agents/module.py) to its
# TemporalAgent, so the workflow can look agents up by artifact name exactly
# like the legacy orchestrator does.
ARTIFACT_TEMPORAL_AGENTS = {
    'wrapper': wrapper_agent,
    'manifest': manifest_agent,
    'paramgroups': paramgroups_agent,
    'gpunit': gpunit_agent,
    'documentation': documentation_agent,
    'dockerfile': dockerfile_agent,
}
