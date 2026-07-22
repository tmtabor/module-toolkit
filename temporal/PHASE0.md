# Phase 0 — Modern, Reproducible Baseline (uv + latest Pydantic AI)

Goal: replace the drifted, partially-installable environment with a **locked, reproducible**
one managed by `uv`, on the latest compatible Pydantic AI, with `pydantic-ai-skills` present and
the full non-live test suite green. This precedes and de-risks
[PHASE1.md](./PHASE1.md) (async conversion), which assumes a green starting point.

Runs before PHASE1 Step 0 — in fact it **replaces** that step's ad-hoc `pip install`.

## Discovered facts (this checkout, at time of writing)

- `uv 0.11.21` is already installed; Python is 3.13.9. No `pyproject.toml`, `setup.py`, or lock
  file exists — deps live in `requirements.txt` (root) and `app/requirements.txt` (Django UI).
- Installed `pydantic-ai` is **1.73.0**, *below* the root pin's own floor (`>=1.77.0`).
- **Latest `pydantic-ai` is a 2.x release** — the root pin `<2.0.0` excludes it, so "update to
  latest" is a **major-version migration**, not a minor bump.
- **The current pins look internally unsatisfiable**, which explains why the env is broken:
  `requirements.txt` pins `pydantic-ai-skills>=0.1.0,<1.0.0`, but the only published
  `pydantic-ai-skills` release is **1.2.0** (excluded by `<1.0.0`). `pydantic-ai-skills` also
  depends on `pydantic-ai-slim`, which will constrain how far `pydantic-ai` can move.

> **Version numbers above are informational.** Do not hard-code them. `uv`'s resolver is the
> source of truth — let it compute the compatible set (Step 0.2) and record the exact pins it
> chooses in `uv.lock`.

## The one decision this phase forces (resolve at Step 0.2)

`pydantic-ai-skills` (used via `from pydantic_ai_skills import SkillsToolset` in
`wrapper/agent.py` and the manifest agent) constrains the usable `pydantic-ai` version. If the
newest `pydantic-ai` and `pydantic-ai-skills` cannot co-resolve:

- **Option A (recommended default): newest co-compatible set.** Take the highest `pydantic-ai`
  that `pydantic-ai-skills` still supports. "Latest" is interpreted as "latest that keeps skills
  working." Lowest risk; keeps the skills-driven agents intact.
- **Option B: go to newest `pydantic-ai` (2.x) and drop/replace skills.** Only if a compelling
  2.x feature is needed. Requires reworking the two agents that load `SkillsToolset`. Larger blast
  radius — treat as its own mini-project, not part of Phase 0.

**Surface this to the maintainer and pick A unless told otherwise.** The rest of the plan assumes A.

---

## Step 0.1 — Initialize uv project management

- [ ] Create a `pyproject.toml` with a PEP 621 `[project]` table. Set `requires-python = ">=3.10"`
      (the project's stated floor) and pin the working interpreter with a `.python-version` file
      (3.13, matching the current interpreter).
- [ ] Migrate **root `requirements.txt`** into `[project.dependencies]` — carry the runtime deps
      (PyYAML, requests, beautifulsoup4, PyPDF2, `mcp[cli]`, `pydantic-ai`, `pydantic-ai-skills`,
      python-dotenv, logfire). Drop the `pytest*` lines from runtime — they move to a dev group.
- [ ] Move test tooling into a PEP 735 **`[dependency-groups]` `dev`** group: `pytest`,
      `pytest-asyncio` (required for PHASE1's async tests). uv installs dev groups by default on
      `uv sync`.
- [ ] Fold **`app/requirements.txt`** (Django, python-dotenv) into an optional extra
      `[project.optional-dependencies] app = ["Django>=4.2", ...]` so the web UI stays installable
      but isn't forced on core/CLI users. Keep `app/requirements.txt` as a thin pointer or delete
      it and note the change in `app/README.md`.
- [ ] Do **not** hand-write version pins for `pydantic-ai`/`pydantic-ai-skills` beyond a floor;
      let Step 0.2 resolve them, then commit the resolved `uv.lock`.

Result: `pyproject.toml` + `.python-version`; both `requirements.txt` files superseded.

---

## Step 0.2 — Resolve to the latest compatible Pydantic AI (+ skills)

- [ ] Ask uv to take `pydantic-ai` and `pydantic-ai-skills` to their newest co-resolvable
      versions:

      ```bash
      uv add "pydantic-ai" "pydantic-ai-skills"     # let the resolver pick compatible latest
      uv lock                                        # writes uv.lock (source of truth)
      uv sync --group dev                            # materialize the venv incl. test tools
      ```

- [ ] Read back the versions uv actually chose (`uv pip list | grep pydantic-ai`). If it could
      **not** take `pydantic-ai` to the newest major because `pydantic-ai-skills` holds it back,
      that is the **Option A vs B decision** above — stop and confirm direction before forcing it.
- [ ] Record the chosen `pydantic-ai` / `pydantic-ai-skills` / `pydantic-ai-slim` versions in the
      Phase 0 PR description so the major/minor delta is explicit for review.

---

## Step 0.3 — API-compatibility migration (only if the major version changed)

If Step 0.2 moved `pydantic-ai` across a major boundary, audit the import + construction surface
the codebase relies on and fix breakages. Targets (from a grep of the tree):

- [ ] Imports: `from pydantic_ai import Agent, RunContext`;
      `from pydantic_ai.models.openai import OpenAIChatModel`;
      `from pydantic_ai.providers.ollama import OllamaProvider`;
      `from pydantic_ai.models.test import TestModel`;
      `from pydantic_ai_skills import SkillsToolset`. Confirm each path still exists / update it.
- [ ] Agent construction kwargs in use: `instructions=`, `output_type=`, `deps_type=`,
      `toolsets=`, `retries=`, `capabilities=` (`agents/researcher.py`), plus the `@agent.tool`,
      `@agent.tool_plain`, and `@agent.instructions` decorators. Verify signatures against the new
      release's changelog.
- [ ] `configured_llm_model()` in `agents/models.py` — the `OpenAIChatModel` + `OllamaProvider`
      wiring for the `ollama:` path is the most fragile spot across versions; smoke it explicitly.
- [ ] Consult the Pydantic AI changelog / migration guide for the specific jump; apply the
      documented codemods. Keep this commit separate from the packaging change for clean review.

If the version did **not** cross a major (Option A landed on same-major latest), this step is a
quick import smoke and likely a no-op.

---

## Step 0.4 — Ensure current tests pass (green baseline)

- [ ] Confirm `pytest-asyncio` is active: no `Unknown config option: asyncio_mode` warning
      (the existing `pytest.ini` already sets `asyncio_mode = auto`).
- [ ] Add `pythonpath = .` to `pytest.ini`. `uv run pytest` invokes the console script (not
      `python -m pytest`), so it does not add the CWD to `sys.path`; without this, `import agents`
      fails at conftest load.
- [ ] Run the **canonical** suite through uv and get it green (this is what `pytest.ini`'s
      `testpaths = tests` targets):

      ```bash
      uv run pytest -m "not live"        # agents/orchestrator suite (tests/)
      ```

- [ ] Specifically verify the two files that previously failed at **collection** now import and
      pass: `tests/test_artifact_agents.py` and `tests/test_module_orchestrator.py` (both pulled
      in `pydantic_ai_skills`, now installed).
- [ ] **Do not batch the per-artifact linter dirs under pytest.** The `<artifact>/tests/*.py`
      files are the linter's rule engine — each defines `run_test(lines)`, not a pytest `test_*`
      function — share basenames across dirs (e.g. `test_file_validation.py`) and have no package
      `__init__.py`, so collecting all six at once raises import-file-mismatch errors (running one
      dir alone just collects 0 items). Verify the linters **functionally** instead:

      ```bash
      uv run python -m manifest.linter manifest/examples/valid/manifest
      uv run python -m wrapper.linter  wrapper/examples/valid/sample_python_wrapper.py
      ```

      The linters are pure-Python and never import `pydantic-ai`, so the upgrade cannot affect
      them beyond import resolution.
- [ ] Fix any failures attributable to the version bump **here**, so PHASE1 starts from a known
      green tree and later async regressions are unambiguous.

### 2.x migration fixes actually required (record for the PR)

- `RunUsage` is now an **attribute, not a call**: `result.usage()` → `result.usage`
  (production `agents/status.py`, plus `test_researcher_agent`, `test_planner_agent`,
  `test_artifact_agents`, `test_websearch_live`, and the `.usage` mocks in
  `test_module_orchestrator`).
- `pydantic-ai-skills` 1.2.0 adds a `load_skill` tool to the wrapper/manifest agents. `TestModel`
  calls every tool with synthetic args, so it hits `load_skill('a')` → `ModelRetry`. Fix in the
  tests with `TestModel(call_tools=[])`, and relax `test_model_receives_registered_tools` from
  `==` to a subset check (the skills toolset legitimately adds tools beyond the agent's own).

---

## Step 0.5 — Wire uv into the workflow & docs

- [ ] Update developer commands to the uv equivalents: `uv sync`, `uv run pytest`,
      `uv run python generate-module.py ...`, `uv run python app/manage.py runserver`.
- [ ] Update `CLAUDE.md` (Commands section) and `README.md` (Quick Start / install) to show the
      uv flow; note the `app` extra (`uv sync --extra app`) for the Django UI.
- [ ] Commit `pyproject.toml`, `uv.lock`, `.python-version`. Decide whether to keep the old
      `requirements.txt` files as thin shims (`-e .`) for external users or delete them (recommend
      delete + a README note, since uv.lock is now authoritative).

---

## Acceptance criteria

1. `pyproject.toml` + `uv.lock` + `.python-version` exist and are committed; `uv sync` reproduces
   the environment from scratch.
2. `uv pip list` shows `pydantic-ai` at the newest version compatible with `pydantic-ai-skills`
   (Option A), and `pydantic-ai-skills` is installed and importable.
3. `uv run pytest -m "not live"` is green (the canonical `tests/` suite), including the two files
   that previously errored at collection; the linters run functionally against their examples.
4. No source behaviour change beyond what the Pydantic AI upgrade required (Step 0.3 diffs are
   migration-only).
5. `temporal/` docs untouched by runtime code; no Temporal dependency added (that is Phase 3).

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| "Latest" pydantic-ai (2.x) can't co-resolve with `pydantic-ai-skills` | Explicit **Option A/B decision** in Step 0.2; default to newest co-compatible (A) |
| Current `requirements.txt` pins are unsatisfiable (skills `<1.0.0` vs. only-published 1.2.0) | uv resolver replaces hand pins; `uv.lock` becomes source of truth |
| Major-version API breakage in agents | Isolated Step 0.3 audit of the exact import/construction surface, done as its own commit against the changelog |
| Hidden Python-version sensitivity (env is 3.13, floor is 3.10) | Pin `.python-version`; `requires-python=">=3.10"`; optionally CI-matrix later |
| uv dev group not installed → async tests silently skipped | `uv sync --group dev`; verify no `asyncio_mode` warning in Step 0.4 |
| Two requirements files drift again | Consolidate into `pyproject.toml` (core deps + `app` extra + `dev` group); remove/shim the old files |

## Suggested commit sequence

1. `build: adopt uv — pyproject.toml, .python-version, migrate requirements` (Steps 0.1)
2. `build: resolve latest compatible pydantic-ai + skills, add uv.lock` (Step 0.2)
3. `refactor: migrate to pydantic-ai <new major> API` (Step 0.3 — only if major changed)
4. `test: green baseline under uv` (Step 0.4)
5. `docs: switch commands to uv; note app extra` (Step 0.5)

Once this lands, PHASE1 begins from a locked, green tree, and its "Step 0" collapses to a single
`uv sync` sanity check.
