"""
Tests for temporal.client.get_workflow_state (temporal/PHASE4.md 4.4): the
Django UI's replacement for tailing status.json, used to poll a
module-generation workflow's progress without blocking on completion.

Marked `temporal` (spins up an embedded WorkflowEnvironment) -- run with:
    uv run pytest -m temporal tests/test_client_state.py
"""
import concurrent.futures
from datetime import timedelta

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.testing import WorkflowEnvironment

from temporal.client import get_workflow_state
from temporal._test_fixtures import ProgressQueryWorkflow, sleep_activity

pytestmark = pytest.mark.temporal

QUEUE = 'test-progress-query-queue'
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


async def _connected_client(env: WorkflowEnvironment) -> Client:
    return await Client.connect(
        env.client.service_client.config.target_host,
        plugins=[PydanticAIPlugin()],
        data_converter=env.client.data_converter,
    )


async def test_unknown_workflow_id_returns_none():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        state = await get_workflow_state('no-such-workflow-id', client=client)
    assert state is None


async def test_running_workflow_reports_progress():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[ProgressQueryWorkflow],
            activities=[sleep_activity], activity_executor=_EXECUTOR,
        ):
            await client.start_workflow(
                ProgressQueryWorkflow.run,
                args=[30.0],
                id='progress-running',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=60),
            )

            state = await get_workflow_state('progress-running', client=client)

            assert state is not None
            assert state['execution_status'] == 'RUNNING'
            assert state['progress'] == {'status': {'done': False}, 'log': ['started']}
            assert state['result'] is None


async def test_completed_workflow_reports_result():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = await _connected_client(env)
        async with Worker(
            client, task_queue=QUEUE, workflows=[ProgressQueryWorkflow],
            activities=[sleep_activity], activity_executor=_EXECUTOR,
        ):
            handle = await client.start_workflow(
                ProgressQueryWorkflow.run,
                args=[0.0],
                id='progress-completed',
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            await handle.result()

            state = await get_workflow_state('progress-completed', client=client)

            assert state is not None
            assert state['execution_status'] == 'COMPLETED'
            assert state['result'] == {'success': True, 'module_directory': 'nowhere'}
            # Queries remain answerable on a completed workflow -- the console-log
            # view depends on this to show the final log tail after completion.
            assert state['progress'] == {'status': {'done': True}, 'log': ['started']}
