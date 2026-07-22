# Temporal Integration — Phased Implementation Plan

A staged path to move the GenePattern Module AI Toolkit from its hand-rolled
`status.json` + `--resume` durability onto Pydantic AI's Temporal integration.

Read [CONSIDERATIONS.md](./CONSIDERATIONS.md) first — it justifies the approach and lists the
gotchas this plan is built to avoid.

## Guiding principles

- **Each phase ships and is independently valuable.** No phase leaves the toolkit broken.
- **Async-first, Temporal-second.** The largest single change (sync → async) is done and
  validated *before* Temporal enters the picture, so failures are easy to attribute.
- **Temporal replaces `status.json`, it does not run alongside it forever.** A shadow period is
  allowed (Phase 3) but removal is the goal (Phase 4).
- **Preserve behaviour parity.** The escalation logic, retry caps, and validation contracts are
  correct today; the refactor must not change *what* the pipeline decides, only *how* it is
  executed and recovered.

## Current-state anchors (what we are refactoring)

| Concern | Location today |
|---|---|
| Orchestration | `agents/module.py` → `ModuleAgent.run()`, `generate_all_artifacts()`, `artifact_creation_loop()` |
| Agent calls (sync) | `agents/researcher.py`, `agents/planner.py`, `<artifact>/agent.py` (all `run_sync`) |
| Deps contract | `agents/models.py` → `ArtifactDeps` (already a Pydantic `BaseModel`) |
| Model selection | `agents/models.py` → `configured_llm_model()` (returns an instance for `ollama:`) |
| Escalation (pure) | `agents/error_classifier.py` → `classify_error`, `should_escalate`, `ARTIFACT_DEPENDENCIES` |
| Durability (to replace) | `save_status`/`load_status` + `status.json`, `--resume` flag |
| Side effects | `download_url_data` (requests), Docker build/run in `dockerfile/runtime.py` + linter, `upload_to_genepattern`, `zip_artifacts`, all file writes |
| Entry points | `generate-module.py` (CLI, interactive `input()`), `app/` (Django UI, tails `status.json`) |

---

## Phase 0 — Spike & decision lock (0.5–1 day)

**Goal:** de-risk before committing; confirm Temporal (vs. DBOS) and prove the wrapping works.

- [ ] Stand up a local Temporal dev server (`temporal server start-dev`) and a Jaeger container
      (already documented in the README) for trace comparison.
- [ ] Minimal spike: wrap **one** agent (`researcher_agent`) in `TemporalAgent`, run it inside a
      throwaway `PydanticAIWorkflow` against `TestModel`, confirm activities appear in the
      Temporal UI.
- [ ] Confirm the model-serialization path for `ollama:` — register the instance via
      `TemporalAgent(models={...})` and verify replay works.
- [ ] **Decision gate:** Temporal vs. DBOS (see CONSIDERATIONS.md "Decision" section). If the
      driver is only crash-safety on a single host, stop and re-scope to DBOS. Otherwise proceed.

**Exit criteria:** a green spike workflow + a written go/no-go on the backend choice.

---

## Phase 1 — Async conversion (no Temporal yet) (2–4 days)

**Goal:** eliminate `run_sync` and make `ModuleAgent` fully `async`. This is the bulk of the
mechanical work and is valuable on its own (enables concurrency, is a prerequisite for Temporal).

- [ ] Convert every `agent.run_sync(...)` to `await agent.run(...)`:
      - `agents/researcher.py` usage in `do_research`
      - `agents/planner.py` usage in `do_planning`
      - `agent.run_sync` in `artifact_creation_loop`
- [ ] Make `do_research`, `do_planning`, `artifact_creation_loop`, `generate_all_artifacts`,
      and `run` `async def`; thread `await` through call sites.
- [ ] Keep all I/O exactly where it is for now (still direct `subprocess`/`requests`/`open`) —
      this phase changes concurrency model only, not I/O placement.
- [ ] Update `generate-module.py` to drive the async entry point via `asyncio.run(...)`; keep
      interactive `input()` in the synchronous pre-amble before the async run starts.
- [ ] Update the test suite: `pytest.ini` already sets `asyncio_mode = auto`; convert
      orchestrator tests in `tests/` to `async def` where they invoke the now-async methods.

**Exit criteria:** full pipeline runs end-to-end async; `tests/` green; a real module still
generates identically (byte-compare a known-good output against a pre-refactor run).

---

## Phase 2 — Extract side effects into activity-ready functions (2–3 days)

**Goal:** isolate all non-deterministic I/O behind clean, individually-callable functions with
serializable inputs/outputs — **without** introducing Temporal yet. This makes Phase 3 a
wrapping exercise instead of a rewrite.

- [ ] Carve out pure functions (path-in, result-out) for each side effect:
      - `download_url_data` → `download_one(url, dest_dir) -> DownloadResult`
      - linter validation → `run_linter(validate_tool, file_path, extra_args) -> ValidationResult`
        (wraps `agents/validator.py`)
      - Docker build + runtime test → `build_and_test_image(spec) -> BuildResult`
        (wraps `dockerfile/runtime.py`)
      - `upload_to_genepattern` → keep signature, ensure inputs/outputs are serializable
      - `zip_artifacts` → returns the zip path
- [ ] Define **serializable I/O models** (Pydantic) for each of the above. Enforce the ~2 MB
      payload rule now: pass **file paths**, never file contents, across these boundaries.
      (See gotcha #2 in CONSIDERATIONS.md.)
- [ ] Replace direct `datetime.now()` in directory naming with an injected timestamp argument
      (so Phase 3 can source it from `workflow.now()`).
- [ ] Audit `@agent.tool` functions (e.g. `validate_wrapper`) for use of `RunContext.model`
      / `.prompt` / `.messages` / `.tracer`; refactor any away (limited under Temporal).
- [ ] Keep `status.json` intact — still the durability layer this phase.

**Exit criteria:** every side effect is a standalone function with a Pydantic input/output; no
behaviour change; `tests/` and per-artifact linter suites green.

---

## Phase 3 — Introduce Temporal (workflow + activities, shadow mode) (3–5 days)

**Goal:** run the real pipeline under Temporal, with `status.json` still written as a shadow so
we can fall back and compare.

- [ ] Add the dependency: `pydantic-ai[temporal]` in `requirements.txt`.
- [ ] Give every agent a stable `name=` (`researcher`, `planner`, `wrapper`, `manifest`,
      `paramgroups`, `gpunit`, `documentation`, `dockerfile`) and stable toolset `id`s. **These
      names are permanent** — changing them later breaks in-flight workflow replay.
- [ ] Wrap each agent in `TemporalAgent`; register the `ollama` model instance via
      `TemporalAgent(models={...})`.
- [ ] Author `temporal/workflow.py`:
      - `@workflow.defn class ModuleGenerationWorkflow(PydanticAIWorkflow)` with
        `__pydantic_ai_agents__ = [...]`.
      - Port `ModuleGenerationStatus` phase progression, `artifact_creation_loop` retry counting,
        and the `generate_all_artifacts` escalation queue-reordering into `@workflow.run`. This is
        pure coordination — `classify_error`/`should_escalate` run in-workflow unchanged.
- [ ] Author `temporal/activities.py`: decorate the Phase-2 functions with `@activity.defn`.
      Give the Docker build activity a generous `start_to_close_timeout` and heartbeating;
      route it to a task queue whose worker has Docker available.
- [ ] Author `temporal/worker.py`: register workflow + activities with `PydanticAIPlugin`.
- [ ] `generate-module.py` becomes a **Temporal client**: gather `input()` up front, then
      `client.start_workflow(...)` / `execute_workflow(...)`. Keep a `--legacy` flag that runs the
      Phase-1 in-process async path for one release, as an escape hatch.
- [ ] **Shadow mode:** activities still write `status.json` so the Django UI keeps working and
      output is byte-comparable to the legacy path.
- [ ] Testing: use Temporal's `WorkflowEnvironment` (time-skipping) for workflow tests;
      `TestModel` continues to back the agents. Add a workflow-determinism test (replay a recorded
      history) to catch accidental non-determinism.

**Exit criteria:** a real module generates end-to-end under Temporal; crash-and-resume works
(kill the worker mid-Docker-build, restart, run completes); output matches the legacy path.

---

## Phase 4 — Make Temporal the source of truth; remove `status.json` (2–3 days)

**Goal:** delete the hand-rolled durability layer and repoint consumers at Temporal.

- [ ] Remove `save_status`/`load_status`, the `status.json` writes, and the `--resume` flag.
      Resume is now "start/signal the workflow" — Temporal replays automatically.
- [ ] Repoint the Django UI (`app/generator/views.py`) from tailing `status.json` to querying
      workflow state via the Temporal client; replace live log streaming with
      `event_stream_handler` (direct streaming is unavailable under Temporal — gotcha #4).
- [ ] Remove the `--legacy` escape hatch once the Temporal path has soaked.
- [ ] Confirm Logfire spans still emit and now nest under Temporal's own tracing
      (`configure_telemetry()` is retained and composes).
- [ ] Update `README.md` (Resume & output flags, Web UI, Observability sections) and `CLAUDE.md`
      (pipeline architecture) to describe the Temporal model.

**Exit criteria:** no `status.json` anywhere; CLI + Django UI both drive Temporal; docs updated.

---

## Phase 5 — Production hardening (ongoing)

**Goal:** exploit what Temporal makes newly possible. See [PHASE5.md](./PHASE5.md) for the
staged plan, grounded in what Phases 1-4 actually shipped (including the Option B deviation —
`--legacy`/`ModuleAgent` were kept rather than removed) and in problems already observed live
during Phase 4 testing (payload-size warnings, a workflow terminated for exceeding history size).

- [ ] Per-activity retry policies with backoff (LLM rate limits, transient Docker/registry
      failures) replacing the in-loop `MAX_ARTIFACT_LOOPS` retry where appropriate — keep
      escalation semantics (`MAX_ESCALATIONS`) in the workflow.
- [ ] **Human-in-the-loop gate**: a workflow signal to approve GenePattern upload before the
      `install` pseudo-artifact runs (a natural fit now that the upload is an activity).
- [ ] Concurrency: run multiple module generations in parallel across workers; validate the
      shared-storage decision from Phase 2 (gotcha #2) under real parallelism.
- [ ] Resolve the shared-filesystem question definitively — either a shared volume/object store
      for the module directory, or pin generation to a single worker host.
- [ ] Dashboards/alerts on workflow failure rate, activity retry counts, and end-to-end latency.

---

## Effort summary

| Phase | Focus | Rough effort | Ships? |
|---|---|---|---|
| 0 | Spike & backend decision | 0.5–1 day | Decision doc |
| 1 | Async conversion | 2–4 days | Yes (async pipeline) |
| 2 | Extract side effects | 2–3 days | Yes (cleaner internals) |
| 3 | Temporal in shadow mode | 3–5 days | Yes (Temporal-backed, `status.json` shadow) |
| 4 | Remove `status.json` | 2–3 days | Yes (Temporal is source of truth) |
| 5 | Hardening | Ongoing | Incremental |

## Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Async conversion introduces subtle ordering bugs | 1 | Byte-compare output against a pre-refactor golden run |
| Non-determinism sneaks into the workflow (`datetime`, `iterdir`, direct I/O) | 3 | Replay-determinism test on recorded history; all I/O in activities |
| Agent `name`/toolset `id` churn breaks in-flight replay | 3+ | Freeze names in Phase 3; treat as a permanent contract |
| Docker build activity times out / worker lacks Docker | 3 | Dedicated task queue + generous timeout + heartbeating |
| Payload size (research text, wrapper source, data files) exceeds ~2 MB | 2 | Pass by path on shared storage, never by value |
| Django UI streaming regresses | 4 | Migrate to `event_stream_handler` before removing `status.json` |
