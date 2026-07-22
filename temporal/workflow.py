"""
ModuleGenerationWorkflow -- Temporal port of agents.module.ModuleAgent's
coordination logic (research -> planning -> per-artifact generation loop with
cross-artifact escalation -> zip/install).

Ported, not rewritten: the retry counting, the classify_error/should_escalate
escalation-queue reordering, and the prompt/deps construction are copied from
agents/module.py essentially verbatim, since that logic is already pure and
deterministic (Phase 1/2). What changed at each call site:
  - `agent.run(prompt, deps=...)` -> the `temporal.agents` TemporalAgent version.
  - `effects.some_function(...)` direct calls -> `workflow.execute_activity(...)`.
  - `datetime.now()` (module-dir timestamp) -> `workflow.now()`.
  - Console/`Logger` output -> `WorkflowLogger` (replay-safe).

Deliberately NOT ported: `--resume` (removed from the CLI entirely in
temporal/PHASE4.md 4.5; this workflow only ever handles fresh runs) and
`print_final_report` (runs client-side after the workflow returns its result
-- see temporal/client.py).

This module must stay side-effect-free at import time: Temporal's workflow
sandbox re-imports it (and everything it imports) to validate determinism,
so nothing here may open a socket, hit the filesystem, or start an event loop
at parse time (see temporal/PHASE3.md Step 3.0's spike finding).
"""
import os
from datetime import timedelta
from pathlib import PurePosixPath as _PurePosixPath
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# These imports transitively reach agents.effects / agents.example_data,
# which import `requests` at module level; requests' http.client usage does
# not sandbox cleanly (Temporal's workflow sandbox re-imports everything a
# workflow module touches to validate determinism, even though these modules
# are only ever *referenced* here -- e.g. activity function objects passed to
# workflow.execute_activity -- never executed inside the sandbox itself).
# imports_passed_through() tells the sandbox to import them normally instead
# of re-validating/re-implementing them.
with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
    from pydantic_ai.usage import UsageLimits

    from agents.config import MAX_AGENT_REQUESTS, MAX_ARTIFACT_LOOPS, MAX_ESCALATIONS
    from agents.error_classifier import (
        classify_error, should_escalate, get_upstream_dependencies, _sanitize_error_line, RootCause,
    )
    from agents.example_data import ExampleDataItem
    from agents.models import ArtifactModel, ArtifactDeps
    from agents.status import ArtifactResult, ModuleGenerationStatus
    from gpunit.linter import normalize_param_type
    from manifest.models import ManifestModel
    from paramgroups.models import ParamgroupsModel
    from wrapper.agent import select_wrapper_language

    from temporal import activities
    from temporal import agents as temporal_agents
    from temporal.logger import WorkflowLogger

# Same list as agents/module.py -- single definition, copied rather than
# imported to avoid pulling agents.module (which constructs plain, non-Temporal
# agent objects at import time) into the workflow sandbox.
ERROR_INDICATORS = [
    'E: Unable to locate package', 'E: Package',
    'ERROR:', 'error:', 'No such file or directory',
    'ModuleNotFoundError', 'ImportError', 'command not found',
    'exit code:', 'executor failed', 'FAILED',
    'the following arguments are required:', 'usage:',
    'unrecognized arguments', 'TypeError:',
    'has no matching flag',
    'unexpected end of statement', 'failed to process',
    'file not found in build context',
    'file does not exist',
    'COPY failed:',
    'failed to solve:',
    'USER ERROR', 'A USER ERROR has occurred',
    'Exception in thread', 'java.lang.', 'java.io.',
    'htsjdk.', 'org.broadinstitute.',
]

# Same shape as ModuleAgent.artifact_agents, but pointing at the TemporalAgent
# wrappers and keyed the same way so artifact-name-driven logic is unchanged.
ARTIFACT_CONFIG = {
    'wrapper': {
        'agent': temporal_agents.wrapper_agent,
        'model': ArtifactModel,
        'filename': 'wrapper.py',
        'validate_tool': 'validate_wrapper',
        'create_method': 'create_wrapper',
        'formatter': lambda m: m.code,
    },
    'manifest': {
        'agent': temporal_agents.manifest_agent,
        'model': ManifestModel,
        'filename': 'manifest',
        'validate_tool': 'validate_manifest',
        'create_method': 'create_manifest',
        'formatter': lambda m: m.to_manifest_string(),
    },
    'paramgroups': {
        'agent': temporal_agents.paramgroups_agent,
        'model': ParamgroupsModel,
        'filename': 'paramgroups.json',
        'validate_tool': 'validate_paramgroups',
        'create_method': 'create_paramgroups',
        'formatter': lambda m: m.to_json_string(),
    },
    'gpunit': {
        'agent': temporal_agents.gpunit_agent,
        'model': ArtifactModel,
        'filename': 'test.yml',
        'validate_tool': 'validate_gpunit',
        'create_method': 'create_gpunit',
        'formatter': lambda m: m.code,
    },
    'documentation': {
        'agent': temporal_agents.documentation_agent,
        'model': ArtifactModel,
        'filename': 'README.md',
        'validate_tool': 'validate_documentation',
        'create_method': 'create_documentation',
        'formatter': lambda m: m.code,
    },
    'dockerfile': {
        'agent': temporal_agents.dockerfile_agent,
        'model': ArtifactModel,
        'filename': 'Dockerfile',
        'validate_tool': 'validate_dockerfile',
        'create_method': 'create_dockerfile',
        'formatter': lambda m: m.code,
    },
}

# Activity start_to_close timeouts. These are read once at import time (a
# module-level constant, not a per-run env read inside workflow code, which
# would be non-deterministic) and are recorded in workflow history when the
# activity is scheduled, so replay is unaffected by later env changes.
#
# Most activities here set no `heartbeat_timeout`: Temporal fails any activity
# that declares one but never calls activity.heartbeat() -- the previous
# `_DOCKER_BUILD_HEARTBEAT` killed the docker build at 30s regardless of
# start_to_close this way (verified; see temporal/PHASE3.5.md H0), because
# agents.effects functions are Temporal-agnostic by design (Phase 2) and don't
# heartbeat themselves. build_and_test_image and download_one are the
# exception: temporal/activities.py's `_wrap_with_heartbeat` runs them in a
# background thread and heartbeats from the activity's own thread while
# polling it, so THEIR call sites below do set heartbeat_timeout (temporal/
# PHASE5.md Workstream C2). Every other activity still relies on generous
# start_to_close_timeouts alone.
_DEFAULT_TIMEOUT = timedelta(seconds=60)
_DOCKER_BUILD_TIMEOUT = timedelta(seconds=int(os.getenv('TEMPORAL_DOCKER_BUILD_TIMEOUT_SEC', '1200')))   # 20 min
_DOWNLOAD_TIMEOUT = timedelta(seconds=int(os.getenv('TEMPORAL_DOWNLOAD_TIMEOUT_SEC', '1800')))           # 30 min
_DOC_LINTER_TIMEOUT = timedelta(seconds=int(os.getenv('TEMPORAL_DOC_LINTER_TIMEOUT_SEC', '300')))        # 5 min (fetches URLs)
# Must exceed temporal/activities.py's _HEARTBEAT_INTERVAL_SEC (10s) with
# margin for scheduling jitter -- only used by the two heartbeat-wrapped
# activities (build_and_test_image, download_one).
_HEARTBEAT_TIMEOUT = timedelta(seconds=30)

# Retry policy for effects activities executed via _act(). Temporal's default
# is UNBOUNDED retries with backoff, up to execution_timeout -- fine for
# genuinely transient failures (network blips, a Docker-daemon hiccup) but
# wrong for a hard infra failure (disk full, daemon permanently down): it
# would retry uselessly for the whole execution_timeout window, bloating
# workflow history the same way a real run was TERMINATED for exceeding its
# history size limit during Phase 4 testing (temporal/PHASE5.md Workstream
# C1). This bounds it instead. Note this only ever governs genuine activity-
# execution exceptions -- agents/effects.py functions return structured
# result objects (BuildResult, ValidationResult, DownloadResult, ...) for
# EXPECTED failures (a bad Dockerfile fails to build, a linter rejects a
# wrapper), they don't raise for those, so this policy never masks or retries
# a real validation failure; MAX_ARTIFACT_LOOPS/MAX_ESCALATIONS already own
# that retry loop at the workflow-logic level.
_DEFAULT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=4,
)
# Long-running activities (each attempt can itself take minutes) get fewer
# retries so a persistent failure doesn't multiply a 20-30 minute timeout by
# several attempts before the workflow gives up.
_LONG_RUNNING_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=2,
)


@workflow.defn
class ModuleGenerationWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = temporal_agents.ALL_TEMPORAL_AGENTS

    def __init__(self) -> None:
        # Progress state exposed via the `progress` query. `_status` is set once
        # run() constructs it; the log sink captures recent WorkflowLogger lines.
        self._status: Optional[ModuleGenerationStatus] = None
        self._log: list[str] = []
        self.logger = WorkflowLogger(sink=self._log)
        # Human-in-the-loop GenePattern-upload approval gate (temporal/PHASE5.md
        # Workstream D). _upload_decision is None while awaiting a signal;
        # 'approved'/'rejected' once one arrives. _awaiting_upload_approval is
        # exposed via `progress()` so a client knows when to show a
        # decision-needed UI, distinct from "still working."
        self._awaiting_upload_approval: bool = False
        self._upload_decision: Optional[str] = None

    # -- human-in-the-loop signals ----------------------------------------

    @workflow.signal
    def approve_upload(self) -> None:
        """Signal: proceed with the pending GenePattern upload.

        No-op if no upload is currently pending (e.g. the signal arrives after
        the decision window already closed, or no upload was ever requested)
        -- Temporal delivers signals asynchronously and a client can't know
        the exact moment the workflow started waiting, so this must tolerate
        an early, late, or spurious call rather than erroring.
        """
        if self._awaiting_upload_approval:
            self._upload_decision = 'approved'

    @workflow.signal
    def reject_upload(self) -> None:
        """Signal: skip the pending GenePattern upload. See approve_upload's
        docstring for why this tolerates arriving when nothing is pending."""
        if self._awaiting_upload_approval:
            self._upload_decision = 'rejected'

    # -- progress query ---------------------------------------------------

    @workflow.query
    def progress(self) -> dict:
        """Read-only snapshot of the run's progress, for the web UI / clients.

        Replaces the shadow status.json the UI used to tail (temporal/PHASE4.md
        4.1/4.3): callers poll this query on the workflow handle instead of
        reading a file. Returns the structured ModuleGenerationStatus, a
        bounded tail of recent log lines, and whether the workflow is
        currently paused awaiting an upload-approval signal.
        """
        return {
            'status': self._status.to_dict() if self._status is not None else None,
            'log': list(self._log),
            'awaiting_upload_approval': self._awaiting_upload_approval,
        }

    # -- small helpers ----------------------------------------------------

    async def _act(self, fn, *args, timeout: timedelta = _DEFAULT_TIMEOUT,
                    retry_policy: RetryPolicy = _DEFAULT_RETRY_POLICY, **kwargs):
        """Run an activity with the given positional args, a default timeout,
        and a bounded retry policy (see _DEFAULT_RETRY_POLICY's comment)."""
        return await workflow.execute_activity(
            fn, args=list(args), start_to_close_timeout=timeout, retry_policy=retry_policy, **kwargs,
        )

    def _emit(self, result) -> None:
        for line in getattr(result, 'log', []) or []:
            self.logger.print_status(line)

    # -- phases -------------------------------------------------------------

    async def _do_research(self, tool_info: dict, status: ModuleGenerationStatus) -> tuple[bool, dict]:
        self.logger.print_section("Research Phase")
        self.logger.print_status("Starting research on tool information")

        try:
            instructions_section = ""
            if tool_info.get('instructions'):
                instructions_section = f"\n            Additional Instructions:\n            {tool_info['instructions']}\n"

            example_data_section = ""
            example_data = tool_info.get('example_data') or []
            if example_data:
                lines = ["", "            Example Data Provided (for reference only):"]
                for item in example_data:
                    kind = "URL" if item['is_url'] else "local file"
                    hint_label = f" [hint: {item['hint']}]" if item.get('hint') else ""
                    lines.append(f"            - {item['filename']} ({item['extension']}) - {kind}{hint_label}")
                lines.append("            These are examples of data the user already has. Use them to understand typical")
                lines.append("            input formats, but do NOT restrict your research to only these formats. Document")
                lines.append("            ALL formats the tool supports so the module remains broadly useful.")
                lines.append("            Where a [hint: ...] is shown, it describes the semantic role of that file")
                lines.append("            (e.g. 'tumor_sample', 'reference', 'germline_resource').")
                lines.append("")
                example_data_section = "\n".join(lines)

            prompt = f"""
            Research the bioinformatics tool '{tool_info['name']}' and provide comprehensive information.

            Known Information:
            - Name: {tool_info['name']}
            - Version: {tool_info['version']}
            - Language: {tool_info['language']}
            - Description: {tool_info.get('description', 'Not provided')}
            - Repository: {tool_info.get('repository_url', 'Not provided')}
            - Documentation: {tool_info.get('documentation_url', 'Not provided')}{instructions_section}{example_data_section}

            Please provide detailed research including:
            1. Tool purpose and scientific applications
            2. Input/output formats and requirements
            3. Parameter analysis and usage patterns
            4. Installation and dependency requirements
            5. Common workflows and use cases
            6. Integration considerations for GenePattern

            Focus on information that will help create a complete GenePattern module.
            """

            result = await temporal_agents.researcher_agent.run(prompt, usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS))
            status.add_usage(result)

            self.logger.print_status("Research phase completed successfully", "SUCCESS")
            return True, {'research': result.output}

        except Exception as e:
            error_msg = f"Research phase failed: {str(e)}"
            self.logger.print_status(error_msg, "ERROR")
            return False, {'error': error_msg}

    async def _do_planning(self, tool_info: dict, research_data: dict, status: ModuleGenerationStatus, module_path: str):
        self.logger.print_section("Planning Phase")
        self.logger.print_status("Starting module planning and parameter analysis")

        try:
            instructions_section = ""
            if tool_info.get('instructions'):
                instructions_section = f"\n            Additional Instructions (IMPORTANT - Pay close attention to these):\n            {tool_info['instructions']}\n"

            base_image_section = ""
            if tool_info.get('base_image'):
                base_image_section = (
                    f"\n            Base Docker Image (CRITICAL - use this EXACTLY as the docker_image_tag field):\n"
                    f"            {tool_info['base_image']}\n"
                    f"            Do NOT invent or normalise a genepattern/* tag - use the value above verbatim.\n"
                )

            example_data_section = ""
            example_data = tool_info.get('example_data') or []
            if example_data:
                lines = ["", "            Example Data Provided (for reference only):"]
                for item in example_data:
                    kind = "URL" if item['is_url'] else "local file"
                    hint_label = f" [hint: {item['hint']}]" if item.get('hint') else ""
                    lines.append(f"            - {item['filename']} ({item['extension']}) - {kind}{hint_label}")
                lines.append("            The user has this format available, so the module MUST accept it. However, do")
                lines.append("            NOT restrict the file_formats field to only this extension - include every")
                lines.append("            format the tool legitimately supports. The example data tells you what to")
                lines.append("            include, not what to exclude.")
                lines.append("            Where a [hint: ...] is shown, use it to assign the file to the correct")
                lines.append("            parameter (e.g. a file hinted 'tumor_sample' maps to the tumor BAM input,")
                lines.append("            'germline_resource' maps to the germline VCF parameter, etc.).")
                lines.append("")
                example_data_section = "\n".join(lines)

            prompt = f"""
            Create a comprehensive structured plan for the GenePattern module for '{tool_info['name']}'.

            Tool Information:
            - Name: {tool_info['name']}
            - Version: {tool_info['version']}
            - Language: {tool_info['language']}
            - Description: {tool_info.get('description', 'Not provided')}{instructions_section}{base_image_section}{example_data_section}

            Research Results:
            {research_data.get('research', 'No research data available')}

            Please create a structured ModulePlan with:
            1. Detailed parameter definitions with types and descriptions
            2. Module architecture recommendations
            3. Integration strategy for GenePattern
            4. Validation and testing approach
            5. Implementation roadmap

            If an author name is not provided, use 'GenePattern Team'.

            Focus on creating actionable specifications for module development.
            """

            result = await temporal_agents.planner_agent.run(prompt, usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS))
            status.add_usage(result)

            if module_path:
                try:
                    training_record = {
                        "instruction": prompt.strip(),
                        "output": result.output.model_dump_json(),
                    }
                    import json as _json
                    await self._act(
                        activities.write_text_file,
                        f"{module_path}/plan.jsonl",
                        _json.dumps(training_record) + "\n",
                    )
                except Exception as capture_err:
                    self.logger.print_status(f"Warning: could not save plan.jsonl: {capture_err}", "WARNING")

            self.logger.print_status("Planning phase completed successfully", "SUCCESS")
            return True, result.output

        except Exception as e:
            error_msg = f"Planning phase failed: {str(e)}"
            self.logger.print_status(error_msg, "ERROR")
            return False, None

    async def _validate_artifact(self, file_path: str, validate_tool: str, extra_args: Optional[list] = None) -> dict:
        """Dispatch to the linter or the docker-build activity, mirroring
        agents.validator.validate_artifact's success/error dict shape but
        without a Logger dependency (workflow-safe)."""
        if validate_tool == 'validate_dockerfile':
            # heartbeat_timeout is safe here (unlike most activities, see the
            # module-level note): activities.build_and_test_image is
            # heartbeat-wrapped, so a genuinely hung build is now detected at
            # _HEARTBEAT_TIMEOUT rather than only at the full 20-minute
            # start_to_close_timeout.
            result = await self._act(
                activities.build_and_test_image, file_path, extra_args,
                timeout=_DOCKER_BUILD_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=_LONG_RUNNING_RETRY_POLICY,
                task_queue='docker-builds',
            )
        elif validate_tool == 'validate_documentation':
            # The documentation linter fetches the doc URL over the network.
            result = await self._act(
                activities.run_linter, validate_tool, file_path, extra_args,
                timeout=_DOC_LINTER_TIMEOUT,
            )
        else:
            result = await self._act(activities.run_linter, validate_tool, file_path, extra_args)

        self._emit(result)
        if result.success:
            return {'success': True, 'result': result.output}
        return {'success': False, 'error': result.output}

    async def _sync_wrapper_script(self, planning_data, module_path: str, status: ModuleGenerationStatus, *, context: str = "") -> None:
        expected_name = planning_data.wrapper_script or "wrapper.py"
        ctx_prefix = f"[{context}] " if context else ""

        if await self._act(activities.file_exists, f"{module_path}/{expected_name}"):
            self.logger.print_status(f"{ctx_prefix}wrapper_script '{expected_name}' confirmed on disk")
            return

        self.logger.print_status(
            f"{ctx_prefix}WARNING: wrapper_script '{expected_name}' not found in {module_path} - scanning for actual wrapper file",
            "WARNING",
        )
        chosen_name = await self._act(activities.find_wrapper_file, module_path)
        if not chosen_name:
            self.logger.print_status(
                f"{ctx_prefix}No wrapper-like file found in {module_path}; keeping planning_data.wrapper_script='{expected_name}' unchanged",
                "WARNING",
            )
            return

        old_name = planning_data.wrapper_script
        planning_data.wrapper_script = chosen_name
        if status.planning_data is not planning_data and status.planning_data is not None:
            status.planning_data.wrapper_script = chosen_name

        self.logger.print_status(f"{ctx_prefix}Corrected wrapper_script: '{old_name}' -> '{chosen_name}'", "WARNING")

    async def _artifact_creation_loop(
        self, artifact_name: str, tool_info: dict, planning_data, module_path: str,
        status: ModuleGenerationStatus, max_loops: int = MAX_ARTIFACT_LOOPS,
        downstream_error_context: str = "",
    ) -> ArtifactResult:
        artifact_config = ARTIFACT_CONFIG[artifact_name]
        agent = artifact_config['agent']
        formatter = artifact_config['formatter']
        filename = artifact_config['filename']

        if artifact_name == 'wrapper':
            planning_dict = planning_data.model_dump(mode='json') if planning_data else {}
            wrapper_script_from_plan = planning_dict.get('wrapper_script')
            tool_language = tool_info.get('language', 'python').lower()
            _expected_wrapper_ext = {'python': '.py', 'r': '.R', 'bash': '.sh'}[select_wrapper_language(tool_language)]

            if wrapper_script_from_plan:
                # The planner freely chooses a wrapper_script name/extension and
                # doesn't always agree with select_wrapper_language()'s deterministic
                # mapping (e.g. it may pick .py for a C tool while wrapper_agent
                # correctly writes bash content for that same tool). The linter
                # infers expected syntax from the file EXTENSION, so a mismatch here
                # makes correct content fail validation against the wrong language.
                # Preserve the planner's chosen base name but enforce the extension.
                _stem = wrapper_script_from_plan.rsplit('.', 1)[0] if '.' in wrapper_script_from_plan else wrapper_script_from_plan
                if not wrapper_script_from_plan.endswith(_expected_wrapper_ext):
                    filename = f"{_stem}{_expected_wrapper_ext}"
                    self.logger.print_status(
                        f"Correcting wrapper filename extension: '{wrapper_script_from_plan}' -> '{filename}' "
                        f"(tool language '{tool_language}' requires a {select_wrapper_language(tool_language)} wrapper)",
                        "WARNING",
                    )
                else:
                    filename = wrapper_script_from_plan
            else:
                # Single source of truth: select_wrapper_language() (also used by
                # wrapper_agent itself), not a separately hand-maintained map.
                filename = f"wrapper{_expected_wrapper_ext}"

        validate_tool = artifact_config['validate_tool']
        create_method = artifact_config['create_method']
        file_path = f"{module_path}/{filename}"
        error_report = ""

        existing_errors = []
        if artifact_name in status.artifacts_status:
            existing_errors = status.artifacts_status[artifact_name].get('errors', [])
        status.artifacts_status[artifact_name] = {
            'generated': False, 'validated': False, 'attempts': 0,
            'errors': existing_errors if downstream_error_context else [],
        }

        for attempt in range(1, max_loops + 1):
            try:
                self.logger.print_status(f"Generating {artifact_name} (attempt {attempt}/{max_loops})")
                status.artifacts_status[artifact_name]['attempts'] = attempt

                planning_data_dict = planning_data.model_dump(mode='json')
                example_data = status.example_data or []
                example_data_dicts = [item.to_dict() for item in example_data]

                # Truncate every artifact's error history, not just dockerfile's
                # (temporal/PHASE5.md Workstream A2 -- see agents/module.py's
                # mirror of this loop for the full rationale).
                all_errors = status.artifacts_status[artifact_name].get('errors', [])

                def _truncate_error_report(raw: str, max_tail: int = 50) -> str:
                    extracted = []
                    for ln in raw.splitlines():
                        if any(ind in ln for ind in ERROR_INDICATORS):
                            sanitized = _sanitize_error_line(ln)
                            if sanitized and sanitized not in extracted:
                                extracted.append(sanitized)
                    tail_lines = raw.splitlines()[-max_tail:]
                    parts = []
                    if extracted:
                        parts.append("KEY ERRORS:\n" + "\n".join(f"  - {e}" for e in extracted))
                    parts.append("LAST 50 LINES OF OUTPUT:\n" + "\n".join(tail_lines))
                    return "\n\n".join(parts)
                error_history_list = [_truncate_error_report(e) for e in all_errors]

                prompt_prefix = ""
                if artifact_name == 'dockerfile':
                    _wrapper_script = planning_data.wrapper_script or 'wrapper.py'
                    _wrapper_src = await self._act(activities.read_text_file, f"{module_path}/{_wrapper_script}")
                    if _wrapper_src is not None:
                        prompt_prefix = (
                            f"Wrapper Script ({_wrapper_script}) - use this to determine "
                            f"which packages must be installed in the image:\n"
                            f"```\n{_wrapper_src}\n```\n\n"
                        )

                prompt = (
                    f"{prompt_prefix}"
                    f"Generate the {artifact_name} artifact for the GenePattern module "
                    f"'{tool_info['name']}'. Call the {create_method} tool."
                )

                deps_context = ArtifactDeps(
                    tool_info=tool_info,
                    planning_data=planning_data.model_dump(mode='json'),
                    error_report=error_report,
                    attempt=attempt,
                    max_loops=max_loops,
                    example_data=example_data_dicts,
                    downstream_error_context=downstream_error_context,
                    error_history=error_history_list,
                )

                result = await agent.run(prompt, deps=deps_context, usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS))
                artifact_model = result.output
                status.add_usage(result)

                formatted_content = formatter(artifact_model)
                await self._act(activities.write_text_file, file_path, formatted_content)

                report_content = getattr(artifact_model, 'artifact_report', None)
                if report_content:
                    report_path = f"{module_path}/report-{artifact_name}.md"
                    await self._act(activities.write_text_file, report_path, report_content)
                    self.logger.print_status(f"Generated {artifact_name} report: report-{artifact_name}.md")

                status.artifacts_status[artifact_name]['generated'] = True
                self.logger.print_status(f"Generated {filename}")

                extra_validation_args = None
                if artifact_name == 'wrapper':
                    planned_params = planning_data_dict.get('parameters', [])
                    if planned_params:
                        param_names_for_lint = [p['name'] for p in planned_params if isinstance(p, dict) and p.get('name')]
                        if param_names_for_lint:
                            extra_validation_args = ['--parameters'] + param_names_for_lint

                elif artifact_name == 'manifest':
                    wrapper_script = planning_data_dict.get('wrapper_script') or 'wrapper.py'
                    wrapper_full_path = f"{module_path}/{wrapper_script}"
                    if await self._act(activities.file_exists, wrapper_full_path):
                        extra_validation_args = ['--wrapper', wrapper_full_path]

                elif artifact_name == 'dockerfile':
                    docker_tag = planning_data_dict.get('docker_image_tag', '')
                    extra_validation_args = []
                    if docker_tag:
                        extra_validation_args.extend(['-t', docker_tag])

                    if example_data:
                        gpunit_params: dict = {}
                        test_yml_text = await self._act(activities.read_text_file, f"{module_path}/test.yml")
                        if test_yml_text is not None:
                            try:
                                import yaml
                                gpunit_doc = yaml.safe_load(test_yml_text)
                                if isinstance(gpunit_doc, dict):
                                    gpunit_params = gpunit_doc.get('params', {}) or {}
                            except Exception as e:
                                self.logger.print_status(f"Could not parse test.yml for runtime params: {e}", "WARNING")

                        rc_result = await self._act(
                            activities.build_dockerfile_runtime_command,
                            planning_data_dict, example_data_dicts, gpunit_params, module_path,
                        )
                        runtime_cmd = rc_result.get('command')
                        volumes = rc_result.get('volumes') or []
                        if runtime_cmd:
                            extra_validation_args.extend(['-c', runtime_cmd])
                        for vol in volumes:
                            extra_validation_args.extend(['-v', vol])

                    if not extra_validation_args:
                        extra_validation_args = None

                elif artifact_name == 'gpunit':
                    extra_validation_args = []
                    module_name = planning_data_dict.get('module_name', '')
                    if module_name:
                        extra_validation_args.extend(['--module', module_name])

                    parameters = planning_data_dict.get('parameters', [])
                    if parameters:
                        required_params = [p for p in parameters if p.get('name') and p.get('required', False)]
                        if required_params:
                            required_param_names = [p['name'] for p in required_params]
                            extra_validation_args.append('--parameters')
                            extra_validation_args.extend(required_param_names)
                            param_types = [normalize_param_type(p.get('type', 'text')) for p in required_params]
                            extra_validation_args.append('--types')
                            extra_validation_args.extend(param_types)

                    if not extra_validation_args:
                        extra_validation_args = None

                validation_result = await self._validate_artifact(file_path, validate_tool, extra_validation_args)

                if validation_result['success']:
                    status.artifacts_status[artifact_name]['validated'] = True
                    self.logger.print_status(f"Successfully generated and validated {artifact_name}", "SUCCESS")
                    return ArtifactResult(success=True, artifact_name=artifact_name)
                else:
                    error_report = f"Validation failed: {validation_result.get('error', 'Unknown validation error')}"
                    self.logger.print_status(error_report, "ERROR")
                    status.artifacts_status[artifact_name]['errors'].append(error_report)

                    if attempt == max_loops:
                        root_cause = classify_error(error_report, artifact_name)
                        return ArtifactResult(success=False, artifact_name=artifact_name, error_text=error_report, root_cause=root_cause)

            except Exception as e:
                error_report = f"Error generating {artifact_name}: {str(e)}"
                self.logger.print_status(error_report, "ERROR")
                status.artifacts_status[artifact_name]['errors'].append(error_report)

                if attempt == max_loops:
                    root_cause = classify_error(error_report, artifact_name)
                    return ArtifactResult(success=False, artifact_name=artifact_name, error_text=error_report, root_cause=root_cause)

        root_cause = classify_error(error_report, artifact_name)
        return ArtifactResult(success=False, artifact_name=artifact_name, error_text=error_report, root_cause=root_cause)

    async def _zip_artifacts(self, module_path: str, tool_name: str, planning_data, zip_only: bool = False) -> Optional[str]:
        self.logger.print_section("Zipping Artifacts")
        members = ['manifest', 'paramgroups.json', 'test.yml', 'README.md', 'Dockerfile']
        wrapper_script = planning_data.wrapper_script if planning_data else None
        if wrapper_script:
            members.append(wrapper_script)
        zip_name = f"{tool_name.lower().replace(' ', '_').replace('-', '_')}.zip"
        result = await self._act(activities.zip_artifacts, module_path, zip_name, members, zip_only)
        self._emit(result)
        if not result.ok or not result.zip_path:
            return None
        return result.zip_path

    async def _run_install_artifact(
        self, tool_info: dict, planning_data, module_path: str, zip_only: bool,
        gp_server: Optional[str], gp_user: Optional[str], gp_password: Optional[str],
        status: ModuleGenerationStatus, require_upload_approval: bool = False,
    ) -> ArtifactResult:
        zip_path = await self._zip_artifacts(module_path, tool_info['name'], planning_data, zip_only)
        if zip_path is None:
            return ArtifactResult(
                success=False, artifact_name='install', error_text="Failed to create zip archive.",
                root_cause=RootCause(target_artifact='manifest', reason="Zip creation failed; manifest or paramgroups may be invalid.", original_artifact='install'),
            )

        if not (gp_server and gp_user):
            return ArtifactResult(success=True, artifact_name='install')

        if require_upload_approval:
            self.logger.print_section("Awaiting Upload Approval")
            self.logger.print_status(
                "Zip archive ready; waiting for a human to approve or reject the GenePattern "
                "upload (signal this workflow's approve_upload/reject_upload).",
            )
            self._awaiting_upload_approval = True
            self._upload_decision = None
            # No timeout: durably waiting on a human decision -- for however
            # long that takes -- is the point of this gate (temporal/PHASE5.md
            # Workstream D). The workflow's own execution_timeout is the outer
            # bound; callers that need approval windows longer than the
            # default should raise it (temporal/client.py's
            # start_module_generation takes an execution_timeout override).
            await workflow.wait_condition(lambda: self._upload_decision is not None)
            self._awaiting_upload_approval = False

            if self._upload_decision == 'rejected':
                self.logger.print_status("Upload rejected by operator; module was generated but not published.", "WARNING")
                status.upload_status = 'declined'
                return ArtifactResult(success=True, artifact_name='install')

            self.logger.print_status("Upload approved by operator.", "SUCCESS")

        self.logger.print_section("Uploading to GenePattern")
        upload_result = await self._act(activities.upload_module, zip_path, gp_server, gp_user, gp_password)
        self._emit(upload_result)
        if upload_result.success:
            status.upload_status = 'uploaded'
            return ArtifactResult(success=True, artifact_name='install')

        status.upload_status = 'failed'
        return ArtifactResult(
            success=False, artifact_name='install', error_text=f"GenePattern upload failed for {zip_path}.",
            root_cause=RootCause(target_artifact='manifest', reason="GenePattern module install failed. The manifest or paramgroups may be invalid.", original_artifact='install'),
        )

    async def _generate_all_artifacts(
        self, tool_info: dict, planning_data, module_path: str, status: ModuleGenerationStatus,
        skip_artifacts: list, max_loops: int, max_escalations: int,
        no_zip: bool, zip_only: bool, gp_server: Optional[str], gp_user: Optional[str], gp_password: Optional[str],
        require_upload_approval: bool = False,
    ) -> bool:
        self.logger.print_section("Artifact Generation Phase")

        all_artifacts_successful = True
        artifact_queue = [name for name in ARTIFACT_CONFIG if name not in skip_artifacts]
        if not no_zip and 'install' not in skip_artifacts:
            artifact_queue.append('install')
        escalation_pair_counts: dict = {}
        pending_downstream_context: dict = {}

        idx = 0
        while idx < len(artifact_queue):
            artifact_name = artifact_queue[idx]

            existing_status = status.artifacts_status.get(artifact_name, {})
            if existing_status.get('validated', False):
                idx += 1
                continue

            self.logger.print_status(f"Generating {artifact_name}...")
            downstream_ctx = pending_downstream_context.pop(artifact_name, "")

            if artifact_name == 'install':
                result = await self._run_install_artifact(
                    tool_info, planning_data, module_path, zip_only, gp_server, gp_user, gp_password,
                    status, require_upload_approval,
                )
            else:
                if artifact_name == 'dockerfile':
                    user_base_image = (tool_info.get('base_image') or '').strip()
                    if user_base_image:
                        manifest_image = await self._act(activities.read_manifest_docker_image, module_path)
                        if manifest_image and manifest_image == user_base_image:
                            self.logger.print_status(
                                f"Skipping Dockerfile generation - manifest already uses base image '{user_base_image}' as job.docker.image"
                            )
                            status.artifacts_status['dockerfile'] = {'generated': True, 'validated': True, 'skipped': True, 'attempts': 0, 'errors': []}
                            idx += 1
                            continue

                    await self._sync_wrapper_script(planning_data, module_path, status, context="pre-dockerfile assertion")

                result = await self._artifact_creation_loop(
                    artifact_name, tool_info, planning_data, module_path, status, max_loops,
                    downstream_error_context=downstream_ctx,
                )

            if result.success:
                if artifact_name == 'wrapper':
                    await self._sync_wrapper_script(planning_data, module_path, status, context="post-wrapper sync")
                idx += 1
                continue

            root_cause = result.root_cause
            escalated = False

            if root_cause and should_escalate(root_cause):
                target = root_cause.target_artifact
                pair_key = (artifact_name, target)
                current_count = escalation_pair_counts.get(pair_key, 0)

                can_escalate = (
                    current_count < max_escalations
                    and target not in skip_artifacts
                    and target in ARTIFACT_CONFIG
                    and target in get_upstream_dependencies(artifact_name)
                )

                if can_escalate:
                    escalation_pair_counts[pair_key] = current_count + 1
                    status.escalation_counts[pair_key[0]] = status.escalation_counts.get(pair_key[0], 0) + 1
                    status.escalation_log.append({
                        'from_artifact': artifact_name, 'to_artifact': target,
                        'reason': root_cause.reason, 'error_snippet': result.error_text[:500],
                    })

                    self.logger.print_section("Cross-Artifact Escalation")
                    self.logger.print_status(f"Escalating: {artifact_name} failure -> regenerating {target}", "WARNING")

                    if target in status.artifacts_status:
                        status.artifacts_status[target]['validated'] = False
                        status.artifacts_status[target]['generated'] = False

                    extra_context = ""
                    if target in ('manifest', 'wrapper'):
                        planning_dict_esc = planning_data.model_dump(mode='json') if planning_data else {}
                        wrapper_script_esc = planning_dict_esc.get('wrapper_script') or 'wrapper.py'
                        wrapper_src = await self._act(activities.read_text_file, f"{module_path}/{wrapper_script_esc}")
                        if wrapper_src is not None:
                            try:
                                import ast as _ast
                                declared_flags = []
                                tree = _ast.parse(wrapper_src)
                                for node in _ast.walk(tree):
                                    if isinstance(node, _ast.Call):
                                        func = node.func
                                        is_add_arg = (
                                            (isinstance(func, _ast.Attribute) and func.attr == 'add_argument')
                                            or (isinstance(func, _ast.Name) and func.id == 'add_argument')
                                        )
                                        if is_add_arg:
                                            for a in node.args:
                                                if isinstance(a, _ast.Constant) and isinstance(a.value, str) and a.value.startswith('--'):
                                                    declared_flags.append(a.value)
                                if declared_flags:
                                    extra_context = (
                                        f"\n\nWrapper script '{wrapper_script_esc}' currently declares these add_argument() flags:\n"
                                        + "\n".join(f"  {f}" for f in sorted(declared_flags))
                                        + "\n\nThe manifest pN_name values and commandLine placeholders MUST "
                                        "use these exact flag names (or the wrapper must be updated to match "
                                        "the manifest's parameter names - they must be consistent)."
                                    )
                            except Exception:
                                pass

                    pending_downstream_context[target] = (
                        f"The downstream artifact '{artifact_name}' failed validation with the following error:\n\n"
                        f"{result.error_text[:1500]}\n\n"
                        f"Root-cause analysis: {root_cause.reason}"
                        f"{extra_context}\n\n"
                        f"You must fix the issue in THIS artifact ({target}) so that the downstream '{artifact_name}' step can succeed."
                    )

                    remaining = artifact_queue[idx:]
                    if target in remaining:
                        remaining.remove(target)
                    artifact_queue = artifact_queue[:idx] + [target, artifact_name] + [a for a in remaining if a != artifact_name]
                    escalated = True
                elif current_count >= max_escalations:
                    self.logger.print_status(
                        f"Escalation cap reached for {artifact_name}->{target} ({max_escalations} attempts). Marking {artifact_name} as failed.",
                        "WARNING",
                    )

            if not escalated:
                self.logger.print_status(f"Failed to generate {artifact_name} after {max_loops} attempts")
                all_artifacts_successful = False

                if artifact_name in ('manifest', 'wrapper'):
                    failure_summary = result.error_text[:1500] if result.error_text else ""
                    existing_ctx = pending_downstream_context.get('dockerfile', '')
                    new_ctx = (
                        f"WARNING: The '{artifact_name}' artifact failed validation. "
                        f"The dockerfile runtime test command is derived from the manifest commandLine - "
                        f"if the manifest has wrong parameter names the runtime test will fail with "
                        f"'unrecognized arguments' even if the Dockerfile itself is correct. "
                        f"DO NOT attempt to fix this by changing the Dockerfile. "
                        f"The wrapper's add_argument() flags and the manifest pN_name values must be made consistent first.\n\n"
                        f"{artifact_name} failure details:\n{failure_summary}"
                    )
                    pending_downstream_context['dockerfile'] = (existing_ctx + "\n\n" + new_ctx) if existing_ctx else new_ctx

                break

        return all_artifacts_successful

    # -- entry point ----------------------------------------------------

    @workflow.run
    async def run(
        self,
        tool_info: dict,
        skip_artifacts: Optional[list] = None,
        max_loops: int = MAX_ARTIFACT_LOOPS,
        max_escalations: int = MAX_ESCALATIONS,
        no_zip: bool = False,
        zip_only: bool = False,
        docker_push: bool = False,
        gp_server: Optional[str] = None,
        gp_user: Optional[str] = None,
        gp_password: Optional[str] = None,
        require_upload_approval: bool = False,
    ) -> dict:
        """Run a fresh module generation. --resume was removed entirely (see module docstring)."""
        self.logger.print_status(f"Generating module for: {tool_info['name']}")

        timestamp = workflow.now().strftime("%Y%m%d_%H%M%S")
        module_path = await self._act(
            activities.make_module_dir, tool_info.get('output_dir', './generated-modules'),
            tool_info['name'], timestamp, tool_info.get('module_dir', ''),
        )

        example_data_dicts = tool_info.get('example_data') or []
        example_data_items = [ExampleDataItem.from_dict(d) for d in example_data_dicts]

        status = ModuleGenerationStatus(
            tool_name=tool_info['name'], module_directory=module_path, example_data=example_data_items,
        )
        self._status = status   # expose to the progress query as it's mutated in place
        tool_info['example_data'] = example_data_dicts

        # Download URL-based example data (deterministic collision-resolution
        # loop, matching agents.module.ModuleAgent.download_url_data).
        url_items = [item for item in example_data_items if item.is_url]
        if url_items:
            data_dir = f"{module_path}/data"
            used_names: set = set()
            for item in url_items:
                filename = item.filename
                if filename in used_names:
                    # Path() here is pure string parsing (.stem/.suffix), no filesystem
                    # access -- safe inside workflow code.
                    stem = _PurePosixPath(filename).stem
                    suffix = _PurePosixPath(filename).suffix
                    counter = 1
                    while filename in used_names:
                        filename = f"{stem}_{counter}{suffix}"
                        counter += 1
                used_names.add(filename)
                dl_result = await self._act(
                    activities.download_one, item.original, data_dir, filename,
                    timeout=_DOWNLOAD_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=_LONG_RUNNING_RETRY_POLICY,
                )
                self._emit(dl_result)
                if dl_result.ok and dl_result.local_path:
                    item.local_path = dl_result.local_path
            status.example_data = example_data_items


        # Phase 1: Research
        research_success, research_data = await self._do_research(tool_info, status)
        if research_success:
            status.research_data = research_data
        else:
            status.error_messages.append(research_data.get('error', 'Research failed'))
        if status.research_data:
            await self._act(activities.write_text_file, f"{module_path}/research.md", status.research_data.get('research', ''))

        if not status.research_complete:
            return {'success': False, 'module_directory': module_path, 'status': status.to_dict()}

        # Phase 2: Planning
        planning_success, planning_data = await self._do_planning(tool_info, status.research_data, status, module_path)
        if planning_success:
            status.planning_data = planning_data
        else:
            status.error_messages.append("Planning failed")
        if status.planning_data:
            await self._act(activities.write_text_file, f"{module_path}/plan.md", status.planning_data.plan)

        if not status.planning_complete:
            return {'success': False, 'module_directory': module_path, 'status': status.to_dict()}

        # Phase 3: Artifact generation
        skip = list(skip_artifacts or [])
        artifacts_success = await self._generate_all_artifacts(
            tool_info, status.planning_data, module_path, status,
            skip, max_loops, max_escalations, no_zip, zip_only, gp_server, gp_user, gp_password,
            require_upload_approval,
        )

        dockerfile_validated = status.artifacts_status.get('dockerfile', {}).get('validated', False)
        if dockerfile_validated:
            await self._act(activities.remove_dir, f"{module_path}/data")

        if artifacts_success and docker_push:
            self.logger.print_section("Docker Push")
            planning_dict = status.planning_data.model_dump(mode='json') if status.planning_data else {}
            tag = planning_dict.get('docker_image_tag', '')
            if tag:
                push_result = await self._act(activities.docker_push, tag)
                self._emit(push_result)

        overall_success = status.research_complete and status.planning_complete and artifacts_success
        return {'success': overall_success, 'module_directory': module_path, 'status': status.to_dict()}
