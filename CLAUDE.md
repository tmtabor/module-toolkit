# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent Pydantic AI pipeline that turns a bioinformatics CLI tool into a complete, validated GenePattern module (wrapper script, manifest, paramgroups.json, GPUnit test, README, Dockerfile). Agents run Research → Planning → per-artifact Generation, each generation step validated by a dedicated linter with automatic retry and cross-artifact error escalation.

## Commands

Dependencies are managed with **uv** (Python 3.10+; `pyproject.toml` + `uv.lock` are
authoritative). `requirements.txt` is a pip fallback that mirrors the direct deps.

```bash
# Install (creates .venv from uv.lock; includes dev/test tools)
uv sync
uv sync --extra app          # also install the Django web-UI deps
# pip fallback (non-uv users): pip install -r requirements.txt

# Run the generator IN-PROCESS (no Temporal needed) — simplest for local dev
uv run python generate-module.py --legacy

# Run non-interactively (in-process)
uv run python generate-module.py --legacy --name samtools --version 1.19 --language c \
  --description "..." --repository-url URL --documentation-url URL

# Generate/regenerate only specific artifacts (fast iteration, skips slow Docker build)
uv run python generate-module.py --legacy --name mytool --skip-dockerfile

# Run the Django web UI (submits runs via the Temporal client -- needs a server + worker, see below)
uv run --extra app python app/manage.py runserver

# Run the MCP server that exposes the linters as tools
uv run python mcp/server.py
```

**Default path is Temporal.** Without `--legacy`, `generate-module.py` submits a Temporal
workflow and needs a server + worker running (see the Temporal section below). Use `--legacy`
for anything that shouldn't depend on Temporal infra. There is no `--resume` (removed in
temporal/PHASE4.md 4.5) -- a failed or interrupted run is retried by starting a fresh one.

```bash
# Durable (Temporal) path: start a server, start a worker, then run the CLI without --legacy
temporal server start-dev
uv run python -m temporal.worker      # serves both task queues; reads the same .env as the CLI
uv run python generate-module.py --name samtools --language c ...
```

### Tests

`pytest.ini` scopes bare `pytest` to `tests/` (the agent/orchestrator suite) and sets
`pythonpath = .` so the flat-layout packages import under `uv run pytest`.

```bash
uv run pytest                       # agents/orchestrator suite (tests/); live tests excluded by default
uv run pytest -m live               # include tests that make real LLM/API calls
uv run pytest tests/test_module_orchestrator.py::TestDoResearch   # a single class
```

The `<artifact>/tests/*.py` files are **not** a pytest suite — each defines `run_test(lines)` and
serves as the linter's rule engine (see the per-artifact package pattern below). They share
basenames and have no `__init__.py`, so batching them under pytest raises import-mismatch errors;
a single dir collects 0 items. Exercise a linter (and thereby its rule modules) by running it
against an example:

```bash
uv run python -m wrapper.linter  wrapper/examples/valid/sample_python_wrapper.py --parameters input output
uv run python -m manifest.linter manifest/examples/valid/manifest --wrapper path/to/wrapper.py
```

`wrapper/examples/{valid,invalid}`, `manifest/examples/{valid,invalid,real}`, `dockerfile/examples/{success,fail}`, etc. hold fixture artifacts used by these tests.

## Architecture

### Pipeline (`agents/module.py` — `ModuleAgent`)

`run()` drives three phases: Research → Planning → Artifact generation. It no longer writes
`status.json` or supports resuming (removed in temporal/PHASE4.md 4.5) — a failed or interrupted
`--legacy` run is retried by starting fresh.

1. **Research** (`agents/researcher.py`) — web search + doc analysis to characterize the tool.
2. **Planning** (`agents/planner.py`) — turns research into a structured `ModulePlan` (parameters, LSID, docker tag, wrapper filename, command line).
3. **Artifact generation** (`generate_all_artifacts` / `artifact_creation_loop`) — for each of `wrapper → manifest → paramgroups → gpunit → documentation → dockerfile → install` (zip + optional GenePattern upload), calls the artifact's Pydantic AI agent, writes the file, then validates it with the artifact's linter (up to `MAX_ARTIFACT_LOOPS` retries, error fed back into the next prompt).

**Cross-artifact escalation** (`agents/error_classifier.py`): when a downstream artifact fails (e.g. Dockerfile build errors on "unrecognized arguments"), `classify_error()` pattern-matches the error text against `ARTIFACT_DEPENDENCIES` to find the true upstream cause (e.g. a manifest/wrapper flag mismatch) and `generate_all_artifacts` reorders the queue to regenerate that upstream artifact first, injecting the downstream error as context — capped by `MAX_ESCALATIONS` per artifact pair. Missing packages are always routed to the Dockerfile, never the wrapper; wrapper escalation is reserved for structural/logic bugs in the generated script itself.

### Per-artifact package pattern

Every artifact type (`wrapper/`, `manifest/`, `paramgroups/`, `gpunit/`, `documentation/`, `dockerfile/`) follows the same internal shape:

- `agent.py` — a Pydantic AI `Agent` (`deps_type=ArtifactDeps`, from `agents/models.py`) with an `@agent.instructions` callback that injects tool_info/planning_data/error history/escalation context per attempt, plus a `create_<artifact>` tool the agent calls to emit the artifact and a `validate_<artifact>` tool wrapping the linter for in-loop self-checking.
- `linter.py` — the standalone validator, runnable via CLI (`if __name__ == "__main__"`) and imported directly by `agents/validator.py` (no subprocess).
- `tests/test_*.py` — **not pytest test cases**. Each file defines one `run_test(lines[, context]) -> List[LintIssue]` check function. `linter.py`'s `discover_tests()`/`run_modular_tests()` globs `tests/test_*.py` and dynamically imports+runs every one against the artifact — this is the linter's rule engine, not a test suite (pytest can still execute them directly since `run_test` is importable, but that's incidental). **To add a new lint rule, add a new `test_*.py` file to the artifact's `tests/` directory** — no changes to `linter.py` needed.
- `examples/{valid,invalid}` (or `success,fail`) — fixture artifacts referenced by the checks above.

`agents/module.py`'s `artifact_agents` dict is the single registry tying an artifact name to its agent, output Pydantic model, output filename, `validate_tool` key, and content formatter — start there when adding a new artifact type.

### Shared contracts (`agents/models.py`)

- `ArtifactDeps` — the dependency object every artifact agent receives via `RunContext`: `tool_info`, `planning_data` (serialized `ModulePlan`), `error_report`, `attempt`/`max_loops`, `example_data`, `downstream_error_context`, `error_history`.
- `ArtifactModel` — the default structured output (`code`, `artifact_report`, `artifact_status`, `meta`); `manifest`/`paramgroups` use their own richer models (`manifest/models.py::ManifestModel`, `paramgroups/models.py::ParamgroupsModel`) with custom `formatter`s in `artifact_agents`.
- `configured_llm_model()` reads `DEFAULT_LLM_MODEL` (`provider:model`, or `ollama:<model>` routed through `OllamaProvider`/`OLLAMA_BASE_URL`) — this is how every agent in the repo picks its model.

### Other pieces

- `mcp/server.py` — exposes the six linters as MCP tools (`validate_dockerfile`, `validate_manifest`, etc.) for use by external MCP clients (Claude Desktop, MCP Inspector); this is separate from the in-process validator (`agents/validator.py`) the pipeline itself uses.
- `agents/example_data.py` — resolves user-supplied `--data` files/URLs (with optional `::hint` semantic tags) into `ExampleDataItem`s used for planning context and Dockerfile runtime testing.
- `agents/status.py` — `ModuleGenerationStatus`/`ArtifactResult`. `to_dict()` is still live: it's what the Temporal workflow's `progress()` query and the CLI's Temporal-path final report serialize; the on-disk `status.json` it used to be dumped to is gone (4.5).
- `skills/gp-*` — Pydantic AI skills (`SkillsToolset`) loaded into the wrapper/manifest agents at *generation time* (LLM-facing instructions in `SKILL.md`), not Claude Code project skills. Don't confuse with `.github/skills/` (Claude Code development skills for this repo itself).
- `app/` — Django web UI; `generator/views.py` submits/polls runs via `temporal.client` (`start_module_generation`/`get_workflow_state`) — it needs a running Temporal server + worker, same as the CLI's default path. No subprocess, no `status.json`, no resume.
- `training/` — captured `plan.jsonl` / prompt-completion pairs from real runs, used for LoRA fine-tuning experiments; not part of the runtime pipeline.

### Temporal durable-execution layer (`temporal/`)

The `temporal/` directory holds **both** the phase docs (`PLAN.md`, `CONSIDERATIONS.md`,
`PHASE*.md`) **and** the runtime package (a deliberate, temporary coexistence). The runtime code
wraps the same pipeline without changing `agents/`:

- `temporal/agents.py` — the 8 pipeline agents wrapped in `TemporalAgent` with permanent `name=`s.
  The dockerfile hint-mapping agent is intentionally *not* wrapped (its LLM call runs inside a
  coarse activity).
- `temporal/activities.py` — the 12 `agents/effects.py` functions wrapped as `@activity.defn`, plus
  `build_dockerfile_runtime_command`.
- `temporal/workflow.py` — `ModuleGenerationWorkflow`, a port of `ModuleAgent`'s coordination:
  `agent.run(...)` → the TemporalAgent versions, `effects.*` → `workflow.execute_activity(...)`,
  `datetime.now()` → `workflow.now()`, `Logger` → `temporal/logger.py::WorkflowLogger`. In-flight
  progress is exposed via a `@workflow.query def progress()` (structured status + recent log tail),
  which clients poll instead of reading a file.
- `temporal/worker.py` (`uv run python -m temporal.worker`) / `temporal/client.py` — worker process
  and the client helpers (`start_module_generation`, `get_workflow_state`) the CLI and Django UI use.

Key gotchas when editing this layer: workflow-defining modules must be **side-effect-free at import
time** (the sandbox re-imports them); `requests`-importing modules need
`workflow.unsafe.imports_passed_through()`; never set a `heartbeat_timeout` on an activity that
doesn't call `activity.heartbeat()` (it will be killed at that interval); the worker reads the same
`.env` the CLI does because agents run there.

Phase 4 status: **done under Option B** (temporal/PHASE4.md) — the workflow's shadow `status.json`
is removed (the `progress` query is the source of truth for in-flight state); the Temporal-vs-
`--legacy` parity run (4.2) passed; the Django UI is repointed to the Temporal client (4.4); `--resume`
and `ModuleAgent.save_status`/`load_status` are removed (4.5). `--legacy`/`ModuleAgent` remain as
the tested, no-infra reference implementation (Option B's explicit tradeoff) — de-duplicating them
against the workflow is deferred to a Phase 5 (`temporal/PHASE4.md`'s "Suggested Phase 5").

### Domain conventions the generator agents must follow

- GenePattern parameter names use dots (`input.file`, not `input-file`); wrapper flags, manifest `pN_name`, and paramgroups must all use the identical dotted name — this is enforced by `manifest/tests/test_wrapper_consistency.py` and friends, and is why parameter names are treated as "locked" once planning produces them.
- Wrapper language is chosen by tool language, not agent preference: JVM tools (Java/Scala/Groovy/Kotlin) always get a **bash** wrapper that shells out to the tool's CLI, never a Java/Python wrapper.
- Generated wrapper scripts must be ASCII-only (no em-dashes, curly quotes, arrows) — GenePattern containers may run under an ASCII locale.

## Code style

This repo follows the local `python-style` Claude Code skill (`.github/skills/python-style/SKILL.md`) for hand-written Python: compact functional density, guard-clause early returns, single-line `if`/`else` for simple bodies, Python 3.10+ type hints (`list[str]`, `X | None`, never `List`/`Optional`), narrative block comments labeling logical phases plus aligned trailing `#` comments on sequential calls, `snake_case`/`PascalCase` naming, and `dict1 | dict2` for merges. Agent/toolset code follows the `building-pydantic-ai-agents` skill conventions (e.g. `@agent.tool` requires `RunContext` as the first param, `@agent.tool_plain` must not have one).
