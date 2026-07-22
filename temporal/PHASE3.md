# Phase 3 — Introduce Temporal (Workflow + Activities, Shadow Mode)

Wrap the async pipeline built in [PHASE1.md](./PHASE1.md) and the effects seam built in
[PHASE2.md](./PHASE2.md) with real Temporal durable execution, running the CLI's actual
generation runs through a Temporal workflow while `status.json` is retained as a shadow
(fallback + comparison) per [PLAN.md](./PLAN.md) Phase 3. See [CONSIDERATIONS.md](./CONSIDERATIONS.md)
for the original rationale — several of its gotchas are corrected below against the API this
project actually has installed (`pydantic-ai 2.13.0`).

## Read this first: verified against the installed package, not just docs

`pydantic-ai 2.13.0` (already in `uv.lock`) ships `pydantic_ai.durable_exec.temporal` in the base
package — only the `temporalio` SDK itself needs adding via the `temporal` extra. Before writing
this plan, the actual installed source (`.venv/.../pydantic_ai/durable_exec/temporal/_agent.py`)
was read directly, which **corrects three assumptions from CONSIDERATIONS.md**:

1. **Agent `name=` does NOT require editing the 9 existing `Agent(...)` constructors.**
   `TemporalAgent.name` falls back to the wrapped agent's own `.name` only if
   `TemporalAgent(agent, name=...)` doesn't supply one itself:
   `self.name` → `self._name or super().name`. So names can be assigned entirely at the
   **wrapping site** in new Temporal-specific code, leaving `agents/researcher.py`,
   `agents/planner.py`, `wrapper/agent.py`, etc. untouched. This is simpler than
   CONSIDERATIONS.md assumed and is the approach this plan takes (Step 3.1).

2. **Toolset `id=` is a real, code-enforced blocker — but only in two places.**
   `TemporalAgent.__init__` raises `UserError` for any toolset in `agent.toolsets` whose
   `.id` is `None`. Verified: a plain `Agent()`'s own `@agent.tool`/`@agent.tool_plain` registry
   auto-gets `id='<agent>'` (no action needed — confirmed by inspection: `agent._function_toolset.id
   == '<agent>'`). But `wrapper/agent.py`'s `_wrapper_skills = SkillsToolset(...)` and
   `manifest/agent.py`'s `_manifest_skills = SkillsToolset(...)` are constructed with no `id=`
   (confirmed: `SkillsToolset(directories=[]).id is None`), so **both must get an explicit `id=`**
   or `TemporalAgent(wrapper_agent, ...)` fails immediately at construction. `SkillsToolset`
   subclasses `FunctionToolset` (confirmed via MRO), so once it has an `id`, the default
   `temporalize_toolset_func` handles it automatically — no custom toolset-prep function needed.

3. **The ollama ("ollama:qwen3:8b") model instance does NOT need manual `models={}`
   pre-registration.** `TemporalAgent.__init__` does
   `wrapped_model = wrapped.model if isinstance(wrapped.model, Model) else None`, then always
   wraps it via `TemporalModel(wrapped_model, ...)`. Since `configured_llm_model()` already
   returns a concrete `OpenAIChatModel` instance for the `ollama:` path, this **already works
   without registration**. `models={}` is only needed for *additional* models an agent might
   switch to at runtime (`agent.run(model=...)`), which this project doesn't do. This softens
   CONSIDERATIONS.md gotcha #1 to a non-issue for the default-model case.

Confirmed unchanged from CONSIDERATIONS.md: `TemporalAgent.run_sync` still wraps
`loop.run_until_complete(...)` and cannot be called from an already-running event loop — Phase 1's
async conversion was a genuine, necessary prerequisite. `run_stream`/`iter` raise inside a
workflow; streaming must go through `event_stream_handler` passed to `TemporalAgent(...)`.

## Package layout: `temporal/` holds both the docs and the runtime code, for now

By project decision, `temporal/` — currently docs-only (`PLAN.md`, `CONSIDERATIONS.md`,
`PHASE0.md`–`PHASE3.md`) — becomes the actual Python package for the Temporal runtime code too.
The `.md` files stay put; splitting them out to a separate location once the Temporal work is
further along is a deliberate, deferred cleanup, not an oversight. `temporal/` isn't a package
yet (no `__init__.py`), so Step 3.0 adds one.

```
temporal/
├── PLAN.md, CONSIDERATIONS.md, PHASE0.md … PHASE3.md   # existing docs, unchanged
├── __init__.py       # NEW (Step 3.0) — makes temporal/ importable
├── agents.py           # TemporalAgent-wrapped versions of the 9 plain agents (Step 3.1)
├── activities.py        # @activity.defn wrappers over agents.effects functions (Step 3.2)
├── workflow.py            # ModuleGenerationWorkflow (Step 3.4)
├── worker.py                # Worker process entry point (Step 3.6)
└── client.py                  # start_workflow helper used by generate-module.py (Step 3.7)
```

## Step 3.0 — Install & spike

- [ ] Add the `temporal` extra: `uv add "pydantic-ai[temporal]"` (pulls in `temporalio`).
      Re-run `uv lock`; confirm `uv run python -c "import temporalio"` succeeds.
- [ ] Create `temporal/__init__.py` (empty) so `temporal/` becomes an importable Python package
      alongside its existing `.md` docs.
- [ ] Local dev server: `temporal server start-dev` (matches the Jaeger-alongside pattern already
      documented in `README.md`'s Observability section).
- [ ] Minimal spike (throwaway, not committed): wrap `researcher_agent` in `TemporalAgent(agent,
      name='researcher')`, run it inside a bare `@workflow.defn` against `TestModel`, confirm the
      activity appears in the Temporal Web UI. This validates the toolchain before the real work
      starts.
- [ ] **Decision gate carried over from PLAN.md Phase 0**: if this spike surfaces friction serious
      enough to reconsider DBOS, stop here — everything past this point assumes Temporal.

## Step 3.1 — Freeze agent identity (names + toolset ids)

New file `temporal/agents.py`. Does **not** modify `agents/researcher.py`, `agents/planner.py`,
`wrapper/agent.py`, `manifest/agent.py`, `paramgroups/agent.py`, `gpunit/agent.py`,
`documentation/agent.py`, `dockerfile/agent.py`, or `dockerfile/runtime.py`.

- [ ] For each of the 9 agents, construct a `TemporalAgent(plain_agent, name='<stable-name>')`.
      Proposed names (permanent once used in production — Temporal keys activities by them):
      `researcher`, `planner`, `wrapper`, `manifest`, `paramgroups`, `gpunit`, `documentation`,
      `dockerfile`, `dockerfile_hint_mapping` (for `dockerfile.runtime._hint_mapping_agent`).
- [ ] Fix the two toolset-id gaps **at the source** (this is a real, minimal edit to existing
      files, not new code): `wrapper/agent.py`'s `_wrapper_skills = SkillsToolset(..., id='wrapper-skills')`
      and `manifest/agent.py`'s `_manifest_skills = SkillsToolset(..., id='manifest-skills')`.
      This is the one unavoidable touch to the existing agent modules.
- [ ] Unit test: construct each `TemporalAgent` outside of any workflow (no Temporal client/worker
      needed for this — it's a synchronous constructor check) and assert no `UserError` is raised.
      This alone would have caught the missing-`id` issue before Step 3.4.

## Step 3.2 — Decorate `agents/effects.py` as activities

New file `temporal/activities.py`.

- [ ] All 12 functions in `agents/effects.py` are already activity-ready per PHASE2.md's design
      rules (module-level, serializable I/O, no `Logger`/`self`). They are **synchronous** — no
      rewrite needed. Wrap each with `@activity.defn(name=...)`:
      `make_module_dir`, `write_text_file`, `read_text_file`, `remove_dir`, `find_wrapper_file`,
      `read_manifest_docker_image`, `download_one`, `upload_module`, `zip_artifacts`, `docker_push`,
      `run_linter`, `build_and_test_image`.
- [ ] Because these are sync activities, the worker (Step 3.6) needs an
      `activity_executor=ThreadPoolExecutor(...)` passed to `Worker(...)` — standard Temporal
      Python SDK requirement for non-async activities. No change to `effects.py` itself.
- [ ] `build_and_test_image` is the heavy one (shells out to `docker build`/`docker run` via the
      dockerfile linter): give it its own `heartbeat_timeout` and a generous
      `start_to_close_timeout` (minutes, not the ~60s default) at the **call site** in the workflow
      (Step 3.4), not at decoration time — Temporal activity timeouts are configured per-call via
      `workflow.execute_activity(..., start_to_close_timeout=..., heartbeat_timeout=...)`.
- [ ] Route it through a **dedicated task queue** (e.g. `docker-builds`) whose worker process has
      Docker available, separate from the general-purpose task queue the other activities use —
      lets you scale/isolate Docker-capable workers independently.

## Step 3.3 — Workflow-safe logging

- [ ] `ModuleAgent`'s coordination calls `self.logger.print_status(...)` throughout, including via
      `_emit()` draining effect `log` lists (PHASE2.md). Plain `print`/logging calls inside
      workflow code are replay-unsafe (they'd double-print on history replay). Add a
      `WorkflowLogger` (implementing the same `print_status`/`print_section` interface as
      `agents/logger.py::Logger`) that calls `temporalio.workflow.logger.info(...)`, which Temporal
      automatically suppresses during replay.
- [ ] This is the only behavioral adapter needed — the coordination logic itself doesn't change,
      only which `Logger`-shaped object it's constructed with when running as a workflow vs. the
      legacy CLI path.

## Step 3.4 — The workflow

New file `temporal/workflow.py`. This is the biggest step: **port**, not rewrite, the
already-async, already-effects-clean coordination in `agents/module.py`.

- [ ] `@workflow.defn class ModuleGenerationWorkflow(PydanticAIWorkflow)` with
      `__pydantic_ai_agents__` listing the 9 `TemporalAgent`s from Step 3.1.
- [ ] Port the bodies of `do_research`, `do_planning`, `artifact_creation_loop`,
      `build_runtime_command`, `generate_all_artifacts`, `run` (the 6 methods Phase 1 made
      `async`) into `@workflow.run` / workflow-local methods. Concretely, two substitutions
      throughout — both mechanical, since the surrounding logic (retry counting, the
      `classify_error`/`should_escalate` escalation-queue reordering, error-history accumulation)
      is already pure/deterministic and needs no change:
      - `agent.run(prompt, deps=...)` → `temporal_agent.run(prompt, deps=...)` (the
        `temporal.agents` versions).
      - `effects.some_function(...)` direct calls (in `download_url_data`, `validate_artifact`,
        `_sync_wrapper_script`, `_get_manifest_docker_image`, `zip_artifacts`, `docker_push`,
        `upload_to_genepattern`, artifact/report file writes, research.md/plan.md/plan.jsonl
        writes, `create_module_directory`) → `await workflow.execute_activity(effects.some_function,
        args=[...], start_to_close_timeout=...)`.
- [ ] `create_module_directory`'s timestamp: **this is why PHASE2.md injected it as a parameter.**
      Replace `datetime.now()` with `workflow.now()` at the call site inside the workflow — the one
      line the determinism checklist (PHASE2.md) flagged as needing this exact swap.
- [ ] `save_status`/`load_status` and `status.json`: **retained, not ported.** Per PLAN.md's
      shadow-mode design, the workflow still calls a `save_status` activity after each phase so
      the file continues to exist for comparison and Django-UI compatibility — it is not the
      source of truth anymore (Temporal's workflow history is), but nothing consumes that
      distinction yet. Full removal is Phase 4.
- [ ] `_run_install_artifact`, `print_final_report`: `print_final_report` does console `print()` +
      a final `Path.iterdir()` listing — this is **reporting**, runs after the workflow result
      returns (client-side, Step 3.7), not inside workflow code.

## Step 3.5 — Worker + client wiring

- [ ] `temporal/worker.py`: `Client.connect(..., plugins=[PydanticAIPlugin()])`;
      `Worker(client, task_queue='module-generation', workflows=[ModuleGenerationWorkflow],
      activities=[...all activities from 3.1's TemporalAgents + 3.2's effects wrappers],
      activity_executor=ThreadPoolExecutor(...))`. A second worker process (or the same one with
      a second `Worker` on the `docker-builds` queue) handles the Docker-build activity.
- [ ] `temporal/client.py`: thin `async def start_module_generation(tool_info, ...) ->
      WorkflowHandle` helper used by the CLI.

## Step 3.6 — CLI: Temporal client, shadow mode

- [ ] `generate-module.py`: gather `input()`/argparse as today (unchanged — still the synchronous
      pre-amble), then instead of `asyncio.run(self.module_agent.run(...))`, call
      `temporal.client.start_module_generation(...)` and await the result.
- [ ] Add a `--legacy` flag that runs the existing Phase 1/2 in-process async path
      (`ModuleAgent.run` directly) unchanged — an escape hatch for one release, per PLAN.md.
- [ ] `status.json` is still written (Step 3.4's shadow-mode activity), so `app/`'s Django UI
      (which tails it via subprocess + file polling) keeps working unmodified in this phase.

## Step 3.7 — Testing (suite must stay green throughout)

- [ ] `tests/test_temporal_agents.py`: construct every `TemporalAgent` from Step 3.1 outside
      a workflow and assert no `UserError` (catches id/name regressions cheaply, no Temporal
      server needed).
- [ ] `tests/test_workflow.py`: use `temporalio.testing.WorkflowEnvironment` (time-skipping) to run
      `ModuleGenerationWorkflow` end-to-end against `TestModel`-backed agents and mocked/`TestModel`
      activities. Cover: happy path; a validation failure that exhausts `MAX_ARTIFACT_LOOPS`; one
      cross-artifact escalation (reuse the existing `error_classifier` test fixtures from
      `tests/test_error_classifier.py`).
- [ ] **Crash-and-resume test**: kill the worker mid-`build_and_test_image` activity (or simulate
      via a forced worker restart in `WorkflowEnvironment`), restart, confirm the workflow
      completes — this is the actual payoff of the phase and should be a real, not aspirational,
      test.
- [ ] **Replay-determinism test**: record a workflow history, replay it, assert no
      non-determinism error — catches accidental `datetime.now()`/unsorted-iteration regressions
      the Step 3.4 port might reintroduce.
- [ ] Existing suites (`tests/test_module_orchestrator.py`, `tests/test_effects.py`, per-artifact
      linter checks) stay green **unchanged** — they test the legacy/`--legacy` path and the
      effects functions directly, neither of which this phase modifies.
- [ ] Gate: `uv run pytest -m "not live"` green after every step; new Temporal-dependent tests
      marked so they're skippable in environments without a Temporal server (`@pytest.mark.temporal`,
      excluded by default like `live`, following the existing `pytest.ini` pattern).

## Acceptance criteria

1. `uv run python -c "import temporalio"` succeeds; `pydantic-ai[temporal]` in `pyproject.toml`.
2. Every `TemporalAgent(...)` construction in `temporal/agents.py` succeeds with no
   `UserError` — verified by a fast, server-less unit test.
3. A real module generation completes end-to-end through
   `temporal.client.start_module_generation(...)`, producing the same artifact set as the
   `--legacy` path for the same inputs.
4. Kill-and-restart the worker mid-Docker-build; the workflow resumes and completes without
   manual intervention.
5. `uv run pytest -m "not live"` stays green throughout; new workflow/replay tests pass where a
   Temporal test environment is available.
6. `status.json` still exists after a run (shadow mode) — not yet removed (Phase 4).
7. Runtime code (`agents.py`, `activities.py`, `workflow.py`, `worker.py`, `client.py`,
   `__init__.py`) lives in `temporal/` alongside the existing phase docs — the deliberate,
   temporary coexistence described in the package-layout note above, not an accident.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Missed toolset `id=` surfaces late, mid-workflow-run | Step 3.1's server-less unit test catches it at agent-construction time, before any workflow runs |
| Sync activities (`agents/effects.py`) misconfigured without a thread executor | Explicit `activity_executor=ThreadPoolExecutor(...)` in Step 3.5; a worker startup smoke test |
| Docker-build activity times out on the shared default (60s) | Own `start_to_close_timeout`/`heartbeat_timeout` at the `workflow.execute_activity` call site (Step 3.4, not the `@activity.defn` decoration in Step 3.2), not left at the `TemporalAgent` default |
| Non-determinism reintroduced while porting `agents/module.py` into the workflow | PHASE2.md's determinism checklist is the punch list; replay-determinism test in Step 3.7 |
| Workflow-side `Logger` calls break replay | `WorkflowLogger` adapter (Step 3.3) used only in the workflow context; legacy path keeps the existing `Logger` |
| Agent names/toolset ids changed later, breaking in-flight workflow replay | Treat the Step 3.1 name list as a frozen contract from this point forward — call this out in the PR |
| Docs and runtime code co-located in `temporal/` reads as clutter or confuses contributors about what's plan vs. code | Documented explicitly as a deliberate, temporary state in the package-layout note; splitting the docs out later is a tracked follow-up, not a silent mess |

## Suggested commit sequence

1. `build: add pydantic-ai[temporal] extra, temporal/__init__.py` (3.0)
2. `feat(temporal): TemporalAgent wrappers + fix SkillsToolset ids` (3.1)
3. `feat(temporal): activity wrappers over agents.effects` (3.2)
4. `feat(temporal): workflow-safe logger` (3.3)
5. `feat(temporal): ModuleGenerationWorkflow` (3.4)
6. `feat(temporal): worker + client` (3.5)
7. `feat(cli): Temporal client path with --legacy fallback, shadow status.json` (3.6)
8. `test(temporal): agent construction, workflow, replay, crash-resume` (3.7)

After Phase 3, `status.json`/`--resume`/`--legacy` are removed in Phase 4, and the Django UI is
repointed from file-tailing to querying the Temporal client directly.
