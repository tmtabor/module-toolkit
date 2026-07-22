# Phase 1 — Async Conversion (no Temporal): Implementation Plan

Converts the pipeline from `run_sync` to `async`/`await` end-to-end. This is the prerequisite
for Temporal (`TemporalAgent` is async-only) but ships as a standalone improvement. **No Temporal
code is introduced in this phase.**

See [PLAN.md](./PLAN.md) Phase 1 and [CONSIDERATIONS.md](./CONSIDERATIONS.md) ("async-only")
for context.

## Scope discovered by code audit

There are **exactly four `run_sync` call sites in production code** — one more than the naive
count, because the fourth is hidden:

| # | Location | Function | Note |
|---|---|---|---|
| 1 | `agents/module.py:310` | `do_research` | `researcher_agent.run_sync(prompt)` |
| 2 | `agents/module.py:385` | `do_planning` | `planner_agent.run_sync(prompt)` |
| 3 | `agents/module.py:552` | `artifact_creation_loop` | `agent.run_sync(prompt, deps=...)` |
| 4 | `dockerfile/runtime.py:103` | `_llm_hint_mapping` (called by `build_runtime_command`, invoked from `artifact_creation_loop:629`) | **Critical:** a nested `run_sync` that would sit *inside* an async call path |

### Why #4 is the one that will bite

`build_runtime_command` is called from inside `artifact_creation_loop`. Once that loop is `async`
and running under an event loop, the nested `_hint_mapping_agent.run_sync()` will raise
`RuntimeError: this event loop is already running` (Pydantic AI's `run_sync` cannot execute while
a loop is active). So the runtime chain **must** be converted too — it is not optional.

### Out of scope (intentionally unchanged)

- `training/planner/build_research_prompts.py:213` — standalone offline script, never called from
  the pipeline. Leave as `run_sync`.
- All synchronous I/O helpers (`save_status`, `load_status`, `download_url_data`, `zip_artifacts`,
  `upload_to_genepattern`, `docker_push`, `validate_artifact`, `_sync_wrapper_script`,
  `create_module_directory`, `print_final_report`, `add_usage`). Phase 1 keeps I/O where it is;
  calling these blocking helpers from `async` functions is acceptable now (they become activities
  in Phase 3). Converting them is explicitly deferred.

---

## Step 0 — Establish a GREEN baseline first (blocker; do before any refactor)

The current environment does **not** produce a green baseline, so "all tests pass" is
unverifiable until this is fixed. Audit findings in this checkout:

- `pydantic_ai` is **1.73.0**; `requirements.txt` pins **`>=1.77.0`**.
- `pydantic_ai_skills` is **not installed** → `tests/test_artifact_agents.py` and
  `tests/test_module_orchestrator.py` fail at **collection** (`ModuleNotFoundError`).
- `pytest-asyncio` is **not installed** → pytest prints `Unknown config option: asyncio_mode`,
  meaning `async def` tests would be silently skipped/errored. This phase's new async tests
  depend on it.

Actions:

- [ ] `pip install -r requirements.txt` (upgrades `pydantic-ai`, adds `pydantic-ai-skills` and
      `pytest-asyncio`). Prefer a fresh venv/conda env to avoid the global anaconda drift.
- [ ] Confirm `asyncio_mode = auto` in `pytest.ini` is now honored (no "Unknown config option"
      warning). With `auto`, `async def test_*` functions are collected without per-test
      decorators.
- [ ] Record the baseline green result of the full suite **before touching code**:

      ```bash
      pytest -m "not live"                 # agents/orchestrator suite (tests/)
      pytest wrapper/tests manifest/tests dockerfile/tests \
             gpunit/tests documentation/tests paramgroups/tests
      ```

- [ ] If the `pydantic-ai` 1.73→1.77+ upgrade itself breaks a test, fix that **here**, as a
      baseline concern, isolated from the async work. (This is why Step 0 precedes Step 1.)

**Exit criteria:** every non-live test passes on an unmodified tree. This is the reference point
the rest of the phase must preserve.

---

## Step 1 — Convert `dockerfile/runtime.py` (bottom of the call graph first)

Converting bottom-up means each step compiles and tests against already-async dependencies.

- [ ] `_llm_hint_mapping(...)` → `async def`; change `_hint_mapping_agent.run_sync(prompt)` to
      `await _hint_mapping_agent.run(prompt)`.
- [ ] `build_runtime_command(...)` (line 307) → `async def`; `await` the `_llm_hint_mapping` call.
- [ ] Grep for every caller of `build_runtime_command` and note them for Step 2
      (`agents/module.py:629` via the `ModuleAgent.build_runtime_command` wrapper at line 785).

**Verify:** `pytest dockerfile/tests` stays green. If any test calls `build_runtime_command`
directly, convert that test to `async def` (auto-collected under `asyncio_mode = auto`).

---

## Step 2 — Convert `agents/module.py` (the orchestrator)

Make the agent-calling methods and everything up the chain to `run()` async. Leave the sync I/O
helpers alone.

- [ ] `do_research` → `async def`; `await researcher_agent.run(prompt)`.
- [ ] `do_planning` → `async def`; `await planner_agent.run(prompt)`.
- [ ] `artifact_creation_loop` → `async def`; `await agent.run(prompt, deps=deps_context)`.
- [ ] `ModuleAgent.build_runtime_command` wrapper (line 775) → `async def`; `await` the delegate.
- [ ] Update the call site at line 629 to `await self.build_runtime_command(...)`.
- [ ] `generate_all_artifacts` → `async def`; `await self.artifact_creation_loop(...)`.
      (`_run_install_artifact` calls zip/upload only — keep sync, but it is `await`-ed callers'
      concern: it is called from `generate_all_artifacts`; since it makes no agent calls it can
      stay sync and be called without `await`. Confirm it contains no agent call before leaving
      it sync.)
- [ ] `run` → `async def`; `await self.do_research(...)`, `await self.do_planning(...)`,
      `await self.generate_all_artifacts(...)`.
- [ ] Leave unchanged (still sync, called directly from async — OK): `save_status`,
      `load_status`, `create_module_directory`, `download_url_data`, `zip_artifacts`,
      `upload_to_genepattern`, `docker_push`, `validate_artifact`, `_sync_wrapper_script`,
      `print_final_report`.

**Verify after this step:** the orchestrator unit tests will now fail (they call the old sync
signatures) — that is expected and fixed in Step 4. Confirm the module still **imports** cleanly
(`python -c "import agents.module"`) and that `pytest dockerfile/tests` and the artifact linter
suites remain green (they do not touch `module.py`).

---

## Step 3 — Convert the entry point `generate-module.py`

`main()` does synchronous work (arg parsing, interactive `input()`) and then calls
`module_agent.run(...)`. Keep `main()` synchronous and bridge to the coroutine at the two return
sites so the interactive pre-amble is untouched.

- [ ] `import asyncio` at the top.
- [ ] Wrap both `return self.module_agent.run(...)` calls (resume path ~line 247 and fresh path
      ~line 270) as `return asyncio.run(self.module_agent.run(...))`.
- [ ] Leave `if __name__ == "__main__": sys.exit(script.main())` as-is.

**Verify:** `python generate-module.py --help` runs; a real end-to-end smoke (below) is deferred
to Step 5.

---

## Step 4 — Update the tests that must change

Only **one** test file exercises the now-async orchestrator methods and must change. The
agent-level tests call `run_sync` on the *unchanged* agent objects in *sync* test functions and
**stay as-is** (minimal churn).

### `tests/test_module_orchestrator.py` — MUST change

It currently `patch`es `researcher_agent`/`planner_agent` and sets `mock_ra.run_sync.return_value`,
then calls `agent.do_research(...)` synchronously. After the refactor:

- [ ] Every test method that calls `do_research`, `do_planning`, `artifact_creation_loop`, or
      `generate_all_artifacts` becomes `async def` and `await`s the call (auto-collected via
      `asyncio_mode = auto`).
- [ ] Replace `.run_sync` mocks with `.run` as an **`AsyncMock`** returning the `mock_result`
      (e.g. `mock_ra.run = AsyncMock(return_value=mock_result)`; for the failure cases,
      `AsyncMock(side_effect=RuntimeError(...))`). Import `AsyncMock` from `unittest.mock`.
- [ ] Update the prompt-assertion helpers that read `mock_ra.run_sync.call_args` to read
      `mock_ra.run.call_args` (the prompt is still positional arg 0).
- [ ] The `save_status`/`load_status` round-trip tests stay **sync** — those methods did not
      change.
- [ ] For `artifact_creation_loop`/`generate_all_artifacts` tests using `monkeypatch.setattr(..., "run_sync", ...)`, switch to patching `"run"` with an async callable.

### Stays unchanged (verify, do not edit)

- [ ] `tests/test_researcher_agent.py`, `tests/test_planner_agent.py`,
      `tests/test_artifact_agents.py` — sync tests calling `<agent>.run_sync(...)` with a
      `TestModel` override. The agents themselves are not modified, so `run_sync` in a sync test
      is still valid. Leave them (a later cleanup pass may unify style; not required for parity).
- [ ] `tests/test_websearch_live.py` — `live`-marked, deselected by default. Unchanged.
- [ ] `tests/conftest.py` — only a docstring at line 84 mentions "run_sync"; optional cosmetic
      edit, no functional change.

**Verify:** `pytest -m "not live"` is fully green again — this restores the Step 0 baseline with
async code underneath.

---

## Step 5 — Full verification & behaviour-parity check

- [ ] Full non-live suite green:

      ```bash
      pytest -m "not live"
      pytest wrapper/tests manifest/tests dockerfile/tests \
             gpunit/tests documentation/tests paramgroups/tests
      ```

- [ ] Import/smoke: `python -c "import agents.module, dockerfile.runtime, generate_module"`
      (note: file is `generate-module.py`; import via `runpy`/`--help` instead).
- [ ] End-to-end parity smoke against a deterministic or local model (not the paid path):
      run one generation with `--skip-dockerfile` (avoids the slow Docker build) pointed at a
      local `ollama:` model or a `TestModel`-backed harness, and confirm it completes and writes
      the same artifact set as a pre-refactor run. Byte-compare where the model is deterministic.
- [ ] Confirm resume still works: start a run, interrupt after planning, `--resume` the module
      directory, verify it skips completed phases (this exercises the still-sync
      `load_status`/`save_status` from the new async `run`).
- [ ] (Optional) `python -W error::RuntimeWarning generate-module.py --help` to surface any
      un-awaited coroutine warnings.

---

## Acceptance criteria

1. Zero `run_sync` calls remain in `agents/module.py` and `dockerfile/runtime.py`
   (`grep -rn run_sync agents/ dockerfile/` returns nothing).
2. `training/` and the sync agent-level tests are intentionally still on `run_sync` and pass.
3. `pytest -m "not live"` and all per-artifact linter suites are green — matching the Step 0
   baseline.
4. A generation runs end-to-end via `asyncio.run(...)` from the CLI, and `--resume` works.
5. No Temporal dependency or import has been added (verified by `grep -rn temporal .` being empty
   outside `temporal/*.md`).

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Missing deps make "all tests pass" unverifiable | **Step 0** provisions the env and locks a green baseline before any edit |
| `pydantic-ai` 1.73→1.77+ upgrade changes behaviour | Isolated in Step 0, before async work, so regressions are attributable to the upgrade not the refactor |
| Hidden nested `run_sync` in `dockerfile/runtime.py` crashes only at runtime under the event loop | Converted explicitly in **Step 1**, bottom-up, before its async callers exist |
| `async def` tests silently skipped | `pytest-asyncio` installed + `asyncio_mode = auto` already set in `pytest.ini`; verified in Step 0 |
| A sync I/O helper accidentally made async (scope creep) | Explicit "leave unchanged" lists in Steps 1–2; acceptance criterion #1 scopes the grep to two files |
| Un-awaited coroutine slips through (returns a coroutine object instead of running) | Optional `RuntimeWarning`-as-error smoke in Step 5; orchestrator tests await real results |
| Behaviour drift vs. pre-refactor output | Parity smoke + resume test in Step 5 against a deterministic/local model |

## Suggested commit sequence

1. `chore: pin env, establish green test baseline` (Step 0 — lockfile/notes only, no logic)
2. `refactor(runtime): async _llm_hint_mapping and build_runtime_command` (Step 1)
3. `refactor(module): async do_research/do_planning/artifact loop/run` (Step 2)
4. `refactor(cli): drive ModuleAgent.run via asyncio.run` (Step 3)
5. `test(orchestrator): async tests + AsyncMock for agent.run` (Step 4)

Each commit keeps the non-orchestrator suites green; the orchestrator suite goes red at commit 3
and returns to green at commit 5 (call this out in the commit 3 body).
