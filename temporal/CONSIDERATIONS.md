# Temporal Integration — Considerations

An evaluation of integrating [Temporal](https://temporal.io/) durable execution into the
GenePattern Module AI Toolkit, and whether Pydantic AI's built-in support makes it worthwhile.

## Short answer: Pydantic AI has first-class Temporal support

It is a co-maintained integration, not a community add-on. Install with:

```bash
pip install pydantic-ai[temporal]      # or: pydantic-ai-slim[temporal]
```

It provides:

- **`TemporalAgent`** — wraps an existing `Agent`. Every model request, tool call, and MCP
  call the agent makes automatically becomes a Temporal **activity** (durable, retried,
  resumable); the agent's own coordination runs inside the deterministic workflow.
- **`PydanticAIWorkflow`** — base class for the workflow; wrapped agents are listed on
  `__pydantic_ai_agents__`.
- **`PydanticAIPlugin`** — registers the agent activities with the Temporal worker/client.

```python
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalAgent, PydanticAIWorkflow

agent = Agent('openai:gpt-5.2', name='wrapper')   # name is now REQUIRED
temporal_agent = TemporalAgent(agent)

@workflow.defn
class ModuleGenerationWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [temporal_agent]

    @workflow.run
    async def run(self, tool_info: dict) -> str:
        result = await temporal_agent.run(...)
        return result.output
```

Pydantic AI also officially supports **DBOS, Prefect, and Restate** as durable backends. For
this project DBOS is a serious alternative — see the last section.

## Why this project is a genuinely good fit

The strongest argument is not "add reliability" — it is that the project **already contains a
hand-rolled version of what Temporal does.** `agents/module.py` persists `status.json` after
every step, and `--resume` replays a partially-completed run, skipping already-validated
artifacts. That is a manual, best-effort durable-execution engine. Temporal *subsumes* it:
exactly-once activities, automatic crash recovery mid-step, retry-with-backoff, and
observability, without maintaining checkpoint bookkeeping by hand. The integration replaces an
existing subsystem rather than bolting a new one on.

The pipeline shape also fits: minutes-long, multi-step, many external side effects (LLM calls,
Docker builds, URL downloads, GenePattern upload), plus a retry/escalation loop — squarely
Temporal's sweet spot.

## The fact that shapes everything: `TemporalAgent` is async-only

`run_sync()` is **not** supported inside a workflow. The pipeline uses `run_sync` everywhere
today — `researcher_agent.run_sync`, `planner_agent.run_sync`, and `agent.run_sync` in
`artifact_creation_loop`. So the core refactor is **sync → async across `ModuleAgent`**, plus a
second rule that trips people up: workflow code must be **deterministic**, so the orchestrator's
*direct* I/O — `subprocess` Docker builds, `requests.get` downloads, `open()` writes,
`datetime.now()`, `Path.iterdir()` — cannot run in the workflow. Each must move into an activity
(or use `workflow.now()`).

## Recommended architecture

Split `ModuleAgent` into a deterministic **workflow** (coordination) and **activities** (all
side effects):

| Today | Becomes |
|---|---|
| `researcher_agent`, `planner_agent`, six `<artifact>_agent`s | wrap each in `TemporalAgent` with a stable `name=` |
| `ModuleAgent.run()` | `@workflow.defn ModuleGenerationWorkflow.run()` |
| `artifact_creation_loop` retry counting, `classify_error`/`should_escalate`, escalation queue reorder | **workflow logic** — pure/deterministic already, stays in-process |
| `download_url_data` (`requests`) | `@activity.defn` |
| `validate_artifact` → linters (subprocess Docker, file I/O) | `@activity.defn` |
| dockerfile build + runtime test | `@activity.defn` — long-running; needs generous `start_to_close_timeout` + heartbeating |
| `upload_to_genepattern`, `zip_artifacts`, file writes | `@activity.defn` |
| `save_status`/`load_status` + `--resume` | **delete** (Temporal is the durability layer) |
| `generate-module.py` interactive `input()` | stays in the CLI, which becomes a thin Temporal **client** that starts the workflow |

Two things make this cheaper than it sounds:

- **`ArtifactDeps` is already a Pydantic `BaseModel`.** The integration requires `deps` be
  serializable, and it already is — `planning_data.model_dump(mode='json')` is passed around
  today. The most common blocker is a no-op here.
- The escalation logic (`agents/error_classifier.py`) is pure regex, so it runs unmodified in
  the workflow.

## Project-specific gotchas

1. **The ollama model instance won't serialize.** `configured_llm_model()` returns an
   `OpenAIChatModel(...)` *instance* for the `ollama:` path. Temporal cannot serialize arbitrary
   model instances for replay — pre-register it via `TemporalAgent(models={'ollama': model})`
   and reference by name. The plain-string path (`return DEFAULT_LLM_MODEL`) is fine as-is.
2. **Activity payload limits (~2 MB) vs. on-disk shared state.** The module directory is shared
   state on local disk today, and wrapper source / research text are passed into prompts. With
   distributed workers, pass files **by path on shared storage**, not by value — or pin the
   worker to one host to keep the local-filesystem assumption. This is the item most likely to
   force design changes; address it early.
3. **Limited `RunContext` in tools.** Inside tools running as activities, `RunContext` excludes
   `model`, `prompt`, `messages`, and `tracer`. Audit `@agent.tool` functions (e.g.
   `validate_wrapper`) for reliance on those — likely clean, but check.
4. **Streaming.** The Django UI streams logs live. Direct streaming methods are not available
   under Temporal; use `event_stream_handler` with `TemporalAgent.run()`. The web UI would query
   workflow state / stream via Temporal instead of tailing `status.json`.
5. **Logfire already in place** (`configure_telemetry()`) — it composes with Temporal's own
   observability, so existing traces are retained.

## Decision: Temporal vs. DBOS

Since the appeal here is mostly *durability + resume* rather than a fleet of distributed
workers, **DBOS** (also an official Pydantic AI backend) deserves a look. It is a Postgres-backed
library — no separate Temporal server/worker cluster to operate — which matches this repo's
current "single process, local disk" deployment far more naturally and sidesteps gotcha #2 almost
entirely.

- **Choose Temporal** for scale (many concurrent module generations), human-in-the-loop approval
  gates (e.g. pausing for GenePattern-upload sign-off), and the richest operational tooling.
- **Choose DBOS** for the lightest path to the same crash-safety when the goal is "don't lose a
  5-minute run when the box reboots."

Both eliminate the `status.json` layer.
