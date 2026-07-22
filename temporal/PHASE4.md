# Phase 4 — Make Temporal the Source of Truth; Retire the Legacy Layer

**Status: complete, under Option B.** `status.json`/`--resume` are removed; the Django UI is
repointed to the Temporal client; `--legacy`/`ModuleAgent` remain as the tested, no-infra reference
implementation (Option B's deliberate tradeoff, see below). Every step's writeup below records what
was actually implemented and how it was verified — several are live-execution verifications, not
just unit tests.

PLAN.md's Phase 4 in one line: *remove `status.json`/`--resume`/`--legacy`, repoint the Django UI
from file-tailing to the Temporal client.* This document turns that into a staged, gated plan,
because a code audit shows the removals are not independent and one of them
(`--legacy`) forces an architectural decision the earlier phases deferred.

Prereqs: Phase 3 ([PHASE3.md](./PHASE3.md)) and the hardening in
[PHASE3.5.md](./PHASE3.5.md) are done. Phase 3.5.8 (the real Temporal-vs-`--legacy` parity run) is
this document's Step 4.2, and it has passed — see its writeup below.

## Current state (audited)

There are **two independent `status.json` writers**, with different consumers:

| Writer | Where | Consumers | Removable independently? |
|---|---|---|---|
| Workflow shadow | `temporal/workflow.py::_save_status` → `activities.save_status_snapshot` (~15 calls/run) | **None.** The Django UI reads the *legacy* file, not this one; the parity comparison it was kept for was never run. | **Yes — zero consumers.** Also removes the ~15 activity round-trips (PHASE3.5 L9). |
| Legacy in-process | `agents/module.py::ModuleAgent.save_status` (~20 calls) + `load_status` | `--resume` (CLI + Django) and the **Django UI** (reads it for per-artifact progress/completion) | No — coupled to `--resume` and the Django UI; needs the repoint first. |

Legacy-path surface to remove (all reachable only via `--legacy`/`--resume`):

- `generate-module.py`: the `--legacy` and `--resume` flags, the `if self.args.resume:` branch, the
  `if self.args.legacy:` branch.
- `agents/module.py::ModuleAgent`: `save_status`, `load_status`, the entire `if resume_status:`
  block in `run()`, and every `self.save_status(...)` call.
- `app/generator/views.py`: subprocess launch, `console.log` tailing, `status.json` reading
  (module listing at ~L86, `module_status` at ~L520-560), the resume module list (~L146), and the
  `--resume`/`--legacy` args it passes.
- `tests/test_module_orchestrator.py::TestStatusPersistence`: the `save_status`/`load_status`
  round-trip tests.

## The decision this phase forces: what happens to `ModuleAgent`?

`temporal/workflow.py::ModuleGenerationWorkflow` is a **port** of `ModuleAgent`, not a reuse — the
two hold ~600 lines each of the same coordination (prompt building, the artifact retry loop, the
cross-artifact escalation state machine, `extra_validation_args` construction). Removing `--legacy`
orphans `ModuleAgent` entirely. Two consequences make this non-trivial:

1. **Duplication is a live maintenance hazard.** Every future coordination fix (a new escalation
   rule, a prompt tweak) must be applied in both places or they drift.
2. **The workflow can't be unit-tested for the pipeline.** Phase 3 established that
   `TemporalAgent.override(model=...)` raises inside a workflow, so `ModuleGenerationWorkflow`
   cannot be driven end-to-end against `TestModel`. `tests/test_module_orchestrator.py` — which
   today gives real coverage of `do_research`/`do_planning`/`artifact_creation_loop`/
   `generate_all_artifacts` including the escalation and skip branches — exercises `ModuleAgent`.
   Delete `ModuleAgent` and that coverage has no equivalent.

### Options

- **Option A — Remove `--legacy`, delete `ModuleAgent` and its tests.** Simplest removal; matches
  PLAN.md literally. **Not recommended:** it deletes the only unit tests of the trickiest logic in
  the system (escalation/skip/retry) with no replacement, and removes the no-infra path.
- **Option B — Keep `--legacy`/`ModuleAgent` as a supported in-process mode; remove only the
  workflow shadow `status.json` and `--resume`.** Pragmatic; preserves the testable reference
  implementation and a no-Temporal path. **Cost:** the duplication persists.
- **Option C — Extract the shared coordination into a framework-agnostic module both drivers
  call.** The pure decision logic (next-artifact selection, `should_escalate`/queue reordering,
  prompt + `extra_validation_args` construction) becomes I/O-free functions / a command-emitting
  state machine; `ModuleAgent` and the workflow become thin drivers that dispatch the I/O (effects
  directly vs. activities). **Solves both problems** — one copy, unit-testable in one place — and
  then removing `--legacy` is safe. **Cost:** a substantial refactor, effectively its own project.

**Recommendation:** do **Option B for Phase 4** (keep `--legacy` as the tested, no-infra reference;
remove the shadow `status.json` and `--resume`), and schedule **Option C as a dedicated Phase 5**
("de-duplicate the orchestrator") rather than forcing a large refactor into Phase 4 under time
pressure. This deviates from PLAN.md's "remove `--legacy`" for a concrete reason: `--legacy` is
currently load-bearing for both testing and no-infra use, and removing it before Option C exists
trades real coverage for cleanup. The steps below are written for Option B, with the Option-A/C
delta called out where it matters.

## Step 4.1 — Remove the workflow shadow `status.json` (decision-independent, low risk)

Safe to do now regardless of the Option A/B/C choice — it has no consumers.

- [x] Delete `activities.save_status_snapshot` and its registration in `ALL_EFFECT_ACTIVITIES`.
- [x] Remove `ModuleGenerationWorkflow._save_status` and every `await self._save_status(status)`
      call in `temporal/workflow.py`. The workflow already returns the final status dict from
      `run()`, so the CLI loses nothing.
- [x] Add a `@workflow.query` progress handler (see 4.3) in the *same* commit if the Django repoint
      is following soon — it is the replacement source of truth for in-flight progress.
- [x] Verify: `uv run pytest -m "not live"` green; the `temporal`-marked workflow tests still pass
      (the MiniWorkflow/registration tests don't depend on the snapshot).

## Step 4.2 — GATE: run the real parity check (PHASE3.5 Step 3.5.8)

Nothing destructive below this line proceeds until this passes.

- [x] With a local/deterministic model (e.g. `ollama`) **and** a running worker, generate one module
      via the Temporal path and one via `--legacy` for identical inputs; diff the produced artifact
      set (wrapper/manifest/paramgroups/test.yml/README/Dockerfile). Record the result.
- [x] If they diverge, the divergence is a workflow port bug — fix it here before removing the
      `--legacy` reference that revealed it.

**Result (2026-07-21, `ollama:qwen3-coder-next`, Docker-hosted Temporal server, `--skip-dockerfile`):**
Both paths completed successfully end-to-end for `samtools` (5/5 artifacts generated and validated
on attempt 1, same file set on both sides modulo `status.json`, which `--legacy` still writes and
Temporal intentionally no longer does per 4.1). Manifest/wrapper/paramgroups/test.yml structure was
identical between paths (same deterministic templates, same field conventions); the only
differences were the LLM's free-choice content per run (which samtools subcommand/parameters it
picked), which is expected non-determinism, not a path divergence — both paths call the exact same
`agents.*`/`wrapper.agent`/`manifest.agent`/etc. objects.

Getting to a clean run surfaced several real, pre-existing bugs (none Temporal-specific — all in
shared `agents.*`/`wrapper.agent`/`planner.py`/`manifest.agent`/`paramgroups.agent` code exercised
identically by both paths), fixed along the way:
- Every agent defaulted to pydantic-ai's `retries=1`; too low for a local model. Now
  `MAX_ARTIFACT_LOOPS` everywhere.
- Tool params typed `List[Dict]`/`Dict[str, Any]` had no defense against a model that
  JSON-stringifies its own tool-call arguments, or truncates a large stringified payload mid-
  generation. Fixed via `agents.models.coerce_stringified_json`, backed by `json-repair`.
- Several tool params were `TYPE = None` instead of `TYPE | None = None` (a real, deterministic
  crash the model could never retry its way out of).
- `wrapper.agent.select_wrapper_language()` (already fixed pre-Phase-4) closed the tool-language-vs-
  wrapper-language gap for the wrapper's own scaffold, but `ModulePlan.wrapper_script`/`command_line`
  — read as context by *every* downstream artifact — were never corrected at the source. Added
  `planner.enforce_wrapper_and_flag_consistency` (`@planner_agent.output_validator`) to
  deterministically fix wrapper language/extension and force every parameter's flag to
  `--<param.name>` (the model was otherwise liable to copy the underlying tool's own native CLI
  flags, e.g. samtools' `-@`/`-q`, into the manifest).
- pydantic-ai defaults `UsageLimits.request_limit` to 50/run; the local model's chattier
  self-validation (re-checking its own plan/parameters) exceeded that on a normal, correct run. Now
  `MAX_AGENT_REQUESTS` (150), passed to every `agent.run()` call on both paths.
- `paramgroups.agent.create_paramgroups` only returns generation guidance (unlike wrapper/manifest,
  whose `create_*` tools return the finished content) and, unlike its sibling `gpunit_agent`, had no
  "call this exactly once" instruction — the model looped on it indefinitely. Added the same
  guardrail `gpunit_agent` already carried.
- `manifest.agent`'s inline-flag/`prefix_when_specified` dedup only stripped an inline flag that
  already spelled the parameter name correctly; a mismatched inline flag slipped through and
  duplicated the parameter's flag. Regex widened to strip any flag-like token before a placeholder.
- `manifest.models.ManifestModel.commandLine`: models sometimes HTML-escape the `<`/`>` around
  placeholders (`&lt;input.file&gt;`), which the linter didn't catch and which silently breaks
  GenePattern's runtime substitution. Added a `field_validator` to unescape on construction.

None of the above are workflow-port bugs (nothing in `temporal/workflow.py`'s coordination logic
itself needed a fix beyond the wrapper-filename-extension correction already applied pre-4.2) — they
were latent bugs in the shared pipeline that a hosted frontier model's better instruction-following
had been masking. **Gate passed.**

## Step 4.3 — Add in-flight progress to the workflow (enables the Django repoint)

The Django UI needs live progress; with the shadow file gone, the workflow must expose it.

- [x] Add a `@workflow.query def progress(self) -> dict` to `ModuleGenerationWorkflow` returning the
      current `status.to_dict()` (phase flags, per-artifact `generated`/`validated`/`attempts`,
      token usage, escalation log). Queries are read-only and replay-safe.
- [x] (Optional, for live text) maintain a bounded `list[str]` of recent log lines on the workflow
      (fed by `WorkflowLogger`) and expose it via the same or a second query, so the UI can show a
      streaming-style log without `console.log`.
- [x] Unit-test the query against `MiniWorkflow`-style fixtures or a small progress-bearing test
      workflow (marked `temporal`).

**Done.** `progress()` returns `{'status': ..., 'log': [...]}`; covered by
`tests/test_workflow_progress.py` (5 tests, no Temporal server needed).

## Step 4.4 — Repoint the Django UI to Temporal

This is the largest new work and it **re-introduces a hard dependency on a running Temporal
server + worker for the web UI** (the opposite of the PHASE3.5 stopgap). Confirm that operational
change is acceptable before starting.

- [x] **Launch:** replace the `subprocess.Popen(['python','generate-module.py',...])` in
      `app/generator/views.py` with `temporal.client.start_module_generation(...)`, storing the
      workflow ID (keyed per user/run) instead of a process handle.
- [x] **Progress:** replace `console.log` tailing + `status.json` reads in `module_status` (~L520)
      and the module listing (~L86) with a client call that fetches the workflow's `progress`
      query; map its dict to the JSON the frontend already expects.
- [x] **Completion:** replace the `console.log` "Process exited" marker with the workflow's
      terminal state (completed/failed) via `handle.describe()` / `handle.result()`.
- [x] **Module listing / history:** either keep scanning the on-disk output dirs (files still land
      there) or switch to `client.list_workflows(...)`; pick one and be consistent.
- [x] **Resume feature (decision):** `--resume` is going away. The Django "resume" affordance must
      either be dropped, or reframed onto Temporal semantics (retry a failed workflow / start a new
      run). Temporal's file-based "continue where it left off across separate invocations" has no
      direct equivalent — call this out to stakeholders as a feature change, not a like-for-like
      port.
- [x] **Worker for the UI:** document (and script) that the web UI now needs
      `uv run python -m temporal.worker` running; update `app/README.md` (undo the PHASE3.5
      `--legacy` note).

**Implementation:** `temporal/client.py` gained `get_workflow_state(workflow_id, client=None)`
(describe → dispatch to `progress()` query if RUNNING/non-terminal, or `handle.result()` if
COMPLETED; returns `None` for an unknown ID) and a public `connect()` for callers that want to
share one `Client` across several calls. Chose **`workflow_id = module_dir`** (deterministic,
set at launch via `start_module_generation(..., workflow_id=module_dir)`) over a separate ID↔dir
index file — any view reconstructs the right workflow handle straight from the directory name
already in the URL, with no mapping to keep in sync (and no new "second source of truth," which is
exactly what 4.1 removed). `app/generator/views.py` was rewritten around this: `generate_module`
submits and returns immediately (no thread, no subprocess, no log-file juggling);
`module_status`/`console_log` call `get_workflow_state` directly; `get_user_modules` (the sidebar
listing) fetches all directories' states concurrently via `asyncio.gather` under one
`async_to_sync` call. The `_map_state_to_status` helper reproduces the old status.json-era
success/error derivation logic (error_messages → artifacts all validated → research/planning
complete) so the frontend's `{status, running, data}` contract is unchanged. Removed the resume
dropdown and its JS (`populateDataFromResume`, `resumableModuleData`) from `dashboard.html`.

**Known limitation (accepted, not fixed here):** the console-log modal now shows the workflow's own
phase-level `print_status`/`print_section` calls (routed through `WorkflowLogger`, capped at 500
lines), not the fine-grained per-tool-call prints (`"🔧 RESEARCH TOOL: Running ..."` etc.) emitted
*inside* agent tool functions — those run inside the `TemporalAgent`-wrapped activity, whose stdout
only reaches the worker process's own console, not the workflow's queryable state. The old
subprocess-based UI captured everything because it tailed the whole process's stdout. This is a
real fidelity reduction for live-watching a run, not a correctness bug (the CLI's own terminal still
shows everything); piping activity-level output into the workflow's log buffer would need routing
tool functions through `temporalio.activity.logger` or similar, which is out of scope here.

**Also required:** `app/manage.py` didn't put the repo root on `sys.path` (Python puts the *script's
own* directory there when run as `python app/manage.py`, and `agents`/`temporal` live one level up)
— views.py's new direct imports of `agents.*`/`temporal.*` would have failed immediately. Fixed by
inserting `Path(__file__).resolve().parent.parent` at the front of `sys.path` in `manage.py`.

**Bug found and fixed during live testing:** `get_workflow_state` only queried `progress()` when
`execution_status == 'RUNNING'` (or a non-terminal failure state); once a workflow reached
`COMPLETED` it fetched `result` but never `progress` again, so `/console/<module>/` silently went
blank the moment a run finished — the exact moment a user is most likely to open the console to see
what happened. Queries remain answerable on closed workflows within the namespace's retention
window (Temporal replays history to compute them), so the fix was to always attempt the `progress()`
query regardless of execution status, in addition to fetching `result` when COMPLETED. Locked in
with an assertion in `tests/test_client_state.py::test_completed_workflow_reports_result`.

**Verified live end-to-end** (Docker-hosted Temporal server + `uv run python -m temporal.worker`,
real `ollama:qwen3-coder-next`): logged into the running dev server via curl and drove the full
flow through the real HTTP endpoints — submitted a generation through `/generate/`; polled
`/status/` and `/console/` while `running: true`, confirming live per-phase log lines with real
research/planning data flowing through; let it run to completion (wrapper-only scope, via a
directly-submitted workflow sharing the same `workflow_id = module_dir` convention, to keep the
test fast — the form itself doesn't expose artifact scoping); confirmed the workflow reached
`COMPLETED` with `status: "success"` after the wrapper validated on a retry, `/console/` showed the
full log tail post-completion (the bug above, now fixed), `/files/` listed the generated artifacts,
`/download/` served the wrapper script's actual bytes, and the dashboard listing correctly showed
`data-status="success"` for the finished module — all sourced live from Temporal, nothing cached or
duplicated on disk beyond the artifacts themselves.

## Step 4.5 — Remove `--resume` and the shadow-status plumbing (gated on 4.2)

- [x] Remove the `--resume` CLI flag and the `if self.args.resume:` branch in `generate-module.py`.
- [x] Under **Option B**: remove `ModuleAgent.load_status` and the `if resume_status:` block in
      `ModuleAgent.run()` (resume is gone), but **keep** `save_status`? No — with `--resume` gone
      and the Django UI repointed, the legacy `status.json` has no reader either. Remove
      `ModuleAgent.save_status`/`load_status` and all `self.save_status(...)` calls too; `--legacy`
      runs then simply don't write `status.json`. `ModuleAgent.run` keeps working (it returns an
      exit code and prints the final report).
- [x] Delete `tests/test_module_orchestrator.py::TestStatusPersistence`; keep the rest of that file
      (it still covers the `ModuleAgent` coordination under Option B).

**Done.** One deviation from the literal instruction: `TestStatusPersistence` had 8 tests, but the
last 3 (`test_model_dump_round_trip`, `test_status_is_pydantic_base_model`, `test_to_dict_still_works`)
exercise `ModuleGenerationStatus.to_dict()`/its `BaseModel` nature directly, not `save_status`/
`load_status` — and `to_dict()` remains live (it's what the workflow's `progress()` query and the
Temporal-path CLI report serialize). Deleting those would have been a real coverage loss for no
reason, so they were kept and moved to a renamed `TestModuleGenerationStatusSerialization`; only the
5 genuinely `save_status`/`load_status`-specific tests were deleted.

Verified: `uv run pytest -m "not live"` green (167 passed, down from 172 by exactly the 5 deleted
tests); a live `--legacy` run (`--artifacts wrapper`, so the fast path) completed successfully with
no `status.json` written to the module directory.

While testing this, live execution surfaced one more real bug (same class as the `paramgroups`
"call this tool once" fix in 4.2's writeup): `agents/researcher.py`'s `create_tool_research_report`
had no "call this exactly once" instruction, unlike its sibling `gpunit_agent`, and the local model
looped on it. Added the same guardrail. (This fix is probabilistic, not a hard guarantee — the same
local model was observed looping on it again in a *later*, unrelated live run during 4.4 testing;
`MAX_AGENT_REQUESTS` bounds the damage but doesn't eliminate the possibility. Worth revisiting with
a deterministic "already called" check if it keeps recurring.)

## Step 4.6 — (Option A or C only) remove `--legacy` and retire `ModuleAgent`

Skip entirely under Option B.

- [ ] **Option A:** remove the `--legacy` flag and branch; delete `agents/module.py::ModuleAgent`
      and `tests/test_module_orchestrator.py`; delete `agents/validator.py` only if nothing else
      imports it (the workflow uses `run_linter`/`build_and_test_image` directly). Accept the
      coverage loss (not recommended).
- [ ] **Option C:** first land the shared-coordination extraction (its own effort), point both the
      workflow and a slimmed `ModuleAgent` (or the CLI directly) at it, move the orchestration unit
      tests onto the shared module, *then* remove `--legacy`.

## Step 4.7 — Docs & config

- [x] README: drop `--legacy` from the "no infrastructure" framing **iff** Option A/C removed it;
      under Option B, keep it but note the web UI now requires a worker (4.4).
- [x] CLAUDE.md: update the Temporal-layer note — no more shadow `status.json`; `--resume` gone;
      state whether `--legacy`/`ModuleAgent` remain (Option B) or not (A/C).
- [x] Remove `TEMPORAL_*` timeout stragglers only if unused; keep `.env.example` accurate.

**Done.** README: removed the `status.json`/`--resume` directory-listing entry and the `--resume`
flag table row (replaced with a note that there's no resume, plus a `--module-dir`-based example
for the old "regenerate just the Dockerfile" use case); the web UI section now says it requires a
running server+worker with no in-process fallback. `app/README.md` rewritten similarly (see 4.4).
CLAUDE.md: updated the pipeline description, the `agents/status.py`/`app/` bullets, and the "Phase 4
status" note to say Option B is done rather than pending. All `TEMPORAL_*` env vars
(`TEMPORAL_ADDRESS`, `TEMPORAL_DOCKER_BUILD_TIMEOUT_SEC`, `TEMPORAL_DOWNLOAD_TIMEOUT_SEC`,
`TEMPORAL_EXECUTION_TIMEOUT_SEC`, `TEMPORAL_DOC_LINTER_TIMEOUT_SEC`) are still read by
`temporal/workflow.py`/`temporal/client.py`, so nothing to remove; `.env.example` already lists
`MAX_AGENT_REQUESTS` (added during the 4.2 parity-gate debugging) alongside the existing
`TEMPORAL_ADDRESS` section.

## Test implications

- Removing the shadow snapshot: the `temporal` workflow tests are unaffected (they don't assert on
  it); add the `progress` query test (4.3).
- Removing `TestStatusPersistence`: expected; those tests validated a feature being deleted.
- **Option B keeps `test_module_orchestrator.py`'s coordination coverage intact** — the main reason
  to prefer it. Under A/C that coverage must be relocated (C) or is lost (A).
- Keep the full-suite gate green after every step: `uv run pytest -m "not live"`, plus
  `uv run pytest -m temporal` where a test server is available.

## Acceptance criteria — all met

1. ✅ The workflow no longer writes a shadow `status.json`; no `save_status_snapshot` remains; the
   ~15 per-run status activities are gone.
2. ✅ A `@workflow.query`-based progress source exists and is unit-tested
   (`tests/test_workflow_progress.py`).
3. ✅ The Django UI starts runs via the Temporal client and shows progress from the workflow query
   (no `console.log`/`status.json` tailing); `app/README.md` documents the worker requirement and
   the resume-feature change. Verified against a real server+worker, not just unit tests.
4. ✅ `--resume` is removed; the parity check (4.2) was executed and recorded before any legacy
   removal.
5. ✅ The `ModuleAgent` decision is Option B, explicitly, and the code matches it: `--legacy`/
   `ModuleAgent` remain as the tested, no-infra reference implementation; `save_status`/
   `load_status`/resume are gone from it; its coordination test coverage
   (`tests/test_module_orchestrator.py`) is intact. De-duplicating it against the workflow (Option
   C) is deferred to a Phase 5, per the original recommendation.
6. ✅ `uv run pytest -m "not live"` stays green throughout (170 passed, 1 skipped, 3 deselected as
   of the final state); `uv run pytest -m temporal` also green (8 passed).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Removing `--legacy`/`ModuleAgent` deletes the only pipeline unit tests | Prefer Option B, or gate `--legacy` removal on Option C landing first |
| Django repoint re-imposes a worker requirement on the web UI operator | Explicit decision + docs; keep `--legacy`-based UI as a fallback config if needed |
| Destructive removals run before the Temporal path is proven | Hard gate at 4.2 (parity run); nothing below it proceeds until it passes |
| Removing `status.json` before repointing Django breaks the UI's progress | Sequence strictly: 4.3 (query) + 4.4 (repoint) before 4.5; never remove the legacy file while a reader exists |
| `--resume` removal is a silent feature loss for UI users | Called out as an explicit stakeholder decision in 4.4, not a like-for-like port |

## Suggested commit sequence

1. `refactor(temporal): remove shadow status.json snapshot activity` (4.1)
2. `feat(temporal): workflow progress query` (4.3)
3. `test: record Temporal-vs-legacy parity result` (4.2 — gate)
4. `feat(app): launch + poll module generation via Temporal client` (4.4)
5. `feat(cli): remove --resume; drop ModuleAgent status persistence` (4.5)
6. `docs: Temporal-only run model; UI worker requirement; resume change` (4.7)
7. *(Option A/C only)* `refactor: extract shared coordination / remove --legacy` (4.6)

Phase 5 candidate (recommended): **de-duplicate the orchestrator** (Option C) and the distributed
shared-storage work for the module output directory (PLAN.md Phase 5 / CONSIDERATIONS.md gotcha #2).
