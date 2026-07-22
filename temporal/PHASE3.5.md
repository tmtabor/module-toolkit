# Phase 3.5 — Harden the Temporal Path (make the default safe for a real run)

Phase 3 ([PHASE3.md](./PHASE3.md)) delivered a structurally-correct Temporal integration whose
tests pass, but a review pass found gaps that would break an actual (live-LLM, real-docker,
real-download) generation — none of which the Phase 3 tests exercise, because they run against a
docker-less, `TestModel`-backed `MiniWorkflow`. This phase closes those gaps so the Temporal path
is genuinely safe to be the CLI default, and records what remains for Phase 4.

Nothing here is a rewrite; every item is a targeted fix to Phase 3's output. Suite must stay green
(`uv run pytest -m "not live"`) throughout.

## The findings this phase addresses

| # | Sev | Finding | Fix location |
|---|-----|---------|--------------|
| H0 | **High** | **Heartbeat bug (verified):** the docker-build activity sets `heartbeat_timeout=30s` but the effect never calls `activity.heartbeat()`. Temporal fails an activity that declares a heartbeat timeout but doesn't heartbeat — so the build dies at 30s despite the 20-min `start_to_close_timeout`. Confirmed in a `WorkflowEnvironment` spike this session. | `temporal/workflow.py` |
| H1 | **High** | Worker process never calls `load_dotenv()` / `configure_telemetry()`. The worker is where agents actually run, so it reads `DEFAULT_LLM_MODEL`/`OLLAMA_BASE_URL`/`BRAVE_API_KEY`/GP creds/token-cost vars from bare OS env (uv doesn't auto-load `.env`) and emits no Logfire traces. | `temporal/worker.py` |
| H2 | **High** | Django web UI silently broken: `app/generator/views.py` shells out to `generate-module.py` **without** `--legacy`, so every web run now needs a Temporal server+worker or it fails/hangs. | `app/generator/views.py` |
| H3 | **High** | `download_one` runs under the 60s default timeout; multi-GB genomic inputs (the README's own `--data` examples are URLs) exceed it, get killed, and retry from scratch. | `temporal/workflow.py` |
| M4 | Medium | No-worker hang: `client.start_workflow` sets no `execution_timeout`; if the server is up but no worker polls the queue, `handle.result()` blocks forever (the CLI's try/except only catches connection errors). | `temporal/client.py`, `generate-module.py` |
| M5 | Medium | `dockerfile_hint_mapping_agent` (`TemporalAgent`) is registered but never invoked; the hint-mapping LLM call actually runs as a plain, non-durable agent call inside the `build_dockerfile_runtime_command` activity. Dead registration + a model request that isn't a durable activity. | `temporal/agents.py`, `temporal/activities.py` |
| M6 | Medium | Zero docs for the Temporal default, worker startup, or `--legacy`. | `README.md`, `CLAUDE.md`, `app/README.md` |
| M7 | Medium | Shared-filesystem assumption undocumented: the worker creates the module dir and writes all artifacts on its own disk; a client on another host gets a success result pointing at a directory it can't see. | docs |
| L8 | Low | pip-fallback `requirements.txt` pins `pydantic-ai>=1.77.0` (no `[temporal]`) → `ModuleNotFoundError: temporalio` for non-uv users on the default path. | `requirements.txt` |
| L9 | Low | ~15+ `save_status_snapshot` activity round-trips per run (shadow mode). Perf, not correctness; disappears in Phase 4. | (defer) |
| L10 | Low | `.env.example` doesn't mention `TEMPORAL_ADDRESS`. | `.env.example` |

## Step 3.5.1 — Fix the activity-timeout bugs (H0, H3)

- [ ] **H0:** In `temporal/workflow.py`, stop declaring `heartbeat_timeout` on the docker-build
      call while the activity doesn't heartbeat. Two acceptable options:
      - **Minimal (recommended for this phase):** remove `heartbeat_timeout` from the
        `build_and_test_image` call; rely on a generous `start_to_close_timeout`
        (`_DOCKER_BUILD_TIMEOUT`, already 20 min — consider making it configurable via env).
        A hung build then fails at `start_to_close_timeout` instead of falsely at 30s.
      - **Better (optional, larger):** make the build genuinely heartbeat. Because
        `agents/effects.py` must stay Temporal-agnostic (Phase 2 rule), the heartbeating belongs
        in a Temporal-aware wrapper in `temporal/activities.py` (e.g. run the blocking
        `docker build` in a thread and `activity.heartbeat()` on a timer / per output line), not
        in `effects.build_and_test_image`. Only then is a short `heartbeat_timeout` safe.
- [ ] **H3:** Give `download_one` its own generous `start_to_close_timeout` (e.g. a
      `_DOWNLOAD_TIMEOUT`, 30–60 min, env-configurable) at the call site in `run()`. Same
      heartbeat caveat as H0 — don't set `heartbeat_timeout` unless the activity actually
      heartbeats.
- [ ] Audit every other `self._act(...)` call site for activities that can legitimately run long:
      `run_linter` for the **documentation** artifact fetches remote URLs; `upload_module` posts a
      zip. Bump those specific call sites off the 60s default as needed. Fast linters
      (manifest/paramgroups/gpunit/wrapper) can keep the default.
- [ ] Add a regression test (marked `temporal`) that asserts a long-but-progressing activity under
      the chosen timeout policy completes (the exact scenario H0's spike showed failing).

## Step 3.5.2 — Worker process configuration (H1)

- [ ] At the very top of `temporal/worker.py`, **before** importing `temporal.workflow` /
      `temporal.agents` (which construct agents via `configured_llm_model()` at import time), call
      `load_dotenv()` and `configure_telemetry()`. Import order is load-bearing: config is read
      when the agent modules import, so `.env` must be loaded first.
- [ ] Telemetry: `configure_telemetry()` handles pydantic-ai instrumentation. Additionally
      consider adding `LogfirePlugin` (exported from `pydantic_ai.durable_exec.temporal`) to the
      worker's plugin list for Temporal workflow/activity spans. Decide whether to include it now
      or note as follow-up.
- [ ] Document the worker's required environment (same vars the CLI needs) in the worker module
      docstring and the README, since it's now a separate process the operator must configure.

## Step 3.5.3 — No-worker UX (M4)

- [ ] In `temporal/client.py`, set a bounded `execution_timeout` on `start_workflow` (env-
      configurable) so a submission with no worker eventually errors instead of hanging forever.
- [ ] In `generate-module.py::run_via_temporal`, print a clear "submitted to task queue
      '{queue}', waiting for a worker…" line before awaiting `handle.result()`, and turn a
      timeout into an actionable message ("no worker appears to be serving queue X; start one with
      `uv run python -m temporal.worker`, or use --legacy").
- [ ] (Optional) Best-effort pre-flight: query the task queue's pollers via the client and warn
      immediately if none are present, rather than waiting for the timeout.

## Step 3.5.4 — Unbreak the Django app (H2)

- [ ] Minimal, low-risk fix for this phase: add `--legacy` to the subprocess command in
      `app/generator/views.py` so the web UI keeps running the pipeline in-process exactly as it
      did pre-Phase-3. This restores the status quo without requiring the app operator to run a
      worker.
- [ ] Note in `app/README.md` that the UI currently uses the in-process (`--legacy`) path; the
      proper repoint to the Temporal client (with live progress from workflow queries instead of
      tailing `status.json`) is Phase 4 work.

## Step 3.5.5 — Resolve the hint-mapping agent inconsistency (M5)

Pick one and apply it:

- [ ] **Option A (recommended, minimal):** drop `dockerfile_hint_mapping_agent` from
      `temporal/agents.py` (`ALL_TEMPORAL_AGENTS` and the standalone binding), since nothing
      invokes it and it only registers dead activities. Document in `build_dockerfile_runtime_command`
      that its internal hint-mapping LLM call is intentionally a plain, non-durable call inside
      the coarse activity (an accepted tradeoff already noted there).
- [ ] **Option B (if that model request must be durable):** hoist the hint-mapping call out of
      the coarse activity and have the *workflow* invoke `dockerfile_hint_mapping_agent.run(...)`
      directly, passing the result into a slimmed `build_runtime_command` that no longer makes the
      LLM call. Larger change; only do it if durability/retry of that specific call matters.

## Step 3.5.6 — Documentation (M6, M7)

- [ ] `README.md`: add a "Running with Temporal" section — start a server (`temporal server
      start-dev`), start a worker (`uv run python -m temporal.worker`), then run the CLI; explain
      `--legacy` for the in-process path; document `TEMPORAL_ADDRESS`.
- [ ] `CLAUDE.md`: add the Temporal architecture (workflow/activities/worker/client split, the
      `temporal/` package holding both docs and runtime code) and the worker/`--legacy` commands.
- [ ] Document the **shared-filesystem limitation (M7)** explicitly: worker and client must share
      a filesystem (or run co-located) because the module is written on the worker; distributed
      workers need shared storage — deferred (Phase 5 in PLAN.md).

## Step 3.5.7 — Polish (L8, L10)

- [ ] `requirements.txt` (pip fallback): change `pydantic-ai>=1.77.0` →
      `pydantic-ai[temporal]>=1.77.0` to match `pyproject.toml`.
- [ ] `.env.example`: add `TEMPORAL_ADDRESS` (and note the worker reads the same LLM/GP vars).

## Step 3.5.8 — Actually run the end-to-end parity check (Phase 3 acceptance #3, never executed)

- [ ] With a local/deterministic model (e.g. a running `ollama`) **and** a worker, run one real
      generation via the Temporal path and one via `--legacy` for the same inputs, and compare the
      produced artifact set. This is the shadow-mode comparison the retained `status.json` was
      meant to enable — it has not been run even once. Record the result; if they diverge, that's
      a port bug to fix here.

## What stays in Phase 4 (not this phase)

- Remove `status.json` / `save_status_snapshot` / `--resume` / `--legacy` once the Temporal path
  is trusted (this also erases L9's chattiness).
- Repoint the Django UI from subprocess+`--legacy` to the Temporal client with live workflow-query
  progress.
- Distributed shared-storage for the module directory (M7's real fix), if multi-host workers are
  desired (PLAN.md Phase 5).

## Acceptance criteria

1. The docker-build and download activities complete for realistically-long durations (no false
   30s/60s timeout); a `temporal`-marked regression test covers the timeout policy.
2. A worker started with `uv run python -m temporal.worker` in a shell that only has `.env`
   (not exported vars) uses the configured model/keys and emits telemetry.
3. The Django UI generates a module again (via `--legacy`) with no Temporal server running.
4. With no worker, the CLI errors with an actionable message within a bounded time instead of
   hanging.
5. No dead/never-invoked `TemporalAgent` remains registered (M5 resolved).
6. README + CLAUDE.md document the worker, `--legacy`, `TEMPORAL_ADDRESS`, and the
   shared-filesystem limitation; pip `requirements.txt` carries the `temporal` extra.
7. A real Temporal-vs-`--legacy` parity run has been executed and its outcome recorded (3.5.8).
8. `uv run pytest -m "not live"` stays green.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Removing `heartbeat_timeout` hides a genuinely hung build until `start_to_close` | Acceptable for this phase (a false 30s kill is strictly worse); real heartbeating is the documented "better" option in 3.5.1 |
| Long download/build timeouts let a wedged activity tie up a worker slot | Make timeouts env-configurable; add real heartbeating later; workers can scale |
| `--legacy` in the Django app is a stopgap that masks the not-yet-done repoint | Explicitly labelled a stopgap in `app/README.md`; tracked as Phase 4 |
| Parity run (3.5.8) needs infra (model + worker) not present in CI | Run it manually/locally; document the result; keep the structural `temporal` tests as the CI gate |

## Suggested commit sequence

1. `fix(temporal): correct docker-build/download activity timeouts (heartbeat bug)` (3.5.1)
2. `fix(temporal): load .env + configure telemetry in the worker` (3.5.2)
3. `fix(temporal): bound workflow start + actionable no-worker message` (3.5.3)
4. `fix(app): pin Django UI to the --legacy in-process path for now` (3.5.4)
5. `refactor(temporal): drop unused hint-mapping TemporalAgent` (3.5.5)
6. `docs: Temporal worker/--legacy/limitations; pip extra; .env.example` (3.5.6, 3.5.7)
7. `test(temporal): activity-timeout regression + record parity run` (3.5.1, 3.5.8)
