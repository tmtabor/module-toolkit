"""
Temporal client helpers for starting a module-generation workflow.

Used by generate-module.py's default (non---legacy) code path.
"""
import os
import uuid
from datetime import timedelta
from typing import Any, Optional

from temporalio.client import Client, WorkflowHandle
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin

from temporal.worker import MODULE_GENERATION_QUEUE
from temporal.workflow import ModuleGenerationWorkflow

# Bounded end-to-end cap so a workflow submitted when no worker is serving the
# queue eventually errors instead of the CLI hanging on handle.result() forever
# (temporal/PHASE3.5.md M4). Generous by default -- real generations can take a
# while -- and env-configurable.
_EXECUTION_TIMEOUT = timedelta(seconds=int(os.getenv('TEMPORAL_EXECUTION_TIMEOUT_SEC', '7200')))  # 2h


async def _connect() -> Client:
    target = os.environ.get('TEMPORAL_ADDRESS', 'localhost:7233')
    return await Client.connect(target, plugins=[PydanticAIPlugin()])


async def connect() -> Client:
    """Public alias of `_connect()`, for callers (the Django UI) that want to
    share one Client across several `get_workflow_state()` calls instead of
    reconnecting per call."""
    return await _connect()


async def start_module_generation(
    tool_info: dict[str, Any],
    *,
    skip_artifacts: Optional[list[str]] = None,
    max_loops: int = 5,
    max_escalations: int = 2,
    no_zip: bool = False,
    zip_only: bool = False,
    docker_push: bool = False,
    gp_server: Optional[str] = None,
    gp_user: Optional[str] = None,
    gp_password: Optional[str] = None,
    require_upload_approval: bool = False,
    workflow_id: Optional[str] = None,
    execution_timeout: Optional[timedelta] = None,
) -> WorkflowHandle:
    """Start ModuleGenerationWorkflow and return a handle without waiting for it to finish.

    `execution_timeout` defaults to `_EXECUTION_TIMEOUT` (2h) but can be
    overridden -- callers that set `require_upload_approval=True` should pass
    something generous here (the default is almost certainly too short for a
    human to notice and act on an approval request; temporal/PHASE5.md
    Workstream D's gate has no timeout of its own, so the workflow's own
    execution_timeout is the real outer bound on how long it can wait).
    """
    client = await _connect()
    wf_id = workflow_id or f"module-generation-{tool_info['name'].lower()}-{uuid.uuid4().hex[:8]}"
    # start_workflow's multi-param overload takes a single positional `args`
    # sequence (no kwargs=) -- order must match ModuleGenerationWorkflow.run's
    # signature exactly.
    return await client.start_workflow(
        ModuleGenerationWorkflow.run,
        args=[
            tool_info, skip_artifacts, max_loops, max_escalations,
            no_zip, zip_only, docker_push, gp_server, gp_user, gp_password,
            require_upload_approval,
        ],
        id=wf_id,
        task_queue=MODULE_GENERATION_QUEUE,
        execution_timeout=execution_timeout or _EXECUTION_TIMEOUT,
    )


async def run_module_generation(tool_info: dict[str, Any], **kwargs) -> dict:
    """Start the workflow and await its result -- the CLI's primary entry point."""
    handle = await start_module_generation(tool_info, **kwargs)
    return await handle.result()


async def decide_upload(workflow_id: str, approve: bool, client: Optional[Client] = None) -> bool:
    """Signal a running workflow's pending upload-approval gate (temporal/
    PHASE5.md Workstream D). Returns True if the signal was sent, False if the
    workflow ID doesn't exist (e.g. already completed or never started) --
    doesn't confirm the workflow was actually *waiting*, since signals are
    fire-and-forget and Temporal doesn't provide a synchronous ack for that;
    a stale/misdirected signal is a no-op on the workflow side regardless (see
    ModuleGenerationWorkflow.approve_upload's docstring).

    `client` is exposed for tests, matching `get_workflow_state`.
    """
    client = client or await _connect()
    handle = client.get_workflow_handle(workflow_id)
    signal = ModuleGenerationWorkflow.approve_upload if approve else ModuleGenerationWorkflow.reject_upload
    try:
        await handle.signal(signal)
        return True
    except Exception:
        return False


async def get_workflow_state(workflow_id: str, client: Optional[Client] = None) -> Optional[dict]:
    """Read the current state of a module-generation workflow by ID, for callers
    (the Django UI) that need to poll without blocking on completion.

    Returns None if the workflow ID is unknown to the server (never started, or
    outside the namespace's retention window). Otherwise returns:
      {'execution_status': 'RUNNING' | 'COMPLETED' | 'FAILED' | 'TERMINATED' | ...,
       'progress': <the progress() query's dict, or None if it couldn't be read>,
       'result': <the workflow's return value, only present when COMPLETED>}

    Used by app/generator/views.py in place of the old status.json tailing
    (temporal/PHASE4.md 4.4) -- the workflow is the single source of truth for
    both in-flight progress and the final result, so there's nothing else to
    keep in sync.

    `client` is exposed for tests to inject a WorkflowEnvironment-connected
    client; production callers omit it and get the real `_connect()`.
    """
    client = client or await _connect()
    handle = client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except Exception:
        return None

    execution_status = desc.status.name if desc.status else 'UNKNOWN'
    state: dict[str, Any] = {'execution_status': execution_status, 'progress': None, 'result': None}

    # Queries are answerable on both open and closed workflows (within the
    # namespace's retention window) -- Temporal replays history to compute
    # them, it doesn't require the workflow to still be RUNNING. Always try,
    # so callers (e.g. the console-log view) can show the final log tail after
    # completion too, not just while in flight.
    try:
        state['progress'] = await handle.query(ModuleGenerationWorkflow.progress)
    except Exception:
        pass

    if execution_status == 'COMPLETED':
        try:
            state['result'] = await handle.result()
        except Exception:
            pass

    return state
