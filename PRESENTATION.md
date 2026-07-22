# Adding Temporal to the GenePattern Module AI Toolkit

**A durable-execution case study: replacing a hand-rolled checkpoint system with Temporal in a
multi-agent LLM pipeline.**

## What this project is

The GenePattern Module AI Toolkit is a multi-agent [Pydantic AI](https://ai.pydantic.dev/)
pipeline that turns a bioinformatics command-line tool into a complete, validated
[GenePattern](https://www.genepattern.org/) module — a wrapper script, a manifest, a parameter
schema, a test suite, documentation, and a Docker image. Eight LLM agents run in sequence
(research → planning → six parallel-but-ordered artifact generators), each generation step
validated by a dedicated linter with automatic retry and cross-artifact error escalation (a
Dockerfile build failure on a missing package gets routed back to regenerate the *wrapper*, not
the Dockerfile, if the classifier determines the wrapper is the true upstream cause). A full run
is minutes long, makes dozens of LLM calls, shells out to Docker, downloads example data over
HTTP, and optionally uploads the finished module to a GenePattern server.

That shape — long-running, many external side effects, a retry/escalation loop, real cost per
step — is exactly Temporal's sweet spot. This document tells the story of how durability was
handled before Temporal, why and how Temporal was integrated, what the resulting architecture
looks like, the trade-offs made along the way, and what's still left to do.

---

## Before Temporal: a hand-rolled durability layer

Before this work, the pipeline's only fault tolerance was self-built:

- `ModuleAgent.run()` (the orchestrator) called `self.save_status(status)` after nearly every
  phase and artifact step, serializing a `ModuleGenerationStatus` object — research findings,
  planning output, per-artifact generation/validation state, token counts, escalation history —
  to a `status.json` file in the module's output directory.
- A `--resume MODULE_DIR` CLI flag reloaded that file, reconstructed the status object (including
  re-hydrating a `ModulePlan` from raw JSON), and continued the pipeline from wherever it left
  off — skipping any artifact already marked `validated: true`.
- The Django web UI *depended* on this file for its live progress display: it tailed a
  subprocess's stdout to a log file and separately re-read `status.json` on every poll to derive
  a `running` / `success` / `error` state for the sidebar and status panel.

This worked, but it was a manual, best-effort re-implementation of what a durable execution
engine provides natively:

- **No mid-step crash recovery.** If the process died in the middle of an LLM call or a Docker
  build, the *next* `--resume` picked up at the last artifact-level checkpoint, not the last
  action — a partially-written Dockerfile mid-build, a half-downloaded file, or an
  in-flight-but-lost LLM call would silently need to be redone in full on the next attempt, and
  there was no automatic mechanism forcing that resume to actually happen; a human had to notice
  the run died and re-invoke it with the right flag.
- **No distributed retry semantics.** Retries existed at exactly one granularity —
  `MAX_ARTIFACT_LOOPS` re-tries of "generate this artifact with the LLM, then validate it" — with
  no way to distinguish a transient failure (a flaky network call) from a deterministic one (a
  logic bug that will fail identically forever), and no backoff.
- **No real observability into an in-flight run** beyond a console log and a file that had to be
  read and reasoned about by hand; there was no query interface, no structured history, no replay.
- **Checkpoint bookkeeping was hand-maintained code**, not a platform feature — every new phase or
  artifact type that needed durability meant another `save_status()` call site to remember, and
  every field added to the status object was another thing that had to serialize correctly to
  JSON and reconstruct correctly on load.

This diagnosis — "the project already contains a hand-rolled version of what Temporal does" — was
the actual argument for adopting Temporal, made explicit in the design doc before any code was
written (`temporal/CONSIDERATIONS.md`): the integration would **replace an existing subsystem**,
not bolt a new one onto a project that didn't need it.

---

## Evaluating Temporal

Pydantic AI ships first-class, co-maintained Temporal support (`pydantic-ai[temporal]`), not a
community add-on: `TemporalAgent` wraps an existing `Agent` so every model request and tool call
automatically becomes a durable, retried Temporal activity, while `PydanticAIWorkflow` provides
the workflow base class. That made the framework-level integration cost low; the real cost was
adapting *this* pipeline to workflow constraints. Two decisions came out of the evaluation phase:

**Temporal vs. DBOS.** Pydantic AI also officially supports DBOS (Postgres-backed, no separate
worker/server cluster to operate) — a serious alternative for a project whose primary ask was
"don't lose a 5-minute run when the box reboots." Temporal was chosen because the project's
priorities extended past that: eventual support for concurrent module generations, a
human-in-the-loop approval gate before GenePattern upload (a natural fit once upload is a Temporal
activity — waiting indefinitely for a human signal is exactly what durable workflows are good
at), and the richer operational tooling (Web UI, CLI, replay debugging) mattered more here than
avoiding a second piece of infrastructure.

**Gotchas identified up front — and later confirmed empirically.** The evaluation doc called out
five project-specific risks before any implementation began, including: *"Inside tools running as
activities, `RunContext` excludes `model`, `prompt`, `messages`, and `tracer`. Audit `@agent.tool`
functions... for reliance on those — likely clean, but check."* Months later, during Phase 5
hardening work, a new deterministic-retry guard built for a different reason (below) started
reading `context.messages` inside a tool — and the very first live Temporal run after adding it
failed outright with exactly the predicted `RunContext` limitation. The fix (below, under
"Design decisions") was fast *because* the failure mode had already been named and understood in
advance. That's the value of writing the risk down before you need it.

Another named gotcha — **activity payload limits vs. on-disk shared state** — turned out to be
the single most consequential trade-off of the whole project; see "Design decisions" below.

---

## The implementation: staged, not a rewrite

The integration shipped as five sequential phases, each with its own written plan, explicit
acceptance criteria, and a green test suite as the exit gate. This staging was deliberate: **the
Temporal workflow was built and proven correct *before* the legacy system was touched**, so at no
point was there a window where the pipeline was down or unverified.

| Phase | What it did | Why it's a separate phase |
|---|---|---|
| **0 — Baseline** | Modernized dependency management (`uv`, locked `pyproject.toml`), resolved the newest Pydantic AI version compatible with the project's other dependencies, got the test suite green. | Temporal work on a broken/unreproducible environment compounds risk for no reason — fix the ground first. |
| **1 — Async conversion** | Converted the four `run_sync()` call sites (including one *nested* inside a sync call chain that would have raised `RuntimeError: this event loop is already running` once the outer loop went async) to `async`/`await`. | `TemporalAgent` is async-only; this is a hard prerequisite, and it shipped as a standalone improvement with zero Temporal code, so it could be verified in isolation. |
| **2 — Extract side effects** | Pulled every non-deterministic operation (file I/O, HTTP downloads, Docker subprocess calls, zip/upload) out of the orchestrator into standalone, serializable-in/out, `Logger`-free functions (`agents/effects.py`) returning structured result models. | Temporal workflow code must be deterministic — no direct file I/O, subprocess calls, or `datetime.now()`. Doing this extraction *without* Temporal in the loop meant Phase 3 became "decorate with `@activity.defn`," not "rewrite while also learning Temporal." It also meant the extracted functions work unmodified from *both* the legacy path and Temporal activities — no duplication. |
| **3 — Temporal in shadow mode** | Built `ModuleGenerationWorkflow`, wrapped all 8 agents in `TemporalAgent`, decorated all 12 effects functions as activities, stood up the worker and client — while **keeping `status.json` writes alive as a shadow**, so the new path's output could be compared against the old one without depending on it. | Prove the new system works before anyone depends on it. The workflow is a faithful *port* of the existing async, already-effects-clean coordination logic, not a redesign — retry counting, error classification, and escalation-queue reordering carried over essentially unchanged, because Phase 1/2 had already made them pure. |
| **3.5 — Hardening** | A dedicated pass specifically to find gaps before relying on the shadow-mode build, via live execution against a real `WorkflowEnvironment`, not just code review. | See below — this is where the project's most instructive bugs were found. |
| **4 — Cutover** | Removed the `status.json` shadow write from the workflow; ran a real parity gate (Temporal path vs. the legacy in-process path, same inputs, real LLM, diffed output); repointed the Django UI from file-tailing to the Temporal client; removed `--resume` and the legacy status-persistence code entirely. | Nothing destructive proceeded until the parity gate passed live — not in a unit test, an actual end-to-end generation run compared artifact-by-artifact against the pre-Temporal path. |
| **5 — Production hardening (in progress)** | Bounded, differentiated retry policies; real activity heartbeating; payload-size discipline; a deterministic guard against a recurring LLM failure mode; a human-in-the-loop upload-approval gate (`@workflow.signal`); concurrent multi-run validation (and a real bug it found and fixed). See "What's not yet done" at the end of this document for the remaining scope. | Temporal unlocks operational maturity the shadow-mode build didn't need yet — this phase spends it. |

### What Phase 3.5's hardening pass actually found

This is worth calling out specifically because it's the clearest evidence that live execution,
not code review, is what catches real Temporal integration bugs:

- **A heartbeat bug that silently killed a 20-minute Docker build at 30 seconds.** The build
  activity declared `heartbeat_timeout=30s` but never called `activity.heartbeat()`. Temporal
  fails any activity that declares a heartbeat timeout it never satisfies — the build was dying at
  30 seconds regardless of its generous `start_to_close_timeout`. This was reproduced and
  confirmed in a live `WorkflowEnvironment` spike, not inferred from documentation. The fix at the
  time was the honest one: drop the heartbeat declaration and rely on generous fixed timeouts
  until real heartbeating could be built properly (which Phase 5 later did — see below).
- **The worker process never loaded environment configuration.** The worker — not the client — is
  where agents actually execute (model requests are activities), but it never called
  `load_dotenv()`, so it silently ran on a default model with no search API key and no telemetry,
  with no error to indicate anything was wrong.
- **The Django web UI was silently broken** by an earlier partial change: it shelled out to the
  CLI without the flag needed to keep using the in-process path, so every web-submitted run
  started requiring Temporal infrastructure that wasn't documented as a new requirement anywhere.

None of these would have been caught by unit tests or a code read; they required actually running
the system.

### The Phase 4 parity gate

Before removing anything, one `samtools` module was generated through the Temporal path and one
through the legacy in-process path, both against the same real local LLM, and the artifact sets
were diffed. Both completed successfully with structurally identical output (same manifest/
wrapper/paramgroups conventions; differences were limited to the LLM's own creative
non-determinism between separate runs, not a code-path divergence). Getting to that clean
comparison surfaced eight real, pre-existing bugs in the shared pipeline code — none Temporal-
specific, all exercised identically by both paths — including a local model's tendency to
JSON-stringify its own tool-call arguments, silently truncate large tool-call payloads mid-
generation, and repeatedly re-invoke a tool after it had already returned its final answer. Fixing
these live, one at a time, with the fix immediately re-verified against a fresh run, is what
"parity gate" meant in practice — not a one-time diff, but an iterate-until-clean loop.

---

## Architecture today

```
                    ┌─────────────┐        ┌──────────────────────┐
  CLI (generate-     │             │        │                      │
  module.py) ────────▶  Temporal   │◀──────▶│   Temporal Server    │
                    │   Client    │        │  (workflow history,  │
  Django Web UI ─────▶  (submit,   │        │   task queues,       │
  (submit + poll     │  query,     │        │   retries, replay)   │
   progress)         │  signal*)   │        │                      │
                    └─────────────┘        └───────────┬──────────┘
                                                          │
                                                          ▼
                                            ┌──────────────────────────┐
                                            │   Worker Process(es)     │
                                            │                          │
                                            │  ModuleGenerationWorkflow│
                                            │  (deterministic          │
                                            │   coordination:          │
                                            │   research → plan →      │
                                            │   6 artifacts + install) │
                                            │           │              │
                                            │           ▼              │
                                            │  8 TemporalAgents  +     │
                                            │  14 effects activities   │
                                            └───────────┬──────────────┘
                                                          │
                                    ┌─────────────────────┼─────────────────────┐
                                    ▼                     ▼                     ▼
                             LLM provider          Docker daemon        Filesystem / HTTP /
                          (OpenAI / Ollama)      (build + runtime      GenePattern server
                                                       test)              (upload)
```

**Two execution paths coexist by design.** The default path is the Temporal one above. A
`--legacy` CLI flag also runs the exact same pipeline logic **in-process**, with no Temporal
server or worker required — useful for local development with no infrastructure, and, more
importantly, kept as the system's only end-to-end-tested reference implementation of the
coordination logic (see "Option B" under Design Decisions). Both paths call the *same* agent
objects, the *same* `agents/effects.py` functions, and the *same* error-classification logic;
`ModuleGenerationWorkflow` and the legacy `ModuleAgent` are two drivers over duplicated — not
shared — coordination code today, a known trade-off discussed below.

---

## Inventory: every Temporal construct in the system

### Workflow

| Construct | Name | Notes |
|---|---|---|
| `@workflow.defn` | `ModuleGenerationWorkflow` (`temporal/workflow.py`) | The only production workflow. Ports `ModuleAgent`'s coordination: research → planning → per-artifact generation loop with cross-artifact error escalation → zip/install/optional upload. `__pydantic_ai_agents__` lists all 8 `TemporalAgent`s below. |
| `@workflow.run` | `run(tool_info, skip_artifacts, max_loops, max_escalations, no_zip, zip_only, docker_push, gp_server, gp_user, gp_password, require_upload_approval)` | Returns `{'success': bool, 'module_directory': str, 'status': dict}` — the workflow's own return value is the final result; no side-channel status file. |
| `@workflow.query` | `progress()` | Returns `{'status': <ModuleGenerationStatus.to_dict()>, 'log': [...], 'awaiting_upload_approval': bool}` — a structured snapshot, a bounded (500-line) tail of recent log output, and whether the workflow is currently paused on the upload-approval gate below. Replaces the `status.json` tailing the Django UI used to do; answerable on both running and completed workflows within the server's retention window. |
| `@workflow.signal` | `approve_upload()` / `reject_upload()` | The human-in-the-loop GenePattern-upload gate. No-op if nothing is currently pending (signals are asynchronous — a client can't know the exact moment, or whether, the workflow is actually waiting). Opt-in via `require_upload_approval=True`; default off. |

### Activities

All 14 are synchronous functions (except one, noted below) run via a `ThreadPoolExecutor` on the
worker, decorated by `temporal/activities.py` rather than in-place — a deliberate indirection so
Temporal's workflow-sandbox re-import of the module doesn't raise "activity already defined" on a
shared function object.

| Activity | Wraps | Task queue | Notable timeout/retry/heartbeat config |
|---|---|---|---|
| `make_module_dir` | `agents.effects.make_module_dir` | `module-generation` | Default (60s timeout, bounded retry policy) |
| `write_text_file` | `agents.effects.write_text_file` | `module-generation` | Default |
| `read_text_file` | `agents.effects.read_text_file` | `module-generation` | Default |
| `file_exists` | `agents.effects.file_exists` | `module-generation` | Default. Added during the Workstream B spike (below) — the workflow previously faked an existence check by calling `read_text_file` and testing for non-`None`, discarding the content it never needed |
| `remove_dir` | `agents.effects.remove_dir` | `module-generation` | Default |
| `find_wrapper_file` | `agents.effects.find_wrapper_file` | `module-generation` | Default |
| `read_manifest_docker_image` | `agents.effects.read_manifest_docker_image` | `module-generation` | Default |
| `download_one` | `agents.effects.download_one` | `module-generation` | 30 min timeout; **heartbeat-wrapped** (10s interval) + 30s `heartbeat_timeout`; low-attempt-count retry policy (each attempt is expensive) |
| `upload_module` | `agents.effects.upload_module` | `module-generation` | Default |
| `zip_artifacts` | `agents.effects.zip_artifacts` | `module-generation` | Default |
| `docker_push` | `agents.effects.docker_push` | `module-generation` | Default |
| `run_linter` | `agents.effects.run_linter` | `module-generation` | Default; documentation's variant uses a 5-min timeout (fetches a URL) |
| `build_and_test_image` | `agents.effects.build_and_test_image` | `docker-builds` (separate queue — the only activity that needs Docker on the host) | 20 min timeout; **heartbeat-wrapped** + 30s `heartbeat_timeout`; low-attempt-count retry policy |
| `build_dockerfile_runtime_command` | `dockerfile.runtime.build_runtime_command` (async, the one exception) | `module-generation` | A deliberately *coarse* activity — wraps a three-strategy fallback (manifest introspection / wrapper introspection / placeholder substitution) including its own internal, non-durable LLM call as one unit, rather than decomposing that intricate logic into workflow code by hand |

Plus, implicitly, one **model-request/tool-call activity per LLM interaction**, auto-generated by
`PydanticAIPlugin` from each `TemporalAgent` — these aren't hand-registered, but they're the
majority of activities executed in any real run.

### TemporalAgents (8)

| Agent | Name (stable, permanent) | Custom `run_context_type` |
|---|---|---|
| Researcher | `researcher` | `_GuardedRunContext` |
| Planner | `planner` | *(default)* |
| Wrapper generator | `wrapper` | `_GuardedRunContext` |
| Manifest generator | `manifest` | `_GuardedRunContext` |
| Paramgroups generator | `paramgroups` | `_GuardedRunContext` |
| GPUnit test generator | `gpunit` | `_GuardedRunContext` |
| Documentation generator | `documentation` | `_GuardedRunContext` |
| Dockerfile generator | `dockerfile` | *(default)* |

One additional agent — a small dockerfile *hint-mapping* LLM call — is deliberately **not**
wrapped as a `TemporalAgent`; it runs as a plain, non-durable call inside the coarse
`build_dockerfile_runtime_command` activity above, an accepted trade-off (see Design Decisions).

### Workers & task queues

- `temporal/worker.py`, run as `uv run python -m temporal.worker`. Runs two `Worker`s
  concurrently in one process by default (`--queue both`), or either independently
  (`--queue module-generation` / `--queue docker-builds`) for production deployments that want to
  scale or isolate the Docker-capable worker separately.
- **`module-generation`** — the workflow definition, all agent-driven activities, and every
  effects activity except the Docker build.
- **`docker-builds`** — only `build_and_test_image`, routable to a worker process that actually
  has Docker available.
- A shared `ThreadPoolExecutor(max_workers=8)` serves as the `activity_executor` for synchronous
  activities across both queues.

### Client

`temporal/client.py`:

- `start_module_generation(tool_info, *, workflow_id=None, ...)` — submits the workflow, returns a
  handle without waiting. `workflow_id` defaults to a random suffix (`module-generation-<tool>-
  <hex>`) for CLI use, but the Django UI passes an explicit `workflow_id = <module directory
  name>` — see Design Decisions.
- `run_module_generation(tool_info, **kwargs)` — starts and awaits the result; the CLI's entry
  point for the default (non-`--legacy`) path.
- `get_workflow_state(workflow_id, client=None)` — the Django UI's replacement for tailing
  `status.json`: describes the workflow, then dispatches to the `progress()` query (if still
  running or in a non-terminal failure state) or `handle.result()` (if completed), returning
  `None` for an unknown ID. Always attempts the `progress()` query regardless of execution status,
  since Temporal answers queries against closed workflows too — this was a real bug (found live)
  in an earlier version that only queried while `RUNNING`, silently going blank the moment a run
  finished.
- `connect()` — a public alias so callers that need several calls (e.g. the Django module listing,
  which queries every visible module directory's workflow state concurrently via `asyncio.gather`)
  can share one `Client` instead of reconnecting per call.
- `decide_upload(workflow_id, approve, client=None)` — signals the upload-approval gate
  (`approve_upload`/`reject_upload`). Returns `False` (rather than raising) if the workflow ID
  doesn't exist, e.g. it already completed — both the CLI (`--approve-upload`/`--reject-upload`)
  and the Django `/upload-decision/<module>/` view surface that as a clear "already finished"
  message rather than a stack trace.

### Retry policies

Every effects activity funnels through a single helper (`ModuleGenerationWorkflow._act`), which
now applies a **bounded** retry policy by default — `maximum_attempts=4`, exponential backoff
capped at 30s — instead of Temporal's unbounded-by-default policy. The two long-running,
heartbeat-wrapped activities (Docker build, downloads) use a separate policy with only
`maximum_attempts=2`, since each attempt can itself take minutes. This governs genuine
activity-*execution* exceptions only — `agents/effects.py` functions return structured result
objects for *expected* failures (a bad Dockerfile fails to build, a linter rejects a wrapper),
they never raise for those, so the pipeline's own `MAX_ARTIFACT_LOOPS`/`MAX_ESCALATIONS`
retry-and-escalate logic (workflow-level, not Temporal-level) is entirely undisturbed by this
policy.

### Testing infrastructure

A dedicated set of small, purpose-built test workflows (`temporal/_test_fixtures.py`) validates
Temporal *mechanisms* directly against a real embedded `WorkflowEnvironment`, independent of the
full LLM pipeline (which can't be driven deterministically — `TemporalAgent.override(model=...)`
is explicitly disallowed inside a running workflow): a minimal agent-plus-activity workflow for
crash/resume and replay-determinism tests; a configurable-timeout workflow that pins the
heartbeat bug and its fix; a deliberately-always-failing activity that proves a `RetryPolicy`'s
`maximum_attempts` is actually honored; a heartbeat-wrapped activity that proves real heartbeats
keep a long activity alive under a short `heartbeat_timeout`; a minimal signal-and-
`wait_condition` workflow that proves a client signal sent after a workflow starts actually
reaches and unblocks it, for the upload-approval gate. Crash-resume is tested by starting a
workflow, running it partway on one worker, killing that worker mid-execution
(`task.cancel()`, not a graceful shutdown), and confirming a second, freshly-started worker on the
same task queue completes it correctly — proof that Temporal, not application code, owns recovery.

---

## Design decisions & trade-offs

**Option B: keep the legacy path as a tested reference, not a stepping-stone to delete.** The
plan considered three options for `ModuleAgent` once Temporal became the default: delete it
(simplest, but destroys the only unit-testable coverage of the trickiest coordination logic —
`TemporalAgent.override(model=...)` can't substitute a test model inside a running workflow, so
the production workflow can't be driven end-to-end deterministically the way `ModuleAgent` can);
extract the shared logic into a framework-agnostic module first (the "right" long-term answer, but
a substantial refactor); or keep both, accepting duplication as a documented, temporary cost. Given
the two implementations' coordination logic was ~600 lines each with a near-1:1 method
correspondence, and given the real risk of drift was demonstrated concretely (the same wrapper-
filename bug had to be hand-fixed in both files in one session), the deliberate choice was to keep
both **and put the de-duplication on the roadmap explicitly**, rather than force a large refactor
under the same timeline as the durability cutover. This is called out below as unfinished work,
not hidden.

**`workflow_id = module directory name`.** Rather than persisting a separate mapping from module
directory to workflow ID (reintroducing exactly the kind of "second source of truth" the whole
project was trying to eliminate), the Django UI sets the workflow ID explicitly to the module
directory name at submission time. Every subsequent view — status polling, console log, file
listing — reconstructs the correct workflow handle directly from the URL path parameter it already
has, with nothing to keep in sync and nothing that can drift.

**Effects extraction stayed Temporal-agnostic on purpose.** `agents/effects.py`'s functions don't
know `activity.heartbeat()` exists. When real heartbeating was added in Phase 5, the alternative
— threading a heartbeat callback down into every effect function — was rejected because it would
have coupled the `--legacy` path (which never heartbeats) to a Temporal-shaped protocol for no
benefit. Instead, `temporal/activities.py::_wrap_with_heartbeat` runs the unmodified, blocking
effect function in a background thread and heartbeats from the *activity's own* thread while
polling it — `activity.heartbeat()` must be called from the activity's execution context, not an
arbitrary worker thread, so this is not a trivial background-thread pattern; it specifically keeps
the heartbeat call on the right thread while letting the actual work block elsewhere.

**Coarse-grained activities where the alternative was riskier, fine-grained everywhere else.**
Most side effects are small, single-purpose activities. The dockerfile runtime-command builder is
the deliberate exception: its three-strategy fallback logic (manifest introspection → wrapper
introspection → placeholder substitution), including an internal LLM call, runs as one coarse
activity rather than being decomposed into workflow code by hand. Reimplementing intricate,
rarely-changed business logic as workflow code risks transcription bugs for no durability benefit
proportional to the risk; a coarse activity wrapping a coherent unit of logic — even one with its
own non-deterministic LLM call inside it — is a standard, legitimate Temporal pattern when that
unit either succeeds or fails as a whole.

**Extending `TemporalRunContext` for a payload-size-conscious cross-cutting concern.** A
deterministic guard against agents re-invoking a "terminal" tool after it already returned its
final answer needed to know which tools had already succeeded in the current run. In-process, that
was a trivial scan of `RunContext.messages`. Under Temporal, tools run inside activities whose
`RunContext` is reconstructed from a serialized subset that excludes `messages` by default (per
the gotcha named at design time — see above). The naive fix, extending the serialization to
include the full message history, would have reintroduced the exact payload-bloat problem the
project had just spent effort eliminating elsewhere. The actual fix: a `TemporalRunContext`
subclass (`_GuardedRunContext`) whose `serialize_run_context` computes and ships only the one
small derived fact the guard needs — a list of tool names that already returned successfully —
never the messages themselves. This is the kind of decision that only becomes visible once you're
inside the SDK's serialization boundary, not from the framework's high-level docs.

**Payload size and workflow history are treated as a real operational constraint, learned the
hard way.** During live testing, `PayloadSizeWarning`s (up to 1.3MB on a single query response)
were observed repeatedly, and one long-lived, repeatedly-retried workflow was outright
**terminated by the server for exceeding its history size limit** — not a hypothetical risk from
the design doc, an actual failure. The root cause: the pipeline's status object embedded large
free-text blobs (a research report, a full generation plan) that were *already* being written to
disk, and re-transmitted them in full on every `progress()` poll and every workflow payload
regardless. The fix — stop serializing the large text fields, since nothing downstream actually
read them from that object rather than from disk — cut the recurring payload size substantially,
verified by a live run producing zero size warnings afterward.

**The shared-filesystem assumption was deliberately not solved with distributed infrastructure.**
Workers write module output to their own local disk; the CLI and Django UI assume they can see the
same path. A shared network volume or object-storage backend would remove that assumption
entirely, but building it now, for a single-process deployment with no current multi-host need,
would be solving a problem that doesn't exist yet. Instead, the constraint is enforced with a
loud, explicit check (the CLI and web UI both verify the reported output directory is actually
visible from where they're running, and say exactly why if it isn't) rather than either building
unneeded infrastructure or leaving a silent failure mode in place. This is flagged as a real,
open architectural question for the next phase of scale, not a solved one.

**Signal handlers must tolerate arriving when nothing is pending.** `approve_upload`/
`reject_upload` are guarded by an explicit `if self._awaiting_upload_approval:` check before they
touch any state. Temporal signals are asynchronous and fire-and-forget from the sender's side — a
client has no way to confirm the workflow has actually reached its `wait_condition` yet, and a
double-click, a retried HTTP request, or a signal aimed at a workflow that already finished are all
realistic. Making the handler a no-op in those cases (rather than, say, raising or silently
corrupting `_upload_decision` for a future, unrelated wait) was a deliberate design choice, not an
accident of the happy-path implementation — and it's exactly the kind of thing that's easy to get
wrong quietly, since the happy path works either way in casual testing.

**The approval wait has no timeout of its own, which has a real consequence: the workflow's
overall `execution_timeout` becomes the actual bound on how long a human can take.** The client's
default (2 hours) is almost certainly too short for "wait for someone to notice and click a
button," so both the CLI and the Django UI pass a much longer override (7 days) specifically when
`require_upload_approval=True`. This is a small thing that would be an easy production surprise if
missed — a workflow silently timing out hours into an approval wait, with the timeout's true cause
several layers removed from the approval feature itself.

**Concurrency validation found a real, previously-latent bug, not just confirmed the absence of
one.** `effects.make_module_dir`'s no-explicit-`module_dir` path named directories from
`tool_name` + a second-granularity timestamp and created them with `mkdir(exist_ok=True)` — silent
success even when the directory already existed. Two concurrent runs of the same tool starting in
the same wall-clock second (a real scenario, not a contrived one: the Django UI's own
"same-second-collision" path derives its workflow ID from that same directory name) would silently
share one directory, each unknowingly overwriting the other's artifacts. This was invisible in
every test up to this point because nothing had run two workflows at once. It was caught by
deliberately writing a test built to trigger it — two independent Temporal client connections, each
backing its own `Worker` polling the same task queue (the SDK-level equivalent of two separate
worker processes), racing six workflow instances against one deliberately-identical directory name
— and confirmed as a genuine regression by reverting the fix and watching the same test fail
(six workflows, one directory, silently). The fix makes directory creation atomic and
collision-resistant (`Path.mkdir(exist_ok=False)`, retried with a bumped numeric suffix on
`FileExistsError`) at the one place both drivers fall through to when no directory is pre-assigned;
the Django view, which pre-assigns a directory name synchronously before submission, was changed to
call the same fixed primitive instead of duplicating the naming logic inline. The module-level
Brave Search rate limiter (a `threading.Lock` + last-call timestamp shared by every research call in
one worker process) was separately confirmed correct under real concurrent access — a dedicated
test drives six threads through it simultaneously and asserts every call is actually serialized with
the minimum gap enforced, not silently dropped or double-fired. It has one known, accepted
limitation worth stating plainly: the lock is per-*process*, so two separate worker processes
sharing one Brave API key are not jointly rate-limited against each other — correct for today's
single-worker-process deployment, a real constraint the moment a second worker process is added
against the same key.

*Scope note:* verification here is mechanism-level (real Temporal SDK dispatch, real filesystem, real
thread concurrency) rather than a full concurrent run of the real LLM-backed pipeline across genuinely
separate OS worker processes. That fuller run is possible but was judged lower value for the risk —
the local model's already-documented tool-call-looping flake (see the Phase 4 parity gate above) makes
multiple simultaneous full runs slow and failure-prone in a way that's orthogonal to what's actually
being tested here, and the SDK-level two-connection setup exercises the exact same dispatch/locking
code path a second real process would.

**Observability didn't need a new dashboard — it needed an existing one to actually work.** The
original Phase 5 sketch for Workstream F assumed building custom dashboards/alerts. Revisiting it:
Temporal's own Web UI (bundled with `temporal server start-dev`, zero extra infrastructure) already
surfaces workflow/activity state, retries, and heartbeats per run today, and Logfire was already
wired in for the AI-agent side (`configure_telemetry()`, composing with Temporal's own tracing per
`CONSIDERATIONS.md`). Building a second, bespoke dashboard on top of two that already exist, for a
single-deployment project with no on-call rotation to page, would have been solving a problem that
doesn't exist yet — the same YAGNI judgment already applied to the shared-filesystem question (A3)
and the Brave rate limiter (E). What *was* a real, worth-fixing gap: `configure_telemetry()`
hardcoded `send_to_logfire=False`, so a correctly-set `LOGFIRE_TOKEN` silently did nothing — the
real Logfire cloud dashboard was unreachable no matter how it was configured. Fixed to
`send_to_logfire='if-token-present'` (only sends when a token is actually set, never blocks or
errors without one). A second, smaller gap found in the same pass: the local-OTel-collector
reachability pre-check was hardcoded to `localhost:4318`, ignoring the
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` variable `.env.example` already documented — so pointing it
anywhere else silently disabled telemetry entirely, even though `logfire.configure()` itself already
knows how to honor that variable. Both are now unit-tested (`tests/test_telemetry_config.py`)
without any real network call. The other concrete, low-cost piece of the original F sketch — making
the web UI's console modal explain that fine-grained per-tool-call output lives in the worker's own
process log, not in the phase-level progress it displays — was implemented as written (the plan's own
"cheapest option first" recommendation).

---

## Benefits realized

- **Crash recovery is no longer application code.** A worker can be killed mid-workflow and a
  freshly started one resumes correctly — proven with a live kill-and-resume test, not asserted.
  No `--resume` flag, no reload-and-reconstruct logic, no risk of the reconstruction being subtly
  wrong.
- **A structured, queryable progress interface replaced a file to `cat` and reason about by
  hand.** The `progress()` query is typed, versioned by the workflow's own code, and answerable
  whether the run is in flight or finished.
- **Retry semantics finally distinguish "worth retrying" from "will fail identically forever."**
  Previously all retries were one undifferentiated `MAX_ARTIFACT_LOOPS` loop; now Temporal-level
  activity retries (bounded, backing off, reserved for genuine transient infra failures) sit
  underneath the pipeline's own escalation logic (reserved for LLM-output quality failures),
  instead of the two being conflated.
- **Real heartbeat-based liveness detection**, where before there was only a generous fixed
  timeout and hope.
- **A durable human-in-the-loop approval gate that the hand-rolled `status.json` system could
  never have supported.** Pausing a run indefinitely — potentially for days, across restarts of
  everything except the Temporal server itself — until a human approves or rejects a GenePattern
  upload, with zero custom state-persistence code, is close to a textbook use case for durable
  execution. The whole feature is two signal handlers, one `wait_condition`, and a query field.
- **The forced separation of deterministic coordination from impure side effects — a Temporal
  requirement — measurably improved the codebase independent of any Temporal benefit.**
  `agents/effects.py`'s extraction (Phase 2) made every side effect independently testable and
  reusable; it's the reason the `--legacy` and Temporal paths can share identical business logic
  today at all.
- **The migration discipline itself is reusable.** Shadow mode before cutover, a live parity gate
  before removing anything, and small purpose-built test workflows that exercise Temporal
  mechanisms directly rather than only through the full application — none of that is specific to
  this pipeline.
- **Concurrent execution is now a demonstrated property, not an assumption riding on Temporal's
  reputation.** Every run verified earlier in this document was one workflow at a time; a dedicated
  concurrency pass (temporal/PHASE5.md Workstream E) proved multiple simultaneous workflows against
  multiple workers don't interfere — and, in the process, found and fixed a real directory-collision
  bug that had been latent since before Temporal was introduced. This is the result that actually
  closes the loop on choosing Temporal over the lighter-weight DBOS alternative considered at the
  outset: the payoff was durability *and* safe concurrency, and only the second half had gone
  unverified until now.
- **The observability story is now actually reachable, not just wired in.** Logfire's AI-agent
  tracing and Temporal's own workflow/activity Web UI were always meant to compose (a design
  decision made at Phase 0/`CONSIDERATIONS.md`), but a latent bug meant the Logfire half could never
  actually reach a real dashboard regardless of configuration. Fixed and unit-tested; both halves of
  the observability story are real now, not aspirational.

---

## What's not yet implemented

Phase 5 ("production hardening") is nearly complete. Five of its workstreams — bounded/
differentiated retry policies and real activity heartbeating; a deterministic guard against a
recurring LLM tool-call-looping failure mode; the human-in-the-loop GenePattern-upload approval
gate; concurrent multi-run validation; and observability — are done and described above. The
remainder is scoped and planned (`temporal/PHASE5.md`) but not yet built:

- **De-duplicate the orchestrator (spiked, tabled).** `agents/module.py` (`ModuleAgent`, the
  `--legacy` driver) and `temporal/workflow.py` (`ModuleGenerationWorkflow`) currently implement the
  same coordination logic twice, with a near-1:1 method correspondence — a deliberate, documented
  trade-off (Option B, above), not an oversight. Both files were read in full and compared
  method-by-method before committing to a plan (per the standing rule: spike first on the largest
  remaining item). That comparison confirmed the refactor is real but non-mechanical — the two
  drivers disagree on data representation (live `ModulePlan`/`Path` objects vs. JSON-serializable
  dicts/strings) and on I/O shape (direct synchronous calls vs. awaited `workflow.execute_activity`
  with per-call retry/heartbeat tuning) — and it surfaced one concrete, independent bug along the
  way: neither driver had a real file-existence primitive (`--legacy` called `Path.exists()`
  directly; Temporal faked it by reading the file and checking for a non-`None` result). That bug
  was fixed immediately (`effects.file_exists`, now a 14th activity, listed above); the broader
  refactor itself was intentionally not started — a scope call to bank the concrete fix now rather
  than commit to the larger, harder-to-bound effort. The plan, if resumed: extract the pure,
  I/O-free decision logic (next-artifact selection, escalation-queue reordering, prompt/deps
  construction, retry counting) into a shared module both drivers call, expressing I/O through an
  interface each implements differently (direct calls for `--legacy`,
  `workflow.execute_activity`/`TemporalAgent.run()` for Temporal). **Benefit:** one implementation
  of the trickiest logic in the system instead of two that can silently drift (as already happened
  once); the ability to finally retire `--legacy` safely, if desired, without losing test coverage,
  since the coordination tests move onto the shared module.
- **Full console-log parity between the web UI and a terminal (a known, deliberately-accepted
  gap).** The web UI's live console view shows the workflow's own phase-level status lines, not the
  fine-grained per-tool-call output emitted inside agent tool functions — that output runs inside an
  *activity* and never crosses back into the workflow's queryable state. The cheapest fix (a
  permanent note in the console modal pointing at the worker's own log / the Temporal Web UI's
  activity view for that level of detail) is done (Workstream F, above); actually *piping* that
  output back through the workflow's own log buffer is possible but was deliberately not done, since
  it would reintroduce the same payload-growth problem the project just spent effort eliminating
  (Workstream A). **Benefit if revisited:** a genuinely equivalent live-debugging experience to
  watching the CLI's own terminal, for web UI users, without leaving the browser.
- **Bespoke dashboards/alerting.** Deliberately not built (see Workstream F's writeup above for the
  reasoning) — Temporal's own Web UI and Logfire's own dashboard already cover this project's actual
  current need. **Benefit if revisited:** worth reconsidering only if this ever runs as an operated
  service with an on-call rotation to page, not before.

This document will be revised as these land.

---

## Appendix: recommended screenshots for this presentation

Notes for assembling the actual submission — not part of the narrative for a reviewer, strip before
sending if that matters. Two tools are already wired up (Workstream F above); both are worth
capturing, for different reasons.

**Temporal's own Web UI** (`http://localhost:8233` when running `temporal server start-dev` locally
— no extra setup). Lead with this one for a Temporal-specific audience: it's the most direct,
legible evidence of the mechanisms this document narrates in prose.
- The **workflow list**, ideally with a few completed/running/failed runs visible at once.
- One execution's **event history / timeline**, specifically one that hit a retry (a real transient
  failure, or force one — e.g. temporarily point `OLLAMA_BASE_URL` at a dead host for one run) so the
  `RetryPolicy` backoff is visibly recorded, not just claimed.
- The **heartbeat-wrapped Docker build activity** mid-run, showing live heartbeats — pairs well with
  the "a heartbeat bug silently killed a 20-minute build at 30 seconds" story above.
- A run submitted with `--require-upload-approval` (or the Django checkbox), paused and showing
  `awaiting_upload_approval` — then the `approve_upload`/`reject_upload` signal landing in the
  timeline after you send it. This is the single best screenshot for "durable execution enabled a
  feature that would have been hard to build otherwise."
- The **Workers tab**, showing both task queues (`module-generation`, `docker-builds`) with active
  pollers — direct evidence for the coarse-vs-fine activity / separate-queue design decision above.

**Logfire** (`https://logfire.pydantic.dev/`, once `LOGFIRE_TOKEN` is set — see the Observability
section of `README.md`). The complementary half of the story: Temporal's UI shows durable execution
*between* activities; Logfire shows what happens *inside* one activity's LLM call.
- The **trace waterfall for one agent run** (e.g. the wrapper-generation agent), showing the nested
  tool calls (`create_wrapper`, `validate_wrapper`) and, ideally, one `ModelRetry` bounce visible in
  the trace — direct evidence for the `guard_single_call`/error-classification stories above.
- **Token usage / cost per run**, if the project's usage-limit design (`MAX_AGENT_REQUESTS`) is worth
  illustrating.

**How to get a run to screenshot**: `temporal server start-dev` (starts both the server and its Web
UI), `uv run python -m temporal.worker` in a second terminal (with `LOGFIRE_TOKEN` set in `.env` if
using Logfire), then either `uv run python generate-module.py --name samtools --version 1.19
--language c ...` or the Django UI (`uv run --extra app python app/manage.py runserver`). A
wrapper-only run (`--skip-manifest --skip-paramgroups --skip-gpunit --skip-documentation
--skip-dockerfile`, or the equivalent skip checkboxes in the UI) is enough to populate both
dashboards without waiting on a full pipeline.
