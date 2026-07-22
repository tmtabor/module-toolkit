# Phase 2 — Extract Side Effects into Activity-Ready Functions

Isolate every non-deterministic side effect behind a **standalone, serializable-in/out,
`Logger`-free, `self`-free** function, **without introducing Temporal**. After this phase, Phase 3
becomes a wrapping exercise (`@activity.defn` on each function) instead of a rewrite, and the
deterministic coordination in `ModuleAgent` is ready to become a Temporal workflow.

Prereq: [PHASE1.md](./PHASE1.md) is done (the orchestrator is `async`). See
[CONSIDERATIONS.md](./CONSIDERATIONS.md) gotcha #2 (payload limits / shared disk) and #3 (limited
`RunContext`) for why the boundary rules below matter.

## Design rules for an "activity-ready" function

A function is activity-ready when it obeys all of these (they are exactly what Temporal will
require in Phase 3):

1. **Module-level, not a method.** No `self`. Lives in a dedicated module so Phase 3 can decorate
   it. Register-once, call-anywhere.
2. **Serializable inputs and outputs.** Only `str`/`int`/`bool`/`list`/`dict`, Pydantic models, or
   things with `to_dict`/`from_dict` (`ExampleDataItem`). **Paths cross as `str`, never `Path`.**
   **Files cross by path, never by content** (gotcha #2: research text, wrapper source, and data
   files blow the ~2 MB activity payload limit).
3. **No `Logger` argument.** `Logger` will not serialize across an activity boundary. A function
   returns a result model that *carries* any log lines (`log: list[str]`); the async orchestrator
   emits them via its `Logger`. (Alternative: a module-level logger inside the function — but
   returning log lines keeps the function pure and testable, so prefer that.)
4. **No hidden non-determinism.** No `datetime.now()`, `Path.iterdir()` ordering assumptions, or
   randomness *inside coordination*. Timestamps are injected as arguments; directory scans are
   themselves side-effect functions that return a sorted, deterministic result.
5. **Idempotent where feasible.** Temporal may retry an activity; writing a file or building an
   image should tolerate re-execution.

The functions stay **synchronous** in Phase 2 (blocking I/O called from the async orchestrator is
fine — Phase 1 already established this). Phase 3 decides sync-activity-in-threadpool vs. async.

## Target layout

- `agents/effects.py` — all extracted side-effect functions, grouped by domain (filesystem, HTTP,
  docker/subprocess, archive). Plain, module-level, sync.
- `agents/effects_models.py` — Pydantic result models (`DownloadResult`, `ValidationResult`,
  `BuildResult`, `UploadResult`, `ZipResult`, `PushResult`, `FileReadResult`). Each carries a
  `log: list[str] = []` and an `ok`/`success` flag.
- `agents/module.py` — `ModuleAgent` methods become **thin coordinators** that call the extracted
  functions and do the (deterministic) sequencing, retry counting, and escalation. This is the
  code that becomes the Phase 3 workflow.
- Docker stays wired through `dockerfile/` (the linter already owns `docker build`/`docker run`);
  the effects function wraps that path rather than moving it.

## Side-effect inventory (the work)

Every non-deterministic operation in the pipeline, its current location, and its extracted target.
Line numbers are anchors as of this writing.

| # | Side effect | Current location | Extract to `effects.py` | Notes / serializable I/O |
|---|---|---|---|---|
| 1 | URL data download | `module.py:148` `download_url_data` (requests.get, open, unlink) | `download_one(url, dest_dir, filename) -> DownloadResult(local_path, size, ok, error, log)` | Orchestrator keeps the filename-collision loop (deterministic) and calls this per URL item. |
| 2 | Linter validation | `agents/validator.py` `validate_artifact` (in-proc linter, stdout capture) | `run_linter(validate_tool, file_path, extra_args) -> ValidationResult(success, output, log)` | Drop `Logger`. Mostly pure already. |
| 3 | **Docker build + runtime test** | `dockerfile/linter.py:72` (subprocess `docker build`/`run`), reached via `run_linter('validate_dockerfile', …)` | `build_and_test_image(dockerfile_path, tag, run_cmd, volumes, cleanup) -> BuildResult(success, output, log)` | **The heavy, long-running one** (Phase 3: generous timeout + heartbeat, Docker-capable worker). NOTE: the build lives in the *linter*, not `runtime.py` — PLAN.md's wording was imprecise. |
| 4 | Runtime-command assembly | `dockerfile/runtime.py` `build_runtime_command` (reads manifest + test.yml) | Split: file reads → effects (#10); the manifest-parse + placeholder substitution stay **deterministic** coordination; the LLM hint-mapping is already an agent call (Phase 1 → auto-activity in Phase 3). | Pass manifest text + test.yml params *in* as data, not paths, so the deterministic part has no I/O. |
| 5 | GenePattern upload | `module.py:719` `upload_to_genepattern` (requests.post, open) | `upload_module(zip_path, gp_server, gp_user, gp_password) -> UploadResult(success, message, log)` | Secrets as args now; Phase 3 may move to Temporal secret handling. |
| 6 | Zip creation | `module.py:1185` `zip_artifacts` (zipfile, iterdir, unlink) | `zip_artifacts(module_dir, zip_name, member_filenames, zip_only) -> ZipResult(zip_path, size, ok, log)` | Pass explicit member filename list (orchestrator computes it from the plan) instead of a `ModulePlan`. |
| 7 | Docker push | `module.py:1146` `docker_push` (subprocess.Popen) | `docker_push(tag) -> PushResult(success, log)` | |
| 8 | Artifact + report file writes | `module.py:564,574` (artifact loop); research.md `1438`; plan.md `1457`; plan.jsonl `400` | `write_text_file(path, content) -> None` (or `persist_artifact(module_dir, filename, content, report_name, report_content)`) | Content crosses as `str` (already generated by the agent, bounded). |
| 9 | Dir create + **timestamp** | `module.py:126` `create_module_directory` (datetime.now, mkdir) | `make_module_dir(output_dir, tool_name, timestamp, module_dir="") -> str` | **Inject `timestamp`** (Phase 3 sources it from `workflow.now()`). mkdir is the side effect. |
| 10 | Filesystem reads/scans | `_sync_wrapper_script` iterdir `838`; `_get_manifest_docker_image` `881`; wrapper-src read `526`; test.yml read `622`; escalation AST read `1043`; manifest read `runtime.py:138` | `read_text_file(path) -> FileReadResult`; `find_wrapper_file(module_dir) -> str\|None` (sorted, deterministic); `read_manifest_docker_image(module_dir) -> str\|None` | These feed *deterministic* decisions but do disk I/O, so they must be activities. Return sorted results (rule 4). |
| 11 | Data-dir cleanup | `module.py:200` `cleanup_data_dir` (shutil.rmtree) | `remove_dir(path) -> None` | Idempotent (no-op if missing). |
| 12 | Status persistence | `module.py:214,223` `save_status`/`load_status` (json I/O) | **Do not extract.** | Temporal *replaces* this in Phase 4; extracting now is wasted work. Leave as methods; they stay until Phase 4. |

## Step-by-step

### Step 2.0 — Scaffolding
- [ ] Create `agents/effects_models.py` with the result models (each: a success/ok flag + `log:
      list[str] = Field(default_factory=list)` + domain fields). Reuse `ArtifactResult`-style
      Pydantic conventions already in `agents/status.py`.
- [ ] Create empty `agents/effects.py` with domain section headers.

### Step 2.1 — Filesystem effects (lowest risk first)
- [ ] Extract #8 (`write_text_file`), #9 (`make_module_dir` with injected timestamp), #10 (reads +
      `find_wrapper_file` + `read_manifest_docker_image`), #11 (`remove_dir`).
- [ ] Repoint `ModuleAgent.create_module_directory`, `_sync_wrapper_script`,
      `_get_manifest_docker_image`, `cleanup_data_dir`, and the inline `open(...,'w')` writes to
      the new functions. `create_module_directory` now takes/needs a timestamp — pass
      `datetime.now().strftime(...)` from the (still-plain) orchestrator for now; the injection
      point is what matters.
- [ ] Unit-test each with `tmp_path`.

### Step 2.2 — HTTP effects
- [ ] Extract #1 (`download_one`) and #5 (`upload_module`). Keep the collision-resolution loop in
      `download_url_data` (it becomes deterministic coordination that calls `download_one`).
- [ ] Unit-test `download_one` with a mocked `requests`; `upload_module` with a mocked `requests`.

### Step 2.3 — Archive + subprocess effects
- [ ] Extract #6 (`zip_artifacts`), #7 (`docker_push`). Orchestrator computes the member filename
      list from the plan and passes it in.
- [ ] Unit-test `zip_artifacts` against a temp dir; `docker_push` with a patched `subprocess`.

### Step 2.4 — Validation + Docker build (the heavy one)
- [ ] Extract #2 (`run_linter`, from `agents/validator.py`) and #3 (`build_and_test_image`). Drop
      the `Logger` param; return `ValidationResult`/`BuildResult` carrying captured output as
      `log`. `ModuleAgent.validate_artifact` becomes a thin wrapper over `run_linter`.
- [ ] Confirm the dockerfile linter's `-t/-c/-v` argument construction (currently assembled in
      `artifact_creation_loop`) is passed as plain data into `build_and_test_image`.

### Step 2.5 — Decouple the runtime-command builder (#4)
- [ ] In `dockerfile/runtime.py`, move the manifest/`test.yml` **reads** to effects (#10) and pass
      their *parsed content* into the pure `build_runtime_command` logic, so that everything except
      the (already-extracted) LLM hint-mapping agent call is deterministic and I/O-free — ready to
      run inside the Phase 3 workflow.

### Step 2.6 — Logger decoupling sweep
- [ ] Audit every extracted function for a remaining `Logger` reference; replace with
      `result.log.append(...)`. In `ModuleAgent`, after each call, drain `result.log` through
      `self.logger.print_status(...)` so console output is unchanged.

### Step 2.7 — Determinism prep
- [ ] Grep the coordination path (`module.py`) for `datetime.now()`, `time.time()`,
      `Path.iterdir()`, `os.listdir()`, `random`. Each must either be injected (timestamp) or be an
      effects function returning a sorted result. Record the list as the Phase 3 "workflow
      determinism" checklist.

## Testing strategy (suite must stay green throughout)

- [ ] **New unit tests** for every extracted function in a new `tests/test_effects.py`
      (filesystem via `tmp_path`; HTTP/subprocess via `monkeypatch`/`patch`). These are pure and
      fast — no LLM, no live marker.
- [ ] **Update `tests/test_module_orchestrator.py`** seams: tests that patched
      `agent.validate_artifact` still work (it becomes a wrapper), but prefer patching the new
      `agents.effects.run_linter` / `build_and_test_image` seam where clearer. The
      `artifact_creation_loop` and `generate_all_artifacts` tests should need only seam-name
      changes, not logic changes — a signal the extraction preserved behavior.
- [ ] **Parity**: re-run the Phase 1 async-entry smoke
      (`scratchpad/smoke_async_entry.py`) — still rc=1, no `RuntimeWarning`.
- [ ] **Golden run** (if a local/deterministic model is available): one `--skip-dockerfile`
      generation before and after Phase 2 produces byte-identical artifacts.
- [ ] Green gate after every step:

      ```bash
      uv run pytest -m "not live"
      uv run python -m manifest.linter manifest/examples/valid/manifest   # linter still runs
      ```

## Acceptance criteria

1. Every side effect in the inventory (except #12) is a module-level function in `agents/effects.py`
   with serializable I/O and **no `Logger`/`Path`/`self` parameter**
   (`grep -nE "Logger|: Path" agents/effects.py` returns nothing).
2. `ModuleAgent`'s side-effect methods are thin wrappers over `effects.py`; the orchestration
   methods (`artifact_creation_loop`, `generate_all_artifacts`, `run`) contain **only coordination
   + calls to effects functions** — no direct `open`/`requests`/`subprocess`/`zipfile`.
3. `datetime.now()` is gone from the coordination path (injected instead); a determinism checklist
   for Phase 3 is recorded.
4. `uv run pytest -m "not live"` is green; new `tests/test_effects.py` covers each function; the
   async-entry smoke still passes with `RuntimeWarning` as error.
5. No Temporal import added; `status.json`/`--resume` untouched (removed in Phase 4).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Behavior drift while moving code | Extract mechanically (cut/paste body, swap `self.logger.x` → `result.log.append`); golden-run + unchanged orchestrator-test *logic* as the parity signal |
| `Logger`-in-activity slips through to Phase 3 | Step 2.6 sweep + acceptance grep #1 |
| Hidden non-determinism (`datetime`, `iterdir`) reaches the Phase 3 workflow | Step 2.7 checklist; directory scans return **sorted** results |
| Passing file **contents** across a boundary (payload bloat) | Rule 2 enforced in review; inventory #4/#8 pass paths or already-bounded generated strings only |
| Over-extraction of `status.json` wastes effort | Explicitly excluded (#12); removed in Phase 4 |
| Docker build function hard to unit-test | Unit-test argument assembly + result parsing with a patched `subprocess`; leave real `docker build` to the existing example-based/manual check |

## Suggested commit sequence

1. `feat(effects): result models + filesystem effects` (2.0–2.1)
2. `feat(effects): http download/upload` (2.2)
3. `feat(effects): zip + docker push` (2.3)
4. `refactor(effects): run_linter + build_and_test_image; validator thin-wraps` (2.4)
5. `refactor(runtime): I/O-free build_runtime_command` (2.5)
6. `refactor(module): drain effect logs; inject timestamp; determinism checklist` (2.6–2.7)
7. `test(effects): unit tests + orchestrator seam updates` (tests alongside each above)

After Phase 2, `ModuleAgent.run` and friends are pure coordination over serializable effects —
Phase 3 wraps the agents in `TemporalAgent`, decorates `agents/effects.py` with `@activity.defn`,
and lifts the coordination into a `@workflow.defn`.

---

## Determinism checklist (recorded during implementation — for the Phase 3 workflow)

Sources of non-determinism / residual I/O that remain in `agents/module.py`, and what Phase 3
must do with each:

| Item | Location | Phase 3 action |
|---|---|---|
| `datetime.now()` — module-dir timestamp | `create_module_directory` (module.py, the one remaining `datetime.now`) | Source from `workflow.now()`; it is already **injected** into `effects.make_module_dir`, so only the call site changes. |
| `open()` for `status.json` | `save_status` / `load_status` | **Deleted in Phase 4** — Temporal owns durability. Do not lift into the workflow. |
| `Path.iterdir()` for the final file listing | `print_final_report` | Reporting only; runs **client-side** after the workflow completes, so it stays as-is (not workflow code). |
| All other file/HTTP/subprocess/docker I/O | routed through `agents/effects.py` | Decorate each `effects` function with `@activity.defn`; the heavy one is `build_and_test_image` (own timeout/heartbeat, Docker-capable worker). |

Confirmed **absent** from the coordination path: `time.time()`, `random.*`, and any direct
`requests`/`subprocess`/`zipfile`/`shutil` use (those imports were removed from `module.py`).
Directory scans that feed decisions (`find_wrapper_file`, `zip_artifacts`) live inside effects and
return **sorted/deterministic** results.

## Implementation notes (what actually landed)

- New modules: `agents/effects.py` (12 functions) and `agents/effects_models.py` (result models,
  each carrying `log: list[str]`). `agents/validator.py` is now a thin shim over
  `effects.run_linter` / `build_and_test_image`.
- `ModuleAgent._emit(result)` drains each effect's `log` through the agent's `Logger`, preserving
  console output (log levels collapse to INFO — a cosmetic change).
- Bug fixed in passing: the old `upload_to_genepattern` had `except Exception(e)` referencing an
  undefined `log`; `effects.upload_module` handles a non-JSON response cleanly.
- Tests: `tests/test_effects.py` (24 unit tests, no network/docker) added; full non-live suite
  **156 passed, 1 skipped**; the Phase 1 async-entry smoke still passes with `RuntimeWarning` as
  error.
- Deferred to Phase 3 (deliberately not done here): restructuring `build_runtime_command` to take
  manifest/`test.yml` **text** instead of paths. For now those reads are routed through
  `effects.read_text_file` (the activity seam), which is sufficient for Phase 2's goal.
