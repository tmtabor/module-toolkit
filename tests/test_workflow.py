"""
End-to-end Temporal workflow tests.

Marked `temporal` (excluded from the default suite -- see pytest.ini) since
each test spins up a temporalio.testing.WorkflowEnvironment, which downloads
and runs an embedded, time-skipping test server. Run explicitly with:

    uv run pytest -m temporal tests/test_workflow.py

Two things this file deliberately does and does not attempt, and why:

1. It validates the REAL, production `ModuleGenerationWorkflow` for sandbox
   determinism and clean activity registration under a genuine Worker (no
   execution needed for that check). This exercises the actual code path
   end users hit.
2. It does NOT execute the full `ModuleGenerationWorkflow` pipeline against
   TestModel. `TemporalAgent.override(model=...)` explicitly raises when
   called from inside a running workflow -- the model must be set at agent
   construction time -- so the production workflow's agents (which wrap the
   real configured LLM) cannot be swapped for TestModel at test time without
   restructuring agent construction to be injectable, which is out of scope
   here. Instead, `temporal/_test_fixtures.py::MiniWorkflow` exercises the
   identical TemporalAgent + workflow.execute_activity pattern end-to-end with
   a real TestModel-backed agent, and the crash-resume / replay-determinism
   tests are built on it -- they validate the *mechanism*
   ModuleGenerationWorkflow relies on, not a full pipeline run.
"""
import asyncio
import concurrent.futures
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker, Replayer, UnsandboxedWorkflowRunner
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.testing import WorkflowEnvironment

from temporal import activities
from temporal.worker import GENERAL_ACTIVITIES, DOCKER_ACTIVITIES
from temporal.workflow import ModuleGenerationWorkflow
from temporal._test_fixtures import (
    MiniWorkflow, mini_agent, TimeoutPolicyWorkflow, sleep_activity,
    RetryPolicyWorkflow, always_failing_activity, _always_fails_call_count,
    HeartbeatWrapperWorkflow, heartbeat_sleep_activity,
    ApprovalGateWorkflow, MakeModuleDirWorkflow,
)

pytestmark = pytest.mark.temporal

QUEUE = 'test-queue'
MINI_ACTIVITIES = [activities.write_text_file, activities.read_text_file]
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


async def _connected_client(env: WorkflowEnvironment) -> Client:
    return await Client.connect(
        env.client.service_client.config.target_host,
        plugins=[PydanticAIPlugin()],
        data_converter=env.client.data_converter,
    )


async def test_production_workflow_registers_cleanly():
    """The real ModuleGenerationWorkflow + all real activities must pass
    Temporal's sandbox determinism validation and register without name
    collisions -- this is what would have caught, e.g., the earlier
    duplicate-activity-registration or asyncio.run-at-import-time bugs found
    while building this integration (see temporal/PHASE3.md Step 3.0)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client,
            task_queue='module-generation-registration-check',
            workflows=[ModuleGenerationWorkflow],
            activities=GENERAL_ACTIVITIES + DOCKER_ACTIVITIES,
            activity_executor=_EXECUTOR,
        ):
            pass  # entering the `async with` block is the whole test


async def test_mini_workflow_runs_end_to_end(tmp_path):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[MiniWorkflow], activities=MINI_ACTIVITIES,
            activity_executor=_EXECUTOR,
        ):
            result = await client.execute_workflow(
                MiniWorkflow.run,
                args=[str(tmp_path), 'say hello'],
                id='mini-happy-path',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )

    assert result['agent_output'] == 'mini workflow output'
    assert result['file_contents'] == 'mini workflow output'
    assert (tmp_path / 'mini.txt').read_text() == 'mini workflow output'


async def test_crash_and_resume(tmp_path):
    """Kill a worker mid-run and confirm a fresh worker completes the workflow.

    Temporal persists workflow/activity progress server-side independent of
    worker process lifetime, so this holds regardless of exactly how far
    worker #1 got before being killed -- the assertion is on final
    correctness, not on hitting a precise interruption window.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)

        handle = await client.start_workflow(
            MiniWorkflow.run,
            args=[str(tmp_path), 'crash test prompt'],
            id='mini-crash-resume',
            task_queue=QUEUE,
            execution_timeout=timedelta(seconds=30),
        )

        # Worker #1: start it, let it make some (unknown amount of) progress,
        # then kill it abruptly (task.cancel(), not a graceful shutdown) to
        # simulate a process crash.
        worker1 = Worker(client, task_queue=QUEUE, workflows=[MiniWorkflow], activities=MINI_ACTIVITIES, activity_executor=_EXECUTOR)
        worker1_task = asyncio.ensure_future(worker1.run())
        await asyncio.sleep(0.1)
        worker1_task.cancel()
        try:
            await worker1_task
        except asyncio.CancelledError:
            pass

        # Worker #2: fresh worker, same task queue -- picks up wherever
        # worker #1 left off (Temporal redelivers/retries in-flight work).
        async with Worker(client, task_queue=QUEUE, workflows=[MiniWorkflow], activities=MINI_ACTIVITIES, activity_executor=_EXECUTOR):
            result = await handle.result()

    assert result['agent_output'] == 'mini workflow output'
    assert (tmp_path / 'mini.txt').read_text() == 'mini workflow output'


async def test_replay_determinism(tmp_path):
    """Record a real workflow history, then replay it and assert no
    non-determinism error -- catches regressions like accidental
    datetime.now()/unsorted-iteration calls creeping into workflow code."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[MiniWorkflow], activities=MINI_ACTIVITIES,
            activity_executor=_EXECUTOR,
        ):
            handle = await client.start_workflow(
                MiniWorkflow.run,
                args=[str(tmp_path), 'replay test prompt'],
                id='mini-replay',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            await handle.result()

        history = await handle.fetch_history()

    # workflow_runner=UnsandboxedWorkflowRunner(): a second, independent
    # sandboxed validation pass in the same process collides with beartype's
    # import hook (a pydantic-ai dependency) -- an environment/dependency
    # interaction unrelated to this workflow's own code, already validated by
    # the sandboxed Worker construction in the other tests in this file.
    # Replay determinism itself -- does re-running the workflow's logic
    # against recorded history produce the same commands -- is unaffected by
    # skipping the extra Python-level sandbox on this second pass.
    replayer = Replayer(workflows=[MiniWorkflow], workflow_runner=UnsandboxedWorkflowRunner())
    # Raises WorkflowNondeterminismError (or similar) on failure; a clean
    # return is the assertion.
    await replayer.replay_workflow(history)


async def test_activity_timeout_policy_no_heartbeat(tmp_path):
    """Regression for temporal/PHASE3.5.md H0.

    An activity that runs longer than a would-be heartbeat window must:
      - COMPLETE when only start_to_close_timeout is set (the policy the real
        workflow uses for the docker build and downloads), and
      - FAIL when a heartbeat_timeout it never satisfies is declared (the exact
        bug that silently killed the docker build at 30s).
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[TimeoutPolicyWorkflow],
            activities=[sleep_activity], activity_executor=_EXECUTOR,
        ):
            # No heartbeat: 1s activity under a 30s start_to_close -> completes.
            ok = await client.execute_workflow(
                TimeoutPolicyWorkflow.run,
                args=[1.0, 30.0, 0.0],
                id='timeout-no-heartbeat',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            assert ok == 'slept'

            # Heartbeat declared but never sent: same generous start_to_close,
            # but a 0.2s heartbeat_timeout -> Temporal kills it (the H0 bug).
            with pytest.raises(Exception):
                await client.execute_workflow(
                    TimeoutPolicyWorkflow.run,
                    args=[1.0, 30.0, 0.2],
                    id='timeout-with-unfed-heartbeat',
                    task_queue=QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )


async def test_retry_policy_bounds_attempts():
    """Regression for temporal/PHASE5.md Workstream C1: an activity's
    RetryPolicy.maximum_attempts must actually bound Temporal's retries,
    not just be accepted and ignored. always_failing_activity raises every
    time, so the workflow must fail once maximum_attempts is exhausted, and
    the activity must have been invoked exactly that many times -- not more
    (policy honored) and not fewer (retries did happen)."""
    _always_fails_call_count['n'] = 0
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[RetryPolicyWorkflow],
            activities=[always_failing_activity], activity_executor=_EXECUTOR,
        ):
            with pytest.raises(Exception):
                await client.execute_workflow(
                    RetryPolicyWorkflow.run,
                    args=[3],
                    id='retry-policy-bounds-attempts',
                    task_queue=QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )

    assert _always_fails_call_count['n'] == 3


async def test_heartbeat_wrapped_activity_survives_short_heartbeat_timeout():
    """Regression for temporal/PHASE5.md Workstream C2.

    heartbeat_sleep_activity (built with the real production
    _wrap_with_heartbeat, heartbeat_interval_sec=0.3) runs for 1.5s under a
    heartbeat_timeout of only 0.6s -- twice the heartbeat interval, so it
    must complete normally, proving the wrapper's real heartbeats keep it
    alive. TimeoutPolicyWorkflow's existing test already proves the negative
    case (a non-heartbeating activity under a short heartbeat_timeout is
    killed); this proves the fix for the two activities that now use it.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[HeartbeatWrapperWorkflow],
            activities=[heartbeat_sleep_activity], activity_executor=_EXECUTOR,
        ):
            result = await client.execute_workflow(
                HeartbeatWrapperWorkflow.run,
                args=[1.5, 0.6],
                id='heartbeat-wrapper-survives',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )

    assert result == 'heartbeat-sleep-done'


async def test_signal_unblocks_a_waiting_workflow():
    """Regression for temporal/PHASE5.md Workstream D (human-in-the-loop
    GenePattern-upload approval gate).

    ApprovalGateWorkflow uses the exact signal-and-wait_condition shape
    ModuleGenerationWorkflow._run_install_artifact uses while awaiting
    approve_upload/reject_upload. tests/test_workflow_progress.py already
    unit-tests that workflow's own signal-handler *logic* (no-op when
    nothing's pending, correct decision value when something is) without a
    server. This proves the underlying Temporal *mechanism* live: a client
    signal sent after the workflow has started actually reaches and unblocks
    a real, running `workflow.wait_condition`, both for approval and
    rejection.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[ApprovalGateWorkflow], activities=[],
        ):
            handle = await client.start_workflow(
                ApprovalGateWorkflow.run,
                id='approval-gate-approve',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            await handle.signal(ApprovalGateWorkflow.approve)
            assert await handle.result() == 'approved'

            handle2 = await client.start_workflow(
                ApprovalGateWorkflow.run,
                id='approval-gate-reject',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            await handle2.signal(ApprovalGateWorkflow.reject)
            assert await handle2.result() == 'rejected'


async def test_concurrent_workflows_across_two_workers_avoid_module_dir_collision(tmp_path):
    """temporal/PHASE5.md Workstream E: N simultaneous workflows against 2+
    worker processes must not collide on module directory names.

    Two real Worker instances poll the same task queue (representative of two
    worker *processes* -- Temporal's dispatch/locking model doesn't
    distinguish them from separate processes; the thing that's actually in
    question is filesystem atomicity, which is OS-level and doesn't care
    either). N workflow instances are started concurrently, all requesting the
    *same* output_dir/tool_name/timestamp -- the exact scenario that used to
    silently produce a shared directory (agents.effects.make_module_dir's
    prior unconditional exist_ok=True) before this workstream's fix.
    """
    n = 6
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        # Two independent client connections, each backing its own Worker on
        # the same task queue -- the Rust bridge refuses two Workers sharing
        # one client's connection to register on an identical task queue, but
        # that's a same-process SDK bookkeeping detail, not a Temporal-server
        # rule. Separate processes each get their own client/connection, so
        # separate connections here is the more faithful two-worker-process
        # simulation anyway.
        client2 = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[MakeModuleDirWorkflow],
            activities=[activities.make_module_dir], activity_executor=_EXECUTOR,
        ), Worker(
            client2, task_queue=QUEUE, workflows=[MakeModuleDirWorkflow],
            activities=[activities.make_module_dir], activity_executor=_EXECUTOR,
        ):
            results = await asyncio.gather(*[
                client.execute_workflow(
                    MakeModuleDirWorkflow.run,
                    args=[str(tmp_path), "tool", "20260101_120000"],
                    id=f'make-module-dir-race-{i}',
                    task_queue=QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )
                for i in range(n)
            ])

    assert len(set(results)) == n
    assert all(Path(p).is_dir() for p in results)
