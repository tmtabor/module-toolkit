"""
Temporal activity wrappers over the pipeline's side-effect functions.

Every function in `agents.effects` is already activity-ready (module-level,
serializable I/O, no `Logger`/`self` -- see temporal/PHASE2.md's design rules).
They are synchronous, so the worker that registers these activities must pass
an `activity_executor` (a `ThreadPoolExecutor`) -- see temporal/worker.py.

`build_and_test_image` (the docker build/runtime test) is intentionally *not*
given a long `start_to_close_timeout` here: Temporal activity timeouts are
configured per call via `workflow.execute_activity(..., start_to_close_timeout=...,
heartbeat_timeout=...)` in temporal/workflow.py, not at decoration time.
"""
import concurrent.futures
import functools
from typing import Any, Optional

from temporalio import activity

from agents import effects
from agents.logger import Logger

# How often the heartbeat-wrapped activities (below) signal liveness while
# their blocking work runs in a background thread.
_HEARTBEAT_INTERVAL_SEC = 10.0


def _wrap(name: str, fn):
    """Decorate a *fresh* wrapper around `fn` rather than `fn` itself.

    `activity.defn` tags the function object it's given with
    `__temporal_activity_definition`. `agents.effects.*` functions are shared,
    cached module-level objects -- if this module gets executed more than
    once in the same process (e.g. Temporal's workflow sandbox re-imports
    modules under its own import machinery), decorating the shared object
    directly raises "Function already contains activity definition" on the
    second pass. Wrapping in a new function each time this module runs avoids
    mutating shared state. functools.wraps preserves the signature/type hints
    Temporal's data converter relies on.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return activity.defn(name=name)(wrapper)


def _wrap_with_heartbeat(name: str, fn, heartbeat_interval_sec: float = _HEARTBEAT_INTERVAL_SEC):
    """Like `_wrap`, but sends a heartbeat every `heartbeat_interval_sec`
    while `fn` runs, instead of running silent until it returns.

    `agents.effects.*` functions are deliberately Temporal-agnostic (Phase 2)
    -- they don't know about `activity.heartbeat()` and shouldn't have to.
    Rather than thread a heartbeat callback down into them (coupling every
    caller, including the --legacy path, to a Temporal-shaped progress
    protocol), run the blocking call in a background thread and heartbeat
    from *this* activity's own thread while polling it -- `activity.heartbeat()`
    resolves via the current thread's activity context, so it must be called
    from here, not from the background thread doing the actual work.

    This is what makes real heartbeat_timeout-based failure detection safe for
    the docker build and downloads (temporal/PHASE3.5.md H0 chose generous
    fixed timeouts specifically *because* nothing heartbeated; temporal/
    PHASE5.md Workstream C2 is closing that gap for these two activities).
    `heartbeat_interval_sec` is a parameter (not just the module constant) so
    tests can use a short interval instead of waiting on real wall-clock time
    -- activities run in real time even under Temporal's time-skipping test
    server, unlike workflow-side timers.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn, *args, **kwargs)
            while True:
                try:
                    return future.result(timeout=heartbeat_interval_sec)
                except concurrent.futures.TimeoutError:
                    activity.heartbeat(f"{name} still running")
    return activity.defn(name=name)(wrapper)


make_module_dir = _wrap('effects__make_module_dir', effects.make_module_dir)
write_text_file = _wrap('effects__write_text_file', effects.write_text_file)
read_text_file = _wrap('effects__read_text_file', effects.read_text_file)
file_exists = _wrap('effects__file_exists', effects.file_exists)
remove_dir = _wrap('effects__remove_dir', effects.remove_dir)
find_wrapper_file = _wrap('effects__find_wrapper_file', effects.find_wrapper_file)
read_manifest_docker_image = _wrap('effects__read_manifest_docker_image', effects.read_manifest_docker_image)
download_one = _wrap_with_heartbeat('effects__download_one', effects.download_one)
upload_module = _wrap('effects__upload_module', effects.upload_module)
zip_artifacts = _wrap('effects__zip_artifacts', effects.zip_artifacts)
docker_push = _wrap('effects__docker_push', effects.docker_push)
run_linter = _wrap('effects__run_linter', effects.run_linter)
build_and_test_image = _wrap_with_heartbeat('effects__build_and_test_image', effects.build_and_test_image)


# ---------------------------------------------------------------------------
# Phase-3-specific activity (not part of agents/effects.py's Phase 2 scope)
# ---------------------------------------------------------------------------
# The workflow's shadow status.json (`save_status_snapshot`) was removed in
# Phase 4 (temporal/PHASE4.md 4.1): the workflow's history + `progress` query
# are the source of truth now, so nothing consumed the file. The dockerfile
# runtime-command builder stays here (kept out of agents/effects.py so that
# module's Phase 2 scope stays stable).

@activity.defn(name='build_dockerfile_runtime_command')
async def build_dockerfile_runtime_command(
    planning_data: dict[str, Any],
    example_data: list[dict[str, Any]],
    gpunit_params: dict[str, Any],
    module_path: Optional[str],
) -> dict[str, Any]:
    """Build the docker runtime test command + volumes for the dockerfile artifact.

    Thin activity wrapper around `dockerfile.runtime.build_runtime_command`,
    run whole (including its internal LLM hint-mapping call and file reads) as
    a single coarse-grained activity rather than decomposed into workflow
    code -- that function's three-strategy fallback (manifest / wrapper
    introspection / placeholder substitution) is intricate enough that
    reimplementing it by hand as workflow code risked transcription bugs; a
    coarse activity wrapping a coherent unit of business logic (including its
    own internal, non-deterministic LLM call) is a standard, legitimate
    Temporal pattern. See temporal/PHASE3.md Step 3.4 for the tradeoff.
    """
    # Local imports: this activity module must stay importable without pulling
    # in dockerfile.runtime's Agent construction at worker-registration time
    # for every other activity; deferred import keeps that cost import-local.
    from pathlib import Path
    from agents.example_data import ExampleDataItem
    from agents.models import ModulePlan
    from dockerfile.runtime import build_runtime_command

    plan = ModulePlan(**planning_data)
    items = [ExampleDataItem.from_dict(d) for d in example_data]
    path = Path(module_path) if module_path else None

    command, volumes = await build_runtime_command(plan, items, gpunit_params, path, Logger())
    return {'command': command, 'volumes': volumes}


# The canonical list for worker registration (temporal/worker.py).
ALL_EFFECT_ACTIVITIES = [
    make_module_dir,
    write_text_file,
    read_text_file,
    file_exists,
    remove_dir,
    find_wrapper_file,
    read_manifest_docker_image,
    download_one,
    upload_module,
    zip_artifacts,
    docker_push,
    run_linter,
    build_and_test_image,
    build_dockerfile_runtime_command,
]
