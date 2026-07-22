"""
Temporal worker process for the module-generation pipeline.

Runs two Workers concurrently in this process for local/dev use:
  - `module-generation`: the workflow itself + all agent activities (auto-
    injected by PydanticAIPlugin from ModuleGenerationWorkflow.__pydantic_ai_agents__)
    + the agents.effects-derived activities.
  - `docker-builds`: only the docker build/runtime-test activity, so a
    Docker-capable worker can be scaled/deployed independently in production
    (temporal/PHASE3.md Step 3.2). Split into a separate process via
    `--queue docker-builds` if desired.

Sync activities (the agents.effects wrappers) run in a ThreadPoolExecutor --
required by the Temporal Python SDK for non-async activities.

Environment: this process needs the SAME configuration the CLI does, because
the agents run here -- DEFAULT_LLM_MODEL, OLLAMA_BASE_URL, BRAVE_API_KEY, the
GP_* upload creds, and the token-cost vars. They are loaded from .env at the
top of this module (see below). TEMPORAL_ADDRESS selects the server
(default localhost:7233).

Run:  uv run python -m temporal.worker      # serves both task queues
"""
import argparse
import asyncio
import concurrent.futures
import os

from dotenv import load_dotenv

# Load .env and configure telemetry BEFORE importing the workflow/agent modules.
# The worker -- not the client -- is where agents actually execute (model
# requests are activities that run here), and configured_llm_model() reads
# DEFAULT_LLM_MODEL / OLLAMA_BASE_URL / BRAVE_API_KEY / GP creds / token-cost
# vars at agent-construction (i.e. import) time. `uv run` does NOT auto-load
# .env, so without this the worker silently runs on the ollama:qwen3:8b default
# with no Brave search and no Logfire traces (temporal/PHASE3.5.md H1).
load_dotenv()

from agents.config import configure_telemetry
configure_telemetry()

from temporalio.client import Client
from temporalio.worker import Worker
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin

from temporal import activities
from temporal.workflow import ModuleGenerationWorkflow

MODULE_GENERATION_QUEUE = 'module-generation'
DOCKER_BUILDS_QUEUE = 'docker-builds'

# The single activity that needs Docker on the host; kept in its own queue so
# it can be routed to a worker process that actually has `docker` available.
DOCKER_ACTIVITIES = [activities.build_and_test_image]
GENERAL_ACTIVITIES = [a for a in activities.ALL_EFFECT_ACTIVITIES if a not in DOCKER_ACTIVITIES]


async def _connect() -> Client:
    target = os.environ.get('TEMPORAL_ADDRESS', 'localhost:7233')
    return await Client.connect(target, plugins=[PydanticAIPlugin()])


async def run_worker(queue: str = 'both') -> None:
    client = await _connect()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    workers = []
    if queue in ('both', MODULE_GENERATION_QUEUE):
        workers.append(Worker(
            client,
            task_queue=MODULE_GENERATION_QUEUE,
            workflows=[ModuleGenerationWorkflow],
            activities=GENERAL_ACTIVITIES,
            activity_executor=executor,
        ))
    if queue in ('both', DOCKER_BUILDS_QUEUE):
        workers.append(Worker(
            client,
            task_queue=DOCKER_BUILDS_QUEUE,
            activities=DOCKER_ACTIVITIES,
            activity_executor=executor,
        ))

    if not workers:
        raise ValueError(f"Unknown queue: {queue}")

    print(f"Starting {len(workers)} worker(s) against {client.service_client.config.target_host}...")
    await asyncio.gather(*(w.run() for w in workers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporal worker for module generation")
    parser.add_argument(
        '--queue', choices=['both', MODULE_GENERATION_QUEUE, DOCKER_BUILDS_QUEUE], default='both',
        help="Which task queue(s) to serve (default: both, in this process)",
    )
    args = parser.parse_args()
    asyncio.run(run_worker(args.queue))


if __name__ == '__main__':
    main()
