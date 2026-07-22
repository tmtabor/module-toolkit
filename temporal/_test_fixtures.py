"""
Test-only Temporal workflow fixture used by tests/test_workflow.py.

Lives inside temporal/ (not tests/) because Temporal's workflow sandbox
re-imports the module a @workflow.defn class lives in via its own import
machinery, keyed by dotted module name; `tests/` has no `__init__.py` (an
implicit namespace package), and when the full pytest suite is collected
(many test modules importing many things), the sandbox's reimport of
`tests.<this file>` intermittently raised `ModuleNotFoundError` even though
the module clearly existed and imported fine standalone -- a real,
reproduced interaction between namespace packages and Temporal's sandbox
importer, not a flake. Placing workflow-defining fixtures in a real package
(temporal/ has __init__.py) sidesteps it entirely. Not part of the public
temporal/ runtime API -- test-only, hence the leading underscore.

TemporalAgent.override(model=...) explicitly raises when called from inside a
running workflow ("Model cannot be contextually overridden inside a Temporal
workflow, it must be set at agent creation time" -- see
pydantic_ai/durable_exec/temporal/_agent.py). That means the production
ModuleGenerationWorkflow (whose agents wrap the real configured LLM model)
cannot be exercised end-to-end with TestModel via the normal override pattern.

MiniWorkflow exists to validate, with a real TestModel-backed agent, the exact
integration pattern ModuleGenerationWorkflow relies on: an agent call via
TemporalAgent.run() interleaved with a real agents.effects-derived activity
call via workflow.execute_activity(). It is intentionally small so it can be
driven fully deterministically for the crash-resume and replay tests.

Must stay side-effect-free at import time -- see temporal/workflow.py's
module docstring for why.
"""
from datetime import timedelta

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from temporalio import activity, workflow

# activities.py transitively imports agents.effects, which imports `requests`
# at module level; requests' http.client usage does not sandbox cleanly (see
# temporal/workflow.py's module docstring for the full explanation).
with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import TemporalAgent, PydanticAIWorkflow
    from temporal import activities

_plain_agent = Agent(TestModel(custom_output_text='mini workflow output'), name='mini')
mini_agent = TemporalAgent(_plain_agent, name='mini')


@activity.defn(name='test_sleep')
def sleep_activity(seconds: float) -> str:
    """A non-async activity that genuinely blocks, and never heartbeats --
    stands in for the docker build / large download (temporal/PHASE3.5.md H0)."""
    import time
    time.sleep(seconds)
    return 'slept'


@workflow.defn
class TimeoutPolicyWorkflow(PydanticAIWorkflow):
    """Runs sleep_activity with a configurable timeout policy so a test can pin
    the H0 fix: a long-but-progressing activity with NO heartbeat_timeout must
    complete, but declaring a heartbeat_timeout it never satisfies must fail."""

    __pydantic_ai_agents__ = []

    @workflow.run
    async def run(self, sleep_s: float, start_to_close_s: float, heartbeat_s: float) -> str:
        from temporalio.common import RetryPolicy
        kwargs = {
            'start_to_close_timeout': timedelta(seconds=start_to_close_s),
            'retry_policy': RetryPolicy(maximum_attempts=1),
        }
        if heartbeat_s > 0:
            kwargs['heartbeat_timeout'] = timedelta(seconds=heartbeat_s)
        return await workflow.execute_activity(sleep_activity, args=[sleep_s], **kwargs)


def _plain_blocking_sleep(seconds: float) -> str:
    import time
    time.sleep(seconds)
    return 'heartbeat-sleep-done'


# Wrapped with a short, test-friendly heartbeat interval via the real
# production _wrap_with_heartbeat (temporal/PHASE5.md Workstream C2) -- this
# exercises the actual wrapper, not a test-only reimplementation of it.
heartbeat_sleep_activity = activities._wrap_with_heartbeat(
    'test_heartbeat_sleep', _plain_blocking_sleep, heartbeat_interval_sec=0.3,
)


@workflow.defn
class HeartbeatWrapperWorkflow(PydanticAIWorkflow):
    """Runs heartbeat_sleep_activity under a heartbeat_timeout SHORTER than
    the activity's total duration, to prove the wrapper's real heartbeats
    keep it alive. Without real heartbeating, an activity that declares
    heartbeat_timeout but never calls activity.heartbeat() is killed at that
    timeout regardless of progress (temporal/PHASE3.5.md H0) -- this is that
    same failure mode, deliberately provoked, to prove the wrapper avoids it."""

    __pydantic_ai_agents__ = []

    @workflow.run
    async def run(self, sleep_s: float, heartbeat_timeout_s: float) -> str:
        return await workflow.execute_activity(
            heartbeat_sleep_activity,
            args=[sleep_s],
            start_to_close_timeout=timedelta(seconds=sleep_s + 30),
            heartbeat_timeout=timedelta(seconds=heartbeat_timeout_s),
        )


# Module-level counter: activities run in the worker's normal (non-sandboxed)
# process, so plain mutable state here is fine -- reset it at the start of
# each test that uses it, since it persists across tests within one pytest
# process.
_always_fails_call_count = {'n': 0}


@activity.defn(name='test_always_fails')
def always_failing_activity() -> str:
    """Activity that always raises, for testing that a RetryPolicy's
    maximum_attempts actually bounds Temporal's retries (temporal/PHASE5.md
    Workstream C1) rather than the default unbounded-until-execution_timeout
    behavior."""
    _always_fails_call_count['n'] += 1
    raise RuntimeError(f"deliberate failure, attempt {_always_fails_call_count['n']}")


@workflow.defn
class RetryPolicyWorkflow(PydanticAIWorkflow):
    """Runs always_failing_activity under a configurable RetryPolicy so a test
    can assert Temporal actually stops retrying at maximum_attempts instead of
    retrying forever (the default) or ignoring the policy."""

    __pydantic_ai_agents__ = []

    @workflow.run
    async def run(self, maximum_attempts: int) -> str:
        from temporalio.common import RetryPolicy
        await workflow.execute_activity(
            always_failing_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=10),
                backoff_coefficient=1.0,
                maximum_attempts=maximum_attempts,
            ),
        )
        return 'unreachable'  # always_failing_activity never returns normally


@workflow.defn
class ProgressQueryWorkflow(PydanticAIWorkflow):
    """Minimal workflow exposing a `progress` query with the same name/shape as
    ModuleGenerationWorkflow.progress, for exercising
    temporal.client.get_workflow_state's RUNNING/COMPLETED dispatch (temporal/
    PHASE4.md 4.4) without needing the full LLM pipeline. Temporal dispatches
    queries by name, so `handle.query(ModuleGenerationWorkflow.progress)`
    against a running instance of *this* workflow resolves correctly."""

    __pydantic_ai_agents__ = []

    def __init__(self) -> None:
        self._done = False

    @workflow.query
    def progress(self) -> dict:
        return {'status': {'done': self._done}, 'log': ['started']}

    @workflow.run
    async def run(self, sleep_s: float) -> dict:
        if sleep_s > 0:
            await workflow.execute_activity(
                sleep_activity, args=[sleep_s],
                start_to_close_timeout=timedelta(seconds=sleep_s + 30),
            )
        self._done = True
        return {'success': True, 'module_directory': 'nowhere'}


@workflow.defn
class MiniWorkflow(PydanticAIWorkflow):
    """agent.run() -> write_text_file activity -> read_text_file activity -> return."""

    __pydantic_ai_agents__ = [mini_agent]

    @workflow.run
    async def run(self, module_dir: str, prompt: str) -> dict:
        agent_result = await mini_agent.run(prompt)

        await workflow.execute_activity(
            activities.write_text_file,
            args=[f"{module_dir}/mini.txt", agent_result.output],
            start_to_close_timeout=timedelta(seconds=30),
        )
        # A second, sequential activity -- gives the crash-resume test a
        # deterministic midpoint to interrupt between.
        readback = await workflow.execute_activity(
            activities.read_text_file,
            args=[f"{module_dir}/mini.txt"],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {'agent_output': agent_result.output, 'file_contents': readback}


@workflow.defn
class ApprovalGateWorkflow(PydanticAIWorkflow):
    """Minimal workflow exercising the exact signal-and-wait_condition shape
    ModuleGenerationWorkflow._run_install_artifact uses for its human-in-the-
    loop upload gate (temporal/PHASE5.md Workstream D), without needing the
    full agent pipeline to reach that step. tests/test_workflow_progress.py
    already unit-tests ModuleGenerationWorkflow.approve_upload/reject_upload's
    own logic directly (no server needed); this proves the underlying
    Temporal mechanism -- a client signal actually reaching and unblocking a
    running workflow's wait_condition -- works live."""

    __pydantic_ai_agents__ = []

    def __init__(self) -> None:
        self._decision: str | None = None

    @workflow.signal
    def approve(self) -> None:
        self._decision = 'approved'

    @workflow.signal
    def reject(self) -> None:
        self._decision = 'rejected'

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._decision is not None)
        return self._decision
