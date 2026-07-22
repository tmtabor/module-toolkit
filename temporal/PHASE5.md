# Phase 5 — Production Hardening & Orchestrator De-duplication

Phase 4 ([PHASE4.md](./PHASE4.md)) made Temporal the source of truth for run state: no more
`status.json`, no `--resume`, the Django UI submits and polls via the Temporal client. This
document is PLAN.md's "Phase 5 — Production hardening (ongoing)" turned into a concrete, staged
plan, updated for what actually shipped in Phases 1-4 (which deviated from PLAN.md in specific,
documented ways — Option B kept `--legacy`/`ModuleAgent` rather than retiring them, for instance).

## How this phase differs from Phases 3/4

Phases 3 and 4 were each a single staged plan with one hard gate (3's shadow-mode proof, 4's
parity run) — nothing after the gate proceeded until it passed. **Phase 5 is not that.** The
workstreams below are largely independent; several were literally listed side-by-side in PLAN.md's
original Phase 5 sketch with no ordering implied. Treat this as a menu with a recommended default
order (see "Recommended sequencing"), not a strict pipeline — pick based on priority, and each
workstream has its own verification bar rather than a single end-of-phase gate.

## Current state (audited)

Grounding facts, so priorities below aren't guesses:

- **Payload/history size is a live, already-observed problem, not a hypothetical one.**
  CONSIDERATIONS.md gotcha #2 flagged this at the design stage; Phase 4's live parity-gate testing
  hit it for real — `PayloadSizeWarning`s at 527KB-1.3MB were logged repeatedly (research reports
  and full serialized `ModulePlan`s riding along in workflow/activity payloads and the `progress()`
  query result), and one long-lived, repeatedly-resumed workflow was outright `TERMINATED` by the
  server for exceeding its history size limit during that debugging. This isn't a future risk to
  plan around — it already broke a run.
- **No activity uses a custom `RetryPolicy`.** Verified via `grep -rn "RetryPolicy\|retry_policy" temporal/`
  — zero hits outside imports. Every activity retries on Temporal's default policy (unbounded,
  backoff, up to `execution_timeout`), which is wrong for deterministic failures (they retry
  identically, forever, bloating history — see the point above) and right for transient ones
  (LLM rate limits) with no way to currently tell them apart.
- **No activity heartbeats.** `temporal/workflow.py`'s `_act` helper (all activity calls funnel
  through it) never sets `heartbeat_timeout`; the docstring at L139 explains this is a deliberate
  stopgap from PHASE3.5's H0 fix (generous fixed timeouts instead of real heartbeating), and
  PHASE3.5's own risk register listed "add real heartbeating later" as still open.
- **`agents/module.py` (`ModuleAgent`, 1222 lines) and `temporal/workflow.py`
  (`ModuleGenerationWorkflow`, 910 lines) have a near-1:1 method correspondence**: `do_research`/
  `_do_research`, `do_planning`/`_do_planning`, `artifact_creation_loop`/`_artifact_creation_loop`,
  `generate_all_artifacts`/`_generate_all_artifacts`, `_sync_wrapper_script` (identical name in
  both), `zip_artifacts`/`_zip_artifacts`, `_run_install_artifact` (identical name in both), `run`/
  `run`. This was flagged as a maintenance hazard when Option B was chosen in PHASE4.md, and it
  already bit once: the wrapper-filename-extension bug found during Phase 4's parity-gate debugging
  had to be fixed by hand in *both* files in the same session.
- **No human-in-the-loop gate exists.** `_run_install_artifact` uploads to GenePattern
  automatically whenever `gp_server`/credentials are supplied — no `@workflow.signal` anywhere in
  `temporal/workflow.py` (verified via grep).
- **No concurrency has been validated.** Every run in this project's history so far — including
  all of Phase 4's live testing — has been one workflow at a time against one worker. Whether two
  simultaneous runs interfere (shared-filesystem module directories, process-wide state like
  `agents/researcher.py`'s module-level `_brave_lock`/`_brave_last_call` Brave rate-limiter) is
  untested.
- **The shared-filesystem assumption is documented but unresolved.** README's "Current limitation"
  note (added in Phase 3.5 in response to risk M7) says the worker and CLI/UI must share a
  filesystem. That's a description of the current constraint, not a fix.
- **The "agent loops on a tool call instead of finishing" bug class recurred three times** across
  Phase 4's live debugging (`gpunit_agent`'s `create_gpunit`, then `paramgroups_agent`'s
  `create_paramgroups`, then `researcher_agent`'s `create_tool_research_report`), each fixed with a
  prompt-level "call this exactly once" instruction. PHASE4.md's own writeup notes this fix is
  probabilistic, not a guarantee — the same local model was observed looping again on the
  *already-fixed* researcher tool in a later, unrelated run. There is no deterministic backstop.
- **Console-log fidelity is a known, accepted gap** (PHASE4.md 4.4): the web UI's `/console/` shows
  the workflow's own phase-level status lines, not the fine-grained per-tool-call prints emitted
  inside agent tool functions (those run inside the activity and their stdout never reaches the
  workflow's queryable state). Documented as an accepted tradeoff, not fixed.

## Workstream A — Shared module storage & payload size (do this first) — done

**Why first:** it's the only workstream here that has already caused a real failure (the
`TERMINATED` workflow), and Workstream E (concurrency) is unsafe to attempt before it's resolved.
Workstream B (de-duplication) will touch the same status/payload plumbing this workstream changes,
so landing A first means B only has to adapt to the final shape once.

**A1 — Stop re-embedding large text in every payload.** `ModuleGenerationStatus.to_dict()`
(`agents/status.py`) includes the full `research_data['research']` markdown (10-15KB typical, seen
up to tens of KB) and the complete serialized `planning_data` (every parameter, every field) in
full every time it's returned — from `progress()` queries (polled every 10s by the Django UI) and
from every activity/workflow payload that threads `status` through. The content is *also* already
written to disk (`research.md`, `plan.md`) by the same run. Change `to_dict()` (or add a
`to_dict(include_full_text=False)` mode used by the hot paths) to carry a reference
(`module_directory` + filename, already present) plus perhaps a bounded excerpt, not the full text;
expose a separate, on-demand way to fetch the full text (the Django UI already has `/files/` +
`/download/` for exactly this — wire the frontend to use those instead of expecting full research
text in the status blob it polls every 10 seconds).

**A2 — Audit other unbounded-growth fields.** `error_history: List[str]` / accumulated
`downstream_error_context` strings threaded through `ArtifactDeps` grow across retries within a
single artifact's loop; `artifact_creation_loop`/`_artifact_creation_loop` already has a
`_truncate_error_report(raw, max_tail=50)` helper — confirm it's applied at every accumulation
point (it was added ad hoc; verify it's not just at one call site) and that `escalation_log`
(unbounded `List[Dict[str,str]]`, appended to across a whole run) has some cap too.

**A3 — Resolve the shared-filesystem question definitively.** Options, cheapest first:
  - **(a) Document + enforce co-located deployment.** Add a startup check (worker and CLI/Django
    both assert they can see the same `GENERATED_MODULES_DIR`/marker file) that fails loudly
    instead of silently succeeding with output nobody can see. Cheapest, matches today's actual
    single-host deployments (Docker-hosted Temporal server + local worker, per this session's
    testing setup).
  - **(b) Shared network volume** (NFS/EFS/a FUSE-mounted bucket) — no code changes beyond
    deployment config, if (a)'s assumption needs to extend to genuinely separate hosts.
  - **(c) True object storage** (S3/GCS) with activities uploading and the CLI/Django downloading
    on demand — the real fix for a distributed worker fleet, and a much bigger lift (`agents/effects.py`'s
    file I/O would need an abstraction over "local disk" vs "object store").

  **Recommendation: ship (a) now.** (b)/(c) are YAGNI until there's an actual need for workers on
  separate hosts from the CLI/UI — don't build distributed storage for a single-process deployment.
  Revisit if/when Workstream E's concurrency testing reveals a real multi-host need.

**Implementation:** A1 — `ModuleGenerationStatus.to_dict()` (`agents/status.py`) now excludes
`research_data['research']` and `planning_data['plan']` (the two large free-text fields;
confirmed via `grep` that `research_data` only ever holds that one key) while keeping every other
structured field (`research_complete`/`planning_complete` booleans, all `ModulePlan` fields except
`plan`, etc.) — nothing downstream (`app/generator/views.py::_map_state_to_status`,
`generate-module.py::print_report_from_status_dict`) ever read the dropped fields, confirmed by
grep before removing them. A2 — the dockerfile-only special case in `_truncate_error_report` was
removed from both `agents/module.py::artifact_creation_loop` and
`temporal/workflow.py::_artifact_creation_loop`; every artifact's error history is now truncated
the same way (`ERROR_INDICATORS` already matches generic linter `ERROR:`/`error:` lines, not just
docker-build output, confirmed live). `escalation_log` was audited and found already effectively
bounded (`MAX_ESCALATIONS` per artifact pair × a small fixed set of artifacts, each entry's
`error_snippet` already truncated to 500 chars) — no change needed. A3 — added
`GenerationScript._check_shared_filesystem()` (`generate-module.py`) and `_filesystem_warning()`
(`app/generator/views.py`, checked on every `/status/` poll, surfaced in the dashboard's status
text) — both check whether the workflow's reported `module_directory` exists on the *caller's*
filesystem and warn explicitly if not, rather than silently claiming success.

**Verified:** `tests/test_module_orchestrator.py::test_to_dict_excludes_large_free_text` locks in
A1. `_check_shared_filesystem`/`_filesystem_warning` were exercised directly (missing path → warns;
existing path / `None` → silent). A live full-pipeline Temporal run (real `ollama:qwen3-coder-next`,
Docker-hosted Temporal server + worker) completed successfully with **zero `PayloadSizeWarning`s**
and every artifact validated on attempt 1 — see Workstream C's writeup below for the same run's
full detail, since A and C were verified together in one pass.

## Workstream B — De-duplicate the orchestrator (PHASE4.md's deferred Option C)

**Scope warning:** this is the largest single effort in this phase — treat it like Phase 0 treated
the Temporal decision itself: spike the pure/impure split first and confirm it's as clean as it
looks before committing to the full refactor.

**Approach**, per CONSIDERATIONS.md's original "Recommended architecture" table and PHASE4.md's
Option C sketch:

1. Extract the **pure, I/O-free decision logic** shared by `ModuleAgent`/`ModuleGenerationWorkflow`
   into a new module (e.g. `agents/orchestration.py`):
   - Next-artifact selection / skip-if-already-validated logic.
   - `classify_error`/`should_escalate` escalation-queue reordering — already pure
     (`agents/error_classifier.py`), just needs a single call site instead of two.
   - Prompt string construction + `ArtifactDeps` assembly per artifact.
   - Retry/attempt counting and `ModuleGenerationStatus` mutation.
   - The wrapper-filename/extension-correction logic — currently duplicated near-verbatim between
     `agents/module.py` and `temporal/workflow.py` (the exact bug class that already drifted once
     during Phase 4).
2. Express I/O as an interface both drivers implement — `ModuleAgent` calls `agents/effects.py`
   functions and `<artifact>_agent.run()` directly; `ModuleGenerationWorkflow` calls
   `workflow.execute_activity(...)` and `TemporalAgent.run()`. This mirrors Phase 2's existing
   `agents/effects.py` extraction; the missing piece is doing the same for the *coordination* logic
   still living in both `module.py` and `workflow.py` themselves.
3. `ModuleAgent.run()` and `ModuleGenerationWorkflow.run()` become thin drivers over the shared
   module, dispatching I/O through their respective effects mechanism.
4. Move `tests/test_module_orchestrator.py`'s coordination coverage (research/planning/
   artifact-loop/escalation/skip branches) onto the shared module — one copy of the trickiest logic
   in the system, one set of tests, exercised transitively by both drivers.
5. **Defer the `--legacy` retirement decision itself to a follow-up**, not bundled into this
   workstream. Once B lands, removing `--legacy`/`ModuleAgent` (PLAN.md's literal original ask,
   PHASE4.md's Option A) becomes *safe* — no coverage loss, since the coordination tests moved to
   the shared module — but there's no urgency to do it immediately. Re-evaluate then, since the
   duplication risk B was meant to solve will already be gone regardless of whether `--legacy`
   itself survives.

**Verification:** full test suite green throughout; equivalent coordination coverage exists and
passes against the shared module; a live parity re-run (CLI `--legacy` vs. Temporal, same
methodology as PHASE4.md's 4.2 gate) after the refactor, to prove behavior is unchanged.

**Status: spiked, full refactor tabled.** Per the scope warning above, `agents/module.py` and
`temporal/workflow.py` were read in full and compared method-by-method before committing to the
extraction. Findings:

- **Object-vs-dict representation.** `ModuleAgent` passes live `ModulePlan`/`ExampleDataItem`
  objects and `pathlib.Path`s between steps; `ModuleGenerationWorkflow` is constrained to
  JSON-serializable dicts/strings across every activity boundary. A shared coordination module
  would need one representation or an adapter layer at every call site — not a mechanical
  extraction.
- **Sync vs. async-with-per-call-tuning effects.** `ModuleAgent` calls `agents/effects.py`
  functions directly and synchronously; `ModuleGenerationWorkflow` calls the same functions through
  `self._act(...)`, awaited, with per-call retry policy/heartbeat/timeout choices. Unifying this
  means either dragging async and Temporal-specific tuning into the `--legacy` path or building the
  `EffectsPort`-style protocol (a `LegacyEffectsPort`/`TemporalEffectsPort` pair) sketched during the
  spike — itself a small design project, not a given.
- **Concrete bug found by the comparison:** neither driver had a real existence-check primitive.
  `--legacy` called `pathlib.Path.exists()` directly (a real gap in Phase 2's "every side effect is
  extracted" claim); the Temporal path had no equivalent activity and worked around it by calling
  `read_text_file` and checking for a non-`None` result — functionally equivalent but semantically
  the wrong effect for the question being asked, and wasteful when the content was never used.

Given these, the user chose to land only the concrete, scoped fix — a proper `effects.file_exists()`
primitive — and table the broader de-duplication effort rather than commit to it now:

- Added `effects.file_exists(path: str) -> bool` (`agents/effects.py`), unit-tested in
  `tests/test_effects.py`.
- Registered as the `file_exists` Temporal activity (`temporal/activities.py`, added to
  `ALL_EFFECT_ACTIVITIES`).
- `agents/module.py` now calls `effects.file_exists(...)` at its three coordination-logic existence
  checks (`cleanup_data_dir`, the manifest branch of `artifact_creation_loop`, `_sync_wrapper_script`)
  instead of `Path.exists()`. `print_final_report`'s existence check was left alone deliberately —
  it's client-side/legacy-only terminal reporting, not shared coordination logic.
- `temporal/workflow.py`'s two matching call sites (`_sync_wrapper_script`, the manifest branch of
  `_artifact_creation_loop`) now call the `file_exists` activity directly instead of the
  `read_text_file`-and-check-`None` workaround.
- Full non-live suite (179 passed) and the `temporal`-marked suite (11 passed, including
  replay-determinism) both green after the change.

The rest of Workstream B (steps 1-5 above, the `EffectsPort` design, the coordination-logic
extraction) remains **not started** and is not scheduled — revisit only on explicit request.

## Workstream C — Reliability hardening (independent, low risk, can start anytime) — done

**C1 — Per-activity retry policies.** Add explicit `RetryPolicy(maximum_attempts=N,
backoff_coefficient=..., non_retryable_error_types=[...])` per activity, distinguishing transient
failures (network, LLM rate limits — worth retrying) from deterministic ones (a real bug that
fails identically every time — should fail fast, not retry until `execution_timeout` while bloating
history). This is orthogonal to `MAX_ARTIFACT_LOOPS` (which governs the generate-and-validate loop
*inside* one activity call) — this is Temporal's own retry of a whole activity invocation.

**C2 — Real heartbeating for long-running activities.** Docker build and large downloads currently
rely on generous fixed `start_to_close_timeout`s (PHASE3.5 H0's explicit stopgap) rather than
incremental progress signaling. Real heartbeating (parsing `docker build`'s streaming output, or a
background heartbeat tick alongside the blocking subprocess call) would let Temporal detect a
genuinely-hung activity well before the full timeout, and is a prerequisite for safely *lowering*
those generous timeouts rather than just living with them.

**C3 — Deterministic "already answered" guard for terminal tool calls.** The prompt-level "call
this exactly once" fix applied three times this session (`create_gpunit`, `create_paramgroups`,
`create_tool_research_report`) reduces but doesn't eliminate the model's tendency to loop — it's
been observed recurring even after the fix. Add a deterministic backstop: a small wrapper/decorator
around the "returns-the-actual-final-content" tools (`create_wrapper`, `create_manifest`,
`create_paramgroups`, `create_gpunit`, `create_documentation`, `create_tool_research_report`) that
tracks call count and, on a repeat call within the same run, returns a sharper `ModelRetry` message
("You already called this and got your answer — use it, don't call this again") instead of quietly
re-running. Converts an open-ended, cost-accumulating loop into a bounded, self-correcting one.

**Implementation:** C1 — `temporal/workflow.py::_act` (the single funnel every effects activity call
goes through) now takes a `retry_policy` parameter, defaulting to a bounded policy
(`maximum_attempts=4`, exponential backoff capped at 30s) instead of Temporal's unbounded default;
the two genuinely long-running activities (`build_and_test_image`, `download_one`) get a separate,
even-more-bounded policy (`maximum_attempts=2`) since each attempt can itself take minutes. Note
this only ever governs genuine activity-*execution* exceptions — `agents/effects.py` functions
return structured result objects for expected failures (a bad Dockerfile failing to build, a linter
rejecting a wrapper), they never raise for those, so `MAX_ARTIFACT_LOOPS`/`MAX_ESCALATIONS` still
own that retry loop entirely at the workflow-logic level, undisturbed.

C2 — added `temporal/activities.py::_wrap_with_heartbeat`, used for `build_and_test_image` and
`download_one`: runs the (Temporal-agnostic, unchanged) `agents/effects.py` function in a
background thread and calls `activity.heartbeat()` from the activity's own thread every 10s while
polling it. This is what makes `heartbeat_timeout` finally safe to declare on those two call sites
(added, 30s) — a genuinely hung build/download is now detected well before the 20-30 minute
`start_to_close_timeout`, without touching `agents/effects.py` itself (preserving its deliberate
Temporal-agnostic design from Phase 2) or asking the --legacy path to know anything about
heartbeating.

C3 — added `agents/models.py::guard_single_call`, applied to all 6 terminal tools
(`create_wrapper`, `create_manifest`, `create_paramgroups`, `create_gpunit`, `create_documentation`,
`create_tool_research_report`). Counts *prior successful* completions (`ToolReturnPart` entries in
`context.messages`, not `ToolCallPart` attempts — see the bug below) and raises `ModelRetry` on a
repeat. **Two real bugs found building this, both live-tested against the actual regression, not
just reasoned about:**
  - Counting `ToolCallPart` (every attempt, including ones that failed argument validation and were
    transparently retried by pydantic-ai itself) instead of `ToolReturnPart` (only successes) made
    the guard fire on the very next attempt for a tool that had *never* successfully returned even
    once, immediately exhausting the retry budget. Broke 4 existing tests
    (`tests/test_researcher_agent.py`) the moment it was wired up — caught by the existing suite,
    not a live run. Fixed by switching to counting `ToolReturnPart`.
  - `context.messages` isn't available inside a Temporal *activity* by default — tools run there,
    not in the workflow itself, and `TemporalRunContext` only serializes a fixed subset of
    `RunContext` across that boundary (`messages` isn't in it). The very first live Temporal-path
    run after adding the guard failed outright with `UserError: 'TemporalRunContext' object has no
    attribute 'messages'`. Shipping the full message history to fix it would have reintroduced the
    Workstream A payload-size problem, so instead `temporal/agents.py::_GuardedRunContext` (a
    `TemporalRunContext` subclass, passed via `run_context_type=` to the 6 relevant `TemporalAgent`s)
    computes and serializes only `completed_tool_names: list[str]` — the one small derived fact the
    guard needs. `guard_single_call` checks for that first, falling back to scanning
    `context.messages` directly when absent (the --legacy/test path).

**Verified:**
  - C1: `tests/test_workflow.py::test_retry_policy_bounds_attempts` — a new `RetryPolicyWorkflow`/
    `always_failing_activity` fixture pair proves Temporal actually stops at `maximum_attempts`
    (not more, not fewer), via a real `WorkflowEnvironment`.
  - C2: `tests/test_workflow.py::test_heartbeat_wrapped_activity_survives_short_heartbeat_timeout`
    — a `_wrap_with_heartbeat`-wrapped activity (via the real production wrapper, 0.3s heartbeat
    interval for test speed) runs for 1.5s under a 0.6s `heartbeat_timeout` and completes normally,
    proving the real heartbeats keep it alive (the existing `TimeoutPolicyWorkflow` test already
    covers the negative case: a non-heartbeating activity under the same short timeout is killed).
  - C3: `tests/test_guard_single_call.py` — two `FunctionModel`-driven tests forcing a real second
    tool call: one proves the guard blocks it with the expected `ModelRetry` message and the tool
    body only ever runs once; the other proves a validation-retry (bad args, tool body never
    reached) does *not* trip the guard, locking in the fix for the bug above.
  - **All of Workstream A + C together, live:** a full Temporal-path pipeline run (Docker-hosted
    Temporal server, real worker, real `ollama:qwen3-coder-next`, samtools) completed successfully
    with wrapper/manifest/paramgroups/gpunit/documentation **all validated on attempt 1**, zero
    `PayloadSizeWarning`s, zero errors, and every terminal tool (including
    `create_tool_research_report`, `create_paramgroups` — the two that had looped repeatedly in
    earlier Phase 4 sessions) called exactly once. `uv run pytest -m "not live"` (175 passed) and
    `uv run pytest -m temporal` (10 passed) green throughout.

## Workstream D — Human-in-the-loop GenePattern-upload approval gate — done

PLAN.md's original ask: "a workflow signal to approve GenePattern upload before the `install`
pseudo-artifact runs." Temporal-specific (waiting indefinitely for a human signal is exactly what
durable workflows are good at, and doesn't map cleanly onto the synchronous CLI/`--legacy` path) —
implemented only in `ModuleGenerationWorkflow`.

**Implementation:**
- `@workflow.signal def approve_upload(self)` / `reject_upload(self)` set `self._upload_decision`,
  but only if `self._awaiting_upload_approval` is currently true — both are no-ops otherwise, since
  Temporal delivers signals asynchronously and a client can't know the exact moment (or whether)
  the workflow is actually waiting; a signal that arrives early, late, or with nothing pending must
  not error or corrupt state.
- A new `require_upload_approval: bool = False` parameter threads through `run()` →
  `_generate_all_artifacts` → `_run_install_artifact`. When true (and an upload was actually
  requested, i.e. `gp_server`/`gp_user` are set), `_run_install_artifact` sets
  `self._awaiting_upload_approval = True` and `await workflow.wait_condition(lambda: self._upload_decision is not None)`
  — no timeout of its own; the workflow's overall `execution_timeout` is the real outer bound (see
  below). On `'rejected'`, it records `status.upload_status = 'declined'` and returns success
  *without* uploading (a declined upload is not a pipeline failure — the module was generated
  correctly, a human just chose not to publish it). Otherwise it proceeds to the pre-existing
  `activities.upload_module` call as before, now also recording `'uploaded'`/`'failed'` on
  `status.upload_status` (a new field on `ModuleGenerationStatus`) so a client can distinguish "no
  upload requested" (`None`) from all three outcomes without reading log text.
- `progress()` now also returns `awaiting_upload_approval`, so a poller can render a
  decision-needed UI distinct from "still working."
- `temporal/client.py::start_module_generation` gained `require_upload_approval` and an
  `execution_timeout` override (default 2h is almost certainly too short to wait on a human; the
  CLI and Django both pass 7 days when approval is required) and a new
  `decide_upload(workflow_id, approve, client=None)` helper wrapping `handle.signal(...)`.
- **CLI:** `--require-upload-approval`, plus `--approve-upload WORKFLOW_ID` / `--reject-upload
  WORKFLOW_ID` as a separate mode that only signals an existing workflow and exits (added to
  `main()` as an early return, before any generation setup). Submitting with
  `--require-upload-approval` prints the exact commands to run to approve/reject it.
- **Django:** new `gp_server`/`gp_user`/`gp_password`/`require_upload_approval` form fields
  (previously entirely absent from the web UI — GP upload was only ever CLI-accessible); a new
  `/upload-decision/<module_dir>/` POST view signaling via `decide_upload`; `_map_state_to_status`
  surfaces `awaiting_upload_approval`; the dashboard shows an amber "Approve Upload / Reject" panel
  whenever a poll reports it, wired to the new endpoint.

Default **off** — this doesn't surprise existing automated CLI users who already pass
`--gp-server`/credentials expecting immediate upload.

**Verified:**
- Unit tests (`tests/test_workflow_progress.py`, no server): `approve_upload`/`reject_upload` are
  no-ops when nothing is pending, and set the correct decision when something is;
  `progress()['awaiting_upload_approval']` reflects the flag.
- Live mechanism test (`tests/test_workflow.py::test_signal_unblocks_a_waiting_workflow`): a small
  dedicated fixture workflow (`ApprovalGateWorkflow`, exercising the identical signal-and-
  `wait_condition` shape) proves a client signal sent after the workflow starts actually reaches
  and unblocks a real, running `wait_condition` — for both approval and rejection — via a genuine
  `WorkflowEnvironment`.
- **Full live integration**, through the real stack (Docker-hosted Temporal server, real worker,
  real `ollama:qwen3-coder-next`, real Django dev server, logged in over HTTP): submitted a scoped
  run (wrapper-only, fake `gp_server`/`gp_user` so no real network call is attempted, matching how
  Phase 4/5's other live tests kept cost/time bounded) with `require_upload_approval=True`; polled
  the real `/status/<module>/` endpoint until it reported `awaiting_upload_approval: true`; confirmed
  via `/console/<module>/` that the workflow logged exactly the expected pause message; sent a
  reject decision through the real `/upload-decision/<module>/` endpoint; confirmed the workflow
  unblocked immediately (visible in the worker log), completed with `status: "success"` and
  `upload_status: "declined"`, and that `samtools.zip` existed on disk (the module was generated
  correctly; only the upload was skipped). Also confirmed a decision sent to an already-completed
  workflow correctly returns 404 rather than silently succeeding or erroring.
- The "approved, then the upload activity actually runs" branch reuses
  `activities.upload_module`/`_act` unchanged from the pre-existing (pre-Workstream-D) code path
  and Workstream C1's already-verified bounded retry policy — not re-tested live here as new
  behavior, since it isn't new behavior.

`uv run pytest -m "not live"` (179 passed) and `uv run pytest -m temporal` (11 passed) green
throughout.

## Workstream E — Concurrent multi-run validation (depends on Workstream A) — done

Unsafe to attempt meaningfully before A resolves the shared-filesystem/payload questions. Once A
lands: run N simultaneous workflows against 2+ worker processes and confirm no cross-run
interference — module directory collisions, and process-wide state such as `agents/researcher.py`'s
module-level `_brave_lock`/`_brave_last_call` Brave rate-limiter (correct *within* one worker
process, but worth confirming it doesn't create unintended cross-run contention at higher
concurrency, or that it's fine because each worker process is independent).

**Found and fixed: a real module-directory-collision bug.** `agents/effects.py::make_module_dir`'s
no-explicit-`module_dir` branch named directories from `tool_name` + a second-granularity timestamp
and created them with `Path.mkdir(parents=True, exist_ok=True)` — silently succeeding even if the
directory already existed. Two workflows for the same tool starting in the same wall-clock second
(a real scenario: the Django UI computes this same name synchronously in the request thread and
also uses it as the Temporal `workflow_id`) would silently share one directory. Fix:

- `make_module_dir`'s auto-named branch now creates the directory with `mkdir(exist_ok=False)` (atomic
  at the OS level) and, on `FileExistsError`, bumps a numeric suffix and retries — the explicit-
  `module_dir` branch (an intentional, caller-specified target) is unchanged and still reuses an
  existing directory on purpose.
- `app/generator/views.py::generate_module` no longer duplicates the naming/`mkdir` logic inline;
  it now calls `effects.make_module_dir` directly (removing the duplication *and* inheriting the fix
  in one change).

**Verification:**
- `tests/test_effects.py`: `test_make_module_dir_bumps_suffix_on_name_collision` (sequential) and
  `test_make_module_dir_concurrent_same_name_all_unique` (8 real threads racing the same
  tool_name/timestamp via `ThreadPoolExecutor`) — both fast, no server needed.
- `tests/test_workflow.py::test_concurrent_workflows_across_two_workers_avoid_module_dir_collision`
  (live, `temporal`-marked): a new fixture (`temporal/_test_fixtures.py::MakeModuleDirWorkflow`)
  calls the real `make_module_dir` *activity*; the test backs two `Worker`s with two independent
  client connections on the same task queue (the SDK-level equivalent of two worker processes —
  a single client can't register two Workers on one queue, itself a useful thing learned live) and
  fires 6 concurrent workflow executions at one deliberately-identical directory name.
- **Regression-confirmed, not just written and trusted:** the fix was temporarily reverted and the
  same live test re-run — it failed exactly as predicted (`assert 1 == 6`, all six workflows
  resolved to the same directory) — before restoring the fix and re-confirming green. This is the
  same "prove the test would have caught the bug" discipline used for `guard_single_call` earlier
  in Phase 5.
- `tests/test_brave_rate_limiter.py` (new, fast, no server): drives 6 threads through the real
  `_brave_get` concurrently (with `_BRAVE_MIN_INTERVAL` monkeypatched down so the test runs in
  well under a second) and asserts every call reaches the mocked HTTP layer exactly once, correctly
  serialized with the minimum gap enforced — confirming the "correct *within* one worker process"
  assumption rather than just asserting it. The cross-*process* case (two worker processes sharing
  one Brave API key are **not** jointly rate-limited against each other, since the lock/timestamp
  are module-level state private to each process) is a real, accepted limitation, not fixed here —
  building a distributed rate limiter (e.g. Redis-backed) for a capability nothing in this codebase
  currently needs would be exactly the kind of premature infrastructure the project has avoided
  elsewhere (see A3's shared-filesystem decision).
- Full non-live suite (185 passed) and `temporal`-marked suite (12 passed) both green.
- **Not done, and deliberately so:** a full concurrent run of the real LLM-backed pipeline across
  genuinely separate OS worker processes. The verification above is mechanism-level (real Temporal
  SDK dispatch, real filesystem, real thread concurrency) rather than a full end-to-end live run —
  judged lower value for the risk, since the local model's already-documented tool-call-looping
  flake (Phase 4's parity gate) makes multiple simultaneous full pipeline runs slow and
  failure-prone in a way orthogonal to what's actually being validated here, and the two-client-
  connection setup exercises the identical dispatch/locking code path a second real OS process
  would.

## Workstream F — Observability

- **Dashboards/alerts** on workflow failure rate, activity retry counts (become meaningful once C1
  lands), end-to-end latency — via Temporal's own Web UI/CLI (`temporal workflow list`, metrics
  export) or Logfire (`configure_telemetry()` already composes with Temporal's tracing per
  CONSIDERATIONS.md; confirm the *worker* process actually has it enabled — PHASE3.5's H1 fixed the
  worker not loading `.env` at all, so this may already be partially covered; verify rather than
  assume).
- **Console-log fidelity** (the accepted PHASE4.md 4.4 gap): revisit whether the phase-level-only
  log is actually a problem in practice. Options, cheapest first: (i) document more prominently in
  the UI itself that detailed per-tool output lives in the worker's own logs; (ii) route tool-level
  `print()`s through `temporalio.activity.logger` (visible in the worker/Temporal UI, still not in
  the workflow's own progress buffer); (iii) have activities return captured stdout as part of
  their result for the workflow to append to its bounded log sink — directly in tension with
  Workstream A's payload-size goals, so only worth it if (i)/(ii) prove insufficient.
  **Recommendation: (i) first**, revisit only on actual user complaint.

## Workstream G — Docs & follow-through

- Update CLAUDE.md/README/this doc as each workstream lands, matching the discipline of Phases 3-4.
- Once Workstream B lands, explicitly revisit (don't silently carry forward) the `--legacy`/
  `ModuleAgent` retirement decision — Option B was chosen because removing it *before* de-duplication
  meant losing real test coverage; that reason goes away once B ships.

## Recommended sequencing

```
A (storage/payload)  ─┬─▶ E (concurrency)
   [DONE]              │      [DONE]
                        │
C (retry/heartbeat/    │
   anti-loop guard)  ──┼─▶ B (de-dup, tabled)  ──▶  G (revisit --legacy decision)
   [DONE]              │      [spiked, not started]
                        │
D (approval gate)  ─────┘  [independent of everything else]
   [DONE]

F (observability) ── independent, ongoing
```

A, C, D, and E are **done** (see their writeups above for implementation + live verification). B was
spiked (see its section above) and its full refactor deliberately tabled, with only its concrete
`file_exists()` finding landed. F remains open and doesn't depend on anything above — pick up
opportunistically. G trails whichever of B/F it's documenting.

## Acceptance criteria

1. ✅ No `PayloadSizeWarning` in normal operation (Workstream A1/A2 — verified live, zero warnings
   across a full pipeline run). A deliberately-forced multi-retry run staying well under Temporal's
   workflow history size limit was not separately re-tested this pass (the original TERMINATED
   failure was reproduced accidentally, not deliberately, during Phase 4 debugging) — reasonable
   confidence from A1/A2 shrinking the recurring-payload contributors, but worth a dedicated stress
   test if history-size issues recur.
2. ✅ The shared-filesystem constraint is enforced with a clear failure, not silent (Workstream A3
   — `_check_shared_filesystem`/`_filesystem_warning`, both verified directly).
3. ⬜ `agents/module.py` and `temporal/workflow.py` no longer duplicate coordination logic — **not
   attempted this phase**; explicitly deferred (Workstream B), per the plan's own "spike first, get
   a go/no-go before the full refactor" scoping.
4. ✅ At least the most failure-prone activities (Docker build, downloads, LLM calls) have explicit,
   justified `RetryPolicy`s distinguishing transient from deterministic failure (Workstream C1 —
   note LLM-call activities are governed by pydantic-ai's own Temporal integration, not `_act`;
   C1 covers the `agents/effects.py`-derived activities).
5. ✅ The "agent loops on a terminal tool call" bug class has a deterministic backstop, not just
   prompt-level instructions (Workstream C3 — verified against the exact two real bugs found
   building it, plus a live full-pipeline run with zero false positives).
6. ✅ A human-in-the-loop approval gate exists for the GenePattern upload step, opt-in and
   defaulting off, exposed through both the CLI and the Django UI (Workstream D — verified live
   through the real Django HTTP endpoints, not just internal mechanisms).
7. ✅ `uv run pytest -m "not live"` (185 passed) and `uv run pytest -m temporal` (12 passed) green
   throughout every workstream landed.
8. ✅ Each landed workstream (A, C, D, E) verified live against a real Temporal server + worker
   run, not just via unit tests — matching the standard set in Phase 4.
9. ✅ N simultaneous workflows against 2+ workers don't corrupt each other's module directories, and
   the one piece of process-wide mutable state (the Brave rate limiter) is confirmed correct under
   real concurrent access (Workstream E — a real collision bug was found, fixed, and the fix
   regression-confirmed by re-running the failing test against the pre-fix code).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Workstream B (de-dup) is large enough to stall or be abandoned mid-refactor | Spike the pure/impure split first, as its own small deliverable, before committing to the full extraction |
| Changing `ModuleGenerationStatus.to_dict()`'s shape (A1) breaks the Django UI's status contract | `app/generator/views.py::_map_state_to_status` is the one place that shape is consumed — update it in the same change, verify live against the real UI (as Phase 4 did) |
| Retry-policy changes (C1) mask real bugs by retrying too aggressively, or fail real transient errors too eagerly | Start conservative (retry only on a documented allow-list of transient error signatures); expand based on observed failure logs, not guesses |
| Human-in-the-loop gate (D) surprises existing automated CLI users | Default off; opt-in flag only |
| Concurrency testing (E) surfaces a real multi-host storage need | Did not surface one — the bug found (module-directory collision) was a same-host race, resolved without touching A's shared-filesystem decision |

## Suggested commit sequence

1. `refactor(status): stop re-embedding full research/plan text in every payload` (A1)
2. `fix: bound error_history/escalation_log growth` (A2)
3. `feat(deploy): enforce shared-filesystem assumption with a clear startup check` (A3)
4. `feat(temporal): per-activity retry policies` (C1)
5. `feat(temporal): real heartbeating for docker build / downloads` (C2)
6. `fix(agents): deterministic guard against repeated terminal tool calls` (C3)
7. `refactor: extract shared orchestration logic; ModuleAgent/ModuleGenerationWorkflow become thin drivers` (B)
8. `feat(temporal): human-in-the-loop GenePattern upload approval signal` (D)
9. `test: validate concurrent multi-run execution` (E)
10. `feat(observability): dashboards/alerts on failure rate, retries, latency` (F)
11. `docs: Phase 5 follow-through; revisit --legacy retirement decision` (G)

Numbering reflects the recommended dependency order above, not a mandate to do all eleven before
shipping anything — each is independently mergeable.
