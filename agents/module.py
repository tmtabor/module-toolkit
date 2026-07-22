"""
ModuleAgent — main orchestrator for GenePattern module generation.

Coordinates the research → planning → artifact-generation pipeline,
delegating to specialised sub-agents for each phase and artifact type.
"""

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic_ai.usage import UsageLimits

from agents.config import DEFAULT_OUTPUT_DIR, MAX_AGENT_REQUESTS, MAX_ARTIFACT_LOOPS, MAX_ESCALATIONS
from agents.error_classifier import (
    classify_error, should_escalate,
    get_upstream_dependencies, _sanitize_error_line, RootCause,
)

# Shared list of error indicator strings used when extracting key errors from
# verbose build/runtime output.  Single definition eliminates the copy-paste
# that previously appeared in multiple places inside artifact_creation_loop.
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
from agents import effects
from agents.example_data import ExampleDataItem
from agents.logger import Logger
from agents.models import ArtifactModel, ArtifactDeps
from agents.planner import planner_agent, ModulePlan
from agents.researcher import researcher_agent
from agents.status import ArtifactResult, ModuleGenerationStatus
from agents.validator import validate_artifact as _validate_artifact
from dockerfile.agent import dockerfile_agent
from dockerfile.runtime import build_runtime_command as _build_runtime_command
from documentation.agent import documentation_agent
from gpunit.agent import gpunit_agent
from gpunit.linter import normalize_param_type
from manifest.agent import manifest_agent
from manifest.models import ManifestModel
from paramgroups.agent import paramgroups_agent
from paramgroups.models import ParamgroupsModel
from wrapper.agent import wrapper_agent, select_wrapper_language


class ModuleAgent:
    """
    Main orchestrator agent for GenePattern module generation.
    Groups all methods for calling other agents, validation, and reporting.
    """

    def __init__(self, logger: Logger = None, output_dir: str = DEFAULT_OUTPUT_DIR):
        """Initialize the module agent with MCP server for validation"""
        self.logger = logger or Logger()
        self.output_dir = output_dir

        # Define artifact agents mapping with models and formatters
        self.artifact_agents = {
            'wrapper': {
                'agent': wrapper_agent,
                'model': ArtifactModel,
                'filename': 'wrapper.py',
                'validate_tool': 'validate_wrapper',
                'create_method': 'create_wrapper',
                'formatter': lambda m: m.code
            },
            'manifest': {
                'agent': manifest_agent,
                'model': ManifestModel,
                'filename': 'manifest',
                'validate_tool': 'validate_manifest',
                'create_method': 'create_manifest',
                'formatter': lambda m: m.to_manifest_string()
            },
            'paramgroups': {
                'agent': paramgroups_agent,
                'model': ParamgroupsModel,
                'filename': 'paramgroups.json',
                'validate_tool': 'validate_paramgroups',
                'create_method': 'create_paramgroups',
                'formatter': lambda m: m.to_json_string()
            },
            'gpunit': {
                'agent': gpunit_agent,
                'model': ArtifactModel,
                'filename': 'test.yml',
                'validate_tool': 'validate_gpunit',
                'create_method': 'create_gpunit',
                'formatter': lambda m: m.code
            },
            'documentation': {
                'agent': documentation_agent,
                'model': ArtifactModel,
                'filename': 'README.md',
                'validate_tool': 'validate_documentation',
                'create_method': 'create_documentation',
                'formatter': lambda m: m.code
            },
            'dockerfile': {
                'agent': dockerfile_agent,
                'model': ArtifactModel,
                'filename': 'Dockerfile',
                'validate_tool': 'validate_dockerfile',
                'create_method': 'create_dockerfile',
                'formatter': lambda m: m.code
            }
        }

    def _emit(self, result) -> None:
        """Drain an effect result's captured log lines through this agent's Logger."""
        for line in getattr(result, 'log', []):
            self.logger.print_status(line)

    def create_module_directory(self, tool_name: str, module_dir: str = "") -> Path:
        """Create and return the module directory path.

        If *module_dir* is a non-empty absolute (or relative) path it is used
        directly, allowing the caller (e.g. the web UI) to guarantee that
        uploaded files and generated artifacts share the same directory.
        """
        # Timestamp is generated here (coordination) and injected into the
        # effect; Phase 3 will source it from workflow.now() instead.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        module_path = Path(effects.make_module_dir(self.output_dir, tool_name, timestamp, module_dir))
        self.logger.print_status(f"Creating module directory: {module_path}")
        return module_path

    def download_url_data(self, example_data: List[ExampleDataItem], module_path: Path) -> None:
        """Download URL-based example data items into {module_path}/data/ before planning.

        Sets item.local_path on each downloaded item so all downstream steps can
        use item.local_path uniformly without checking is_url.
        """
        url_items = [item for item in example_data if item.is_url]
        if not url_items:
            return

        data_dir = module_path / "data"

        # Track filenames used in this session to handle collisions (deterministic
        # coordination); each resolved filename is handed to the download effect.
        used_names: set = set()

        for item in url_items:
            # Resolve filename collisions
            filename = item.filename
            if filename in used_names:
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                counter = 1
                while filename in used_names:
                    filename = f"{stem}_{counter}{suffix}"
                    counter += 1
            used_names.add(filename)

            result = effects.download_one(item.original, str(data_dir), filename)
            self._emit(result)
            if result.ok and result.local_path:
                item.local_path = result.local_path
            # On failure local_path stays None — downstream steps skip the item.

    def cleanup_data_dir(self, module_path: Path) -> None:
        """Remove the data/ subdirectory after a successful dockerfile step."""
        data_dir = module_path / "data"
        if not data_dir.exists():
            return
        effects.remove_dir(str(data_dir))
        self.logger.print_status(f"Cleaned up data directory: {data_dir}")

    async def do_research(self, tool_info: Dict[str, str], status: ModuleGenerationStatus = None) -> Tuple[bool, Dict[str, Any]]:
        """Run research phase using researcher agent"""
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
                    kind = "URL" if item.is_url else "local file"
                    lines.append(f"            - {item.filename} ({item.extension}) — {kind}{item.hint_label}")
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

            result = await researcher_agent.run(prompt, usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS))

            # Track token usage if status provided
            if status:
                status.add_usage(result)

            self.logger.print_status("Research phase completed successfully", "SUCCESS")
            return True, {'research': result.output}

        except Exception as e:
            error_msg = f"Research phase failed: {str(e)}"
            self.logger.print_status(error_msg, "ERROR")
            self.logger.print_status(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return False, {'error': error_msg}

    async def do_planning(self, tool_info: Dict[str, str], research_data: Dict[str, Any], status: ModuleGenerationStatus = None, module_path: Path = None) -> Tuple[bool, ModulePlan]:
        """Run planning phase using planner agent"""
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
                    f"            Do NOT invent or normalise a genepattern/* tag — use the value above verbatim.\n"
                )

            example_data_section = ""
            example_data = tool_info.get('example_data') or []
            if example_data:
                lines = ["", "            Example Data Provided (for reference only):"]
                for item in example_data:
                    kind = "URL" if item.is_url else "local file"
                    lines.append(f"            - {item.filename} ({item.extension}) — {kind}{item.hint_label}")
                lines.append("            The user has this format available, so the module MUST accept it. However, do")
                lines.append("            NOT restrict the file_formats field to only this extension — include every")
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

            result = await planner_agent.run(prompt, usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS))

            # Track token usage if status provided
            if status:
                status.add_usage(result)

            # Capture training data for LoRA fine-tuning
            if module_path is not None:
                try:
                    training_record = {
                        "instruction": prompt.strip(),
                        "output": result.output.model_dump_json(),
                    }
                    jsonl_path = module_path / "plan.jsonl"
                    effects.write_text_file(str(jsonl_path), json.dumps(training_record) + "\n")
                    self.logger.print_status(f"Training data saved to {jsonl_path}", "DEBUG")
                except Exception as capture_err:
                    self.logger.print_status(f"Warning: could not save plan.jsonl: {capture_err}", "WARNING")

            self.logger.print_status("Planning phase completed successfully", "SUCCESS")
            return True, result.output

        except Exception as e:
            error_msg = f"Planning phase failed: {str(e)}"
            self.logger.print_status(error_msg, "ERROR")
            self.logger.print_status(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return False, None

    async def artifact_creation_loop(self, artifact_name: str, tool_info: Dict[str, str], planning_data: ModulePlan, module_path: Path, status: ModuleGenerationStatus, max_loops: int = MAX_ARTIFACT_LOOPS, downstream_error_context: str = "") -> ArtifactResult:
        """Generate and validate a single artifact using its dedicated agent"""
        artifact_config = self.artifact_agents[artifact_name]
        agent = artifact_config['agent']
        model_class = artifact_config.get('model', ArtifactModel)
        formatter = artifact_config.get('formatter', lambda m: m.code)
        filename = artifact_config['filename']

        # Special handling for wrapper: determine extension based on tool language
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
                        f"Correcting wrapper filename extension: '{wrapper_script_from_plan}' → '{filename}' "
                        f"(tool language '{tool_language}' requires a {select_wrapper_language(tool_language)} wrapper)",
                        "WARNING",
                    )
                else:
                    filename = wrapper_script_from_plan
                    self.logger.print_status(f"Using wrapper filename from planning data: {filename}")
            else:
                # Single source of truth: select_wrapper_language() (also used by
                # wrapper_agent itself), not a separately hand-maintained map.
                filename = f'wrapper{_expected_wrapper_ext}'
                self.logger.print_status(f"Using default wrapper filename: {filename}")

        validate_tool = artifact_config['validate_tool']
        create_method = artifact_config['create_method']
        file_path = module_path / filename
        error_report = ""

        # Initialize artifact status (preserve errors from previous runs during escalation)
        existing_errors = []
        if artifact_name in status.artifacts_status:
            existing_errors = status.artifacts_status[artifact_name].get('errors', [])
        status.artifacts_status[artifact_name] = {
            'generated': False,
            'validated': False,
            'attempts': 0,
            'errors': existing_errors if downstream_error_context else []
        }

        def build_error_history() -> str:
            """Build a numbered history of all previous errors for this artifact."""
            errors = status.artifacts_status[artifact_name].get('errors', [])
            if not errors:
                return ""
            lines = ["Previous attempt errors (avoid repeating these mistakes):"]
            for i, err in enumerate(errors, 1):
                lines.append(f"\nAttempt {i} error:\n{err}")
            return "\n".join(lines)

        for attempt in range(1, max_loops + 1):
            try:
                self.logger.print_status(f"Generating {artifact_name} (attempt {attempt}/{max_loops})")
                status.artifacts_status[artifact_name]['attempts'] = attempt

                # Serialize planning_data here for use in prompt-building below.
                # NOTE: deps_context re-serializes immediately before the agent
                # call so that any correction made by _sync_wrapper_script (which
                # runs in generate_all_artifacts before this loop is entered) is
                # always reflected in what the LLM tool receives.
                planning_data_dict = planning_data.model_dump(mode='json')
                example_data: List[ExampleDataItem] = status.example_data or []

                # Serialize example_data for ArtifactDeps
                example_data_dicts = [item.to_dict() for item in example_data]

                # Build error history list for this artifact. Truncate every
                # entry (not just dockerfile's, which is where this was
                # originally scoped) -- linter output for any artifact can run
                # to several KB per attempt, and status.artifacts_status[...]
                # ['errors'] accumulates one entry per retry with no other
                # bound, so an untruncated list was a real unbounded-growth
                # contributor to the payload-size issues in
                # temporal/PHASE5.md Workstream A2.
                all_errors = status.artifacts_status[artifact_name].get('errors', [])

                def _truncate_error_report(raw: str, max_tail: int = 50) -> str:
                    """Return structured error lines + last `max_tail` lines of raw output."""
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

                # For dockerfile, inject wrapper source into the prompt directly
                # (too large for ArtifactDeps; passed as prompt text to create_dockerfile tool)
                prompt_prefix = ""
                if artifact_name == 'dockerfile':
                    _wrapper_script = planning_data.wrapper_script or 'wrapper.py'
                    _wrapper_path = module_path / _wrapper_script
                    _wrapper_src = effects.read_text_file(str(_wrapper_path))
                    if _wrapper_src is not None:
                        try:
                            prompt_prefix = (
                                f"Wrapper Script ({_wrapper_script}) — use this to determine "
                                f"which packages must be installed in the image:\n"
                                f"```\n{_wrapper_src}\n```\n\n"
                            )
                        except Exception as _we:
                            self.logger.print_status(f"Could not read wrapper for dockerfile prompt: {_we}", "WARNING")

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

                result = await agent.run(
                    prompt,
                    deps=deps_context,
                    usage_limits=UsageLimits(request_limit=MAX_AGENT_REQUESTS),
                )
                artifact_model = result.output

                # Track token usage
                status.add_usage(result)

                formatted_content = formatter(artifact_model)

                effects.write_text_file(str(file_path), formatted_content)

                # Write the report file if the artifact has one
                report_content = None
                if hasattr(artifact_model, 'artifact_report') and artifact_model.artifact_report:
                    report_content = artifact_model.artifact_report

                if report_content:
                    report_path = module_path / f"report-{artifact_name}.md"
                    effects.write_text_file(str(report_path), report_content)
                    self.logger.print_status(f"Generated {artifact_name} report: {report_path.name}")

                status.artifacts_status[artifact_name]['generated'] = True
                self.logger.print_status(f"Generated {filename}")

                # Prepare extra validation arguments based on artifact type
                extra_validation_args = None
                if artifact_name == 'wrapper':
                    # Pass planning-data parameter names so the wrapper linter can
                    # verify that every planned parameter appears as a --flag in
                    # the generated wrapper script.
                    planned_params = planning_data_dict.get('parameters', [])
                    if planned_params:
                        param_names_for_lint = [
                            p['name'] for p in planned_params
                            if isinstance(p, dict) and p.get('name')
                        ]
                        if param_names_for_lint:
                            extra_validation_args = ['--parameters'] + param_names_for_lint
                            self.logger.print_status(
                                f"Passing {len(param_names_for_lint)} planning-data parameter names to wrapper linter"
                            )

                elif artifact_name == 'manifest':
                    # Pass the wrapper script path so the consistency linter can
                    # cross-check manifest parameter names against add_argument() flags.
                    wrapper_script = planning_data_dict.get('wrapper_script') or 'wrapper.py'
                    wrapper_path = module_path / wrapper_script
                    if wrapper_path.exists():
                        extra_validation_args = ['--wrapper', str(wrapper_path)]
                        self.logger.print_status(f"Passing wrapper to manifest linter: {wrapper_path.name}")

                elif artifact_name == 'dockerfile':
                    docker_tag = planning_data_dict.get('docker_image_tag', '')
                    extra_validation_args = []
                    if docker_tag:
                        extra_validation_args.extend(['-t', docker_tag])
                        self.logger.print_status(f"Using docker tag for build: {docker_tag}")

                    if example_data:
                        gpunit_params: Dict[str, Any] = {}
                        test_yml_text = effects.read_text_file(str(module_path / "test.yml"))
                        if test_yml_text is not None:
                            try:
                                import yaml
                                gpunit_doc = yaml.safe_load(test_yml_text)
                                if isinstance(gpunit_doc, dict):
                                    gpunit_params = gpunit_doc.get('params', {}) or {}
                            except Exception as e:
                                self.logger.print_status(f"Could not parse test.yml for runtime params: {e}", "WARNING")

                        runtime_cmd, volumes = await self.build_runtime_command(
                            planning_data, example_data, gpunit_params, module_path
                        )
                        if runtime_cmd:
                            extra_validation_args.extend(['-c', runtime_cmd])
                            self.logger.print_status(f"Runtime command for dockerfile test: {runtime_cmd}")
                        for vol in volumes:
                            extra_validation_args.extend(['-v', vol])
                            self.logger.print_status(f"Volume mount: {vol}")

                    if not extra_validation_args:
                        extra_validation_args = None

                elif artifact_name == 'gpunit':
                    extra_validation_args = []
                    module_name = planning_data_dict.get('module_name', '')
                    if module_name:
                        extra_validation_args.extend(['--module', module_name])
                        self.logger.print_status(f"Using module name for gpunit validation: {module_name}")

                    parameters = planning_data_dict.get('parameters', [])
                    if parameters:
                        required_params = [
                            p for p in parameters
                            if p.get('name') and p.get('required', False)
                        ]
                        if required_params:
                            required_param_names = [p['name'] for p in required_params]
                            extra_validation_args.append('--parameters')
                            extra_validation_args.extend(required_param_names)
                            self.logger.print_status(f"Using {len(required_param_names)} required parameters for gpunit validation")

                            param_types = [normalize_param_type(p.get('type', 'text')) for p in required_params]
                            extra_validation_args.append('--types')
                            extra_validation_args.extend(param_types)

                    if not extra_validation_args:
                        extra_validation_args = None

                validation_result = self.validate_artifact(str(file_path), validate_tool, extra_validation_args)

                if validation_result['success']:
                    status.artifacts_status[artifact_name]['validated'] = True
                    self.logger.print_status(f"✅ Successfully generated and validated {artifact_name}")
                    return ArtifactResult(success=True, artifact_name=artifact_name)
                else:
                    error_report = f"Validation failed: {validation_result.get('error', 'Unknown validation error')}"
                    self.logger.print_status(f"❌ {error_report}")
                    status.artifacts_status[artifact_name]['errors'].append(error_report)

                    if attempt == max_loops:
                        root_cause = classify_error(error_report, artifact_name)
                        return ArtifactResult(
                            success=False,
                            artifact_name=artifact_name,
                            error_text=error_report,
                            root_cause=root_cause,
                        )

            except Exception as e:
                error_report = f"Error generating {artifact_name}: {str(e)}"
                self.logger.print_status(error_report, "ERROR")

                tb_str = traceback.format_exc()
                self.logger.print_status(f"Full traceback:\n{tb_str}", "ERROR")

                full_error = f"{error_report}\n\nTraceback:\n{tb_str}"
                status.artifacts_status[artifact_name]['errors'].append(full_error)

                if attempt == max_loops:
                    root_cause = classify_error(full_error, artifact_name)
                    return ArtifactResult(
                        success=False,
                        artifact_name=artifact_name,
                        error_text=full_error,
                        root_cause=root_cause,
                    )

        # Fallback (should not normally reach here)
        root_cause = classify_error(error_report, artifact_name)
        return ArtifactResult(
            success=False,
            artifact_name=artifact_name,
            error_text=error_report,
            root_cause=root_cause,
        )

    def upload_to_genepattern(self, zip_path: Path, gp_server: str, gp_user: str, gp_password: str) -> bool:
        """
        Upload a module zip file to a GenePattern server.

        Args:
            zip_path: Path to the zip file to upload
            gp_server: GenePattern server URL (e.g., http://host:port/gp)
            gp_user: GenePattern username
            gp_password: GenePattern password

        Returns:
            True if upload was successful, False otherwise
        """
        self.logger.print_section("Uploading to GenePattern")
        result = effects.upload_module(str(zip_path), gp_server, gp_user, gp_password)
        self._emit(result)
        return result.success

    async def build_runtime_command(
        self,
        planning_data: ModulePlan,
        example_data: List[ExampleDataItem],
        gpunit_params: Dict[str, Any],
        module_path: Path = None,
    ) -> Tuple[Optional[str], List[str]]:
        """Build a docker runtime command and volume list for Dockerfile runtime testing.
        Delegates to dockerfile.runtime.build_runtime_command.
        """
        return await _build_runtime_command(planning_data, example_data, gpunit_params, module_path, self.logger)

    def validate_artifact(self, file_path: str, validate_tool: str, extra_args: List[str] = None) -> Dict[str, Any]:
        """Validate an artifact using its linter. Delegates to agents.validator."""
        return _validate_artifact(file_path, validate_tool, extra_args, self.logger)

    def _sync_wrapper_script(
        self,
        planning_data: 'ModulePlan',
        module_path: Path,
        status: 'ModuleGenerationStatus',
        *,
        context: str = "",
    ) -> None:
        """Ensure planning_data.wrapper_script points to a file that actually exists.

        After wrapper generation the LLM may have written a file whose name
        differs from what planning_data.wrapper_script says (e.g. the plan said
        ``run_mutect2.py`` but the file on disk is ``wrapper.py``).  This method:

        1. Checks whether ``module_path / planning_data.wrapper_script`` exists.
        2. If it does not, scans *module_path* for the most likely wrapper
           candidate (a ``.py``, ``.R``, or ``.sh`` file that is not a known
           non-wrapper name) and updates ``planning_data.wrapper_script`` to
           match.
        3. Persists the updated status to disk so all downstream steps pick up
           the corrected name.
        """
        expected_name = planning_data.wrapper_script or "wrapper.py"
        expected_path = module_path / expected_name

        ctx_prefix = f"[{context}] " if context else ""

        if expected_path.exists():
            self.logger.print_status(
                f"{ctx_prefix}wrapper_script '{expected_name}' confirmed on disk ✓"
            )
            return

        # The expected file is missing — scan for a real wrapper candidate.
        self.logger.print_status(
            f"{ctx_prefix}⚠️  wrapper_script '{expected_name}' not found in {module_path} "
            f"— scanning for actual wrapper file",
            "WARNING",
        )

        # Scan the module dir for the most likely wrapper file (deterministic).
        chosen_name = effects.find_wrapper_file(str(module_path))

        if not chosen_name:
            self.logger.print_status(
                f"{ctx_prefix}No wrapper-like file found in {module_path}; "
                f"keeping planning_data.wrapper_script='{expected_name}' unchanged",
                "WARNING",
            )
            return

        old_name = planning_data.wrapper_script
        planning_data.wrapper_script = chosen_name

        # Reflect the correction in status.planning_data if it is the same object
        if status.planning_data is planning_data:
            pass  # already updated via the shared reference
        elif status.planning_data is not None:
            status.planning_data.wrapper_script = chosen_name

        self.logger.print_status(
            f"{ctx_prefix}✅ Corrected wrapper_script: '{old_name}' → '{chosen_name}'",
            "WARNING",
        )

    def _get_manifest_docker_image(self, module_path: Path) -> Optional[str]:
        """Read job.docker.image from the manifest file, unescaping colons."""
        return effects.read_manifest_docker_image(str(module_path))

    async def generate_all_artifacts(self, tool_info: Dict[str, str], planning_data: ModulePlan, module_path: Path, status: ModuleGenerationStatus, skip_artifacts: List[str] = None, max_loops: int = MAX_ARTIFACT_LOOPS, max_escalations: int = MAX_ESCALATIONS, no_zip: bool = False, zip_only: bool = False, gp_server: Optional[str] = None, gp_user: Optional[str] = None, gp_password: Optional[str] = None) -> bool:
        """Run artifact generation phase with cross-artifact error escalation."""
        self.logger.print_section("Artifact Generation Phase")
        self.logger.print_status("Starting artifact generation")

        if skip_artifacts is None:
            skip_artifacts = []
        all_artifacts_successful = True

        artifact_queue: List[str] = [
            name for name in self.artifact_agents
            if name not in skip_artifacts
        ]
        if not no_zip and 'install' not in skip_artifacts:
            artifact_queue.append('install')
        escalation_pair_counts: Dict[tuple, int] = {}
        pending_downstream_context: Dict[str, str] = {}

        idx = 0
        while idx < len(artifact_queue):
            artifact_name = artifact_queue[idx]

            if artifact_name in skip_artifacts:
                self.logger.print_status(f"Skipping {artifact_name} (--skip-{artifact_name} specified)")
                idx += 1
                continue

            existing_status = status.artifacts_status.get(artifact_name, {})
            if existing_status.get('validated', False):
                self.logger.print_status(f"✓ {artifact_name} already validated, skipping")
                idx += 1
                continue

            self.logger.print_status(f"Generating {artifact_name}...")

            downstream_ctx = pending_downstream_context.pop(artifact_name, "")

            if artifact_name == 'install':
                result = self._run_install_artifact(
                    tool_info, planning_data, module_path, zip_only, gp_server, gp_user, gp_password
                )
            else:
                # Before starting the dockerfile step, make absolutely sure
                # planning_data.wrapper_script points to a file that exists on
                # disk.  This guards against cases where the wrapper was skipped
                # (--skip-wrapper), resumed from a prior run, or the sync above
                # was not triggered (e.g. wrapper generation failed but the run
                # is being resumed with the file already present).
                if artifact_name == 'dockerfile':
                    # Skip Dockerfile generation when the user supplied a base
                    # image AND the manifest already points to that same image —
                    # the existing image will be used directly, so no build is needed.
                    user_base_image = tool_info.get('base_image', '').strip()
                    if user_base_image:
                        manifest_image = self._get_manifest_docker_image(module_path)
                        if manifest_image and manifest_image == user_base_image:
                            self.logger.print_status(
                                f"⏭  Skipping Dockerfile generation — manifest already uses "
                                f"base image '{user_base_image}' as job.docker.image"
                            )
                            status.artifacts_status['dockerfile'] = {
                                'generated': True, 'validated': True, 'skipped': True,
                                'attempts': 0, 'errors': [],
                            }
                            idx += 1
                            continue

                    self._sync_wrapper_script(
                        planning_data, module_path, status,
                        context="pre-dockerfile assertion",
                    )
                result = await self.artifact_creation_loop(
                    artifact_name, tool_info, planning_data, module_path, status,
                    max_loops,
                    downstream_error_context=downstream_ctx,
                )

            if result.success:
                # After the wrapper is written to disk, verify planning_data.wrapper_script
                # matches the actual filename.  The LLM sometimes saves a file with a
                # different name than what the plan specified (e.g. wrapper.py vs
                # run_mutect2.py), which would cause the Dockerfile COPY to fail later.
                if artifact_name == 'wrapper':
                    self._sync_wrapper_script(
                        planning_data, module_path, status,
                        context="post-wrapper sync",
                    )
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
                    and target in self.artifact_agents
                    and target in get_upstream_dependencies(artifact_name)
                )

                if can_escalate:
                    escalation_pair_counts[pair_key] = current_count + 1

                    status.escalation_counts[pair_key[0]] = (
                        status.escalation_counts.get(pair_key[0], 0) + 1
                    )
                    escalation_event = {
                        'from_artifact': artifact_name,
                        'to_artifact': target,
                        'reason': root_cause.reason,
                        'error_snippet': result.error_text[:500],
                    }
                    status.escalation_log.append(escalation_event)

                    self.logger.print_section("Cross-Artifact Escalation")
                    self.logger.print_status(
                        f"🔀 Escalating: {artifact_name} failure → regenerating {target}",
                        "WARNING",
                    )
                    self.logger.print_status(f"   Reason: {root_cause.reason}", "WARNING")
                    self.logger.print_status(
                        f"   Escalation {current_count + 1}/{max_escalations} "
                        f"for {artifact_name}→{target}",
                    )

                    if target in status.artifacts_status:
                        status.artifacts_status[target]['validated'] = False
                        status.artifacts_status[target]['generated'] = False

                    # Build an enriched context message for manifest/wrapper escalations
                    # that includes wrapper flag details to guide alignment.
                    extra_context = ""
                    if target in ('manifest', 'wrapper'):
                        planning_dict_esc = planning_data.model_dump(mode='json') if planning_data else {}
                        wrapper_script_esc = planning_dict_esc.get('wrapper_script') or 'wrapper.py'
                        wrapper_path_esc = module_path / wrapper_script_esc
                        wrapper_src = effects.read_text_file(str(wrapper_path_esc))
                        if wrapper_src is not None:
                            try:
                                import ast as _ast
                                # Extract declared flags for the context message
                                declared_flags = []
                                try:
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
                                except Exception:
                                    pass
                                if declared_flags:
                                    extra_context = (
                                        f"\n\nWrapper script '{wrapper_script_esc}' currently declares these "
                                        f"add_argument() flags:\n"
                                        + "\n".join(f"  {f}" for f in sorted(declared_flags))
                                        + "\n\nThe manifest pN_name values and commandLine placeholders MUST "
                                        "use these exact flag names (or the wrapper must be updated to match "
                                        "the manifest's parameter names — they must be consistent)."
                                    )
                            except Exception:
                                pass

                    pending_downstream_context[target] = (
                        f"The downstream artifact '{artifact_name}' failed validation "
                        f"with the following error:\n\n"
                        f"{result.error_text[:1500]}\n\n"
                        f"Root-cause analysis: {root_cause.reason}"
                        f"{extra_context}\n\n"
                        f"You must fix the issue in THIS artifact ({target}) so that "
                        f"the downstream '{artifact_name}' step can succeed."
                    )

                    remaining = artifact_queue[idx:]
                    if target in remaining:
                        remaining.remove(target)
                    artifact_queue = (
                        artifact_queue[:idx]
                        + [target, artifact_name]
                        + [a for a in remaining if a != artifact_name]
                    )

                    escalated = True

                else:
                    if current_count >= max_escalations:
                        self.logger.print_status(
                            f"⚠️  Escalation cap reached for {artifact_name}→{target} "
                            f"({max_escalations} attempts). Marking {artifact_name} as failed.",
                            "WARNING",
                        )

            if not escalated:
                self.logger.print_status(
                    f"❌ Failed to generate {artifact_name} after {max_loops} attempts"
                )
                all_artifacts_successful = False

                # When manifest or wrapper fails, pre-warn the dockerfile agent
                # because the runtime validation command is built from the on-disk
                # manifest — if the manifest is broken the dockerfile test will also
                # fail for the wrong reason (mismatched arg names, not a Docker issue).
                if artifact_name in ('manifest', 'wrapper'):
                    failure_summary = result.error_text[:1500] if result.error_text else ""
                    existing_ctx = pending_downstream_context.get('dockerfile', '')
                    new_ctx = (
                        f"WARNING: The '{artifact_name}' artifact failed validation. "
                        f"The dockerfile runtime test command is derived from the manifest "
                        f"commandLine — if the manifest has wrong parameter names the "
                        f"runtime test will fail with 'unrecognized arguments' even if the "
                        f"Dockerfile itself is correct. "
                        f"DO NOT attempt to fix this by changing the Dockerfile. "
                        f"The wrapper's add_argument() flags and the manifest pN_name values "
                        f"must be made consistent first.\n\n"
                        f"{artifact_name} failure details:\n{failure_summary}"
                    )
                    if existing_ctx:
                        pending_downstream_context['dockerfile'] = existing_ctx + "\n\n" + new_ctx
                    else:
                        pending_downstream_context['dockerfile'] = new_ctx

                # Abort the remaining pipeline — there is no point generating further
                # artifacts when one has permanently failed, because downstream artifacts
                # depend on this one (or the module is simply incomplete).
                # Zipping, upload, and install are also skipped.
                remaining_queue = artifact_queue[idx + 1:]
                if remaining_queue:
                    skipped_names = ', '.join(remaining_queue)
                    self.logger.print_status(
                        f"⛔ Aborting pipeline: skipping remaining artifact(s): {skipped_names}",
                        "WARNING",
                    )
                break

        return all_artifacts_successful

    def docker_push(self, planning_data: ModulePlan) -> bool:
        """Push the built Docker image to Docker Hub."""
        self.logger.print_section("Docker Push")

        planning_dict = planning_data.model_dump(mode='json') if planning_data else {}
        tag = planning_dict.get('docker_image_tag', '')

        if not tag:
            self.logger.print_status("No docker_image_tag found in planning data, cannot push", "ERROR")
            return False

        result = effects.docker_push(tag)
        self._emit(result)
        return result.success

    def zip_artifacts(self, module_path: Path, tool_name: str, planning_data: 'ModulePlan', zip_only: bool = False) -> Optional[Path]:
        """Zip all artifact files into {module_name}.zip at the top level."""
        self.logger.print_section("Zipping Artifacts")
        self.logger.print_status("Creating zip archive of artifact files")

        # Compute the member list here (coordination) and hand it to the effect.
        members = ['manifest', 'paramgroups.json', 'test.yml', 'README.md', 'Dockerfile']
        wrapper_script = planning_data.wrapper_script if planning_data else None
        if wrapper_script:
            members.append(wrapper_script)

        zip_name = f"{tool_name.lower().replace(' ', '_').replace('-', '_')}.zip"
        result = effects.zip_artifacts(str(module_path), zip_name, members, zip_only)
        self._emit(result)

        if not result.ok or not result.zip_path:
            return None
        return Path(result.zip_path)

    def _run_install_artifact(
        self,
        tool_info: Dict[str, str],
        planning_data: 'ModulePlan',
        module_path: Path,
        zip_only: bool,
        gp_server: Optional[str],
        gp_user: Optional[str],
        gp_password: Optional[str],
    ) -> 'ArtifactResult':
        """Zip artifacts and optionally upload to GenePattern as a pseudo-artifact."""
        zip_path = self.zip_artifacts(module_path, tool_info['name'], planning_data, zip_only)
        if zip_path is None:
            return ArtifactResult(
                success=False,
                artifact_name='install',
                error_text="Failed to create zip archive.",
                root_cause=RootCause(
                    target_artifact='manifest',
                    reason="Zip creation failed; manifest or paramgroups may be invalid.",
                    original_artifact='install',
                ),
            )

        if not (gp_server and gp_user):
            # No upload configured — zip success is sufficient
            return ArtifactResult(success=True, artifact_name='install')

        upload_ok = self.upload_to_genepattern(zip_path, gp_server, gp_user, gp_password)
        if upload_ok:
            return ArtifactResult(success=True, artifact_name='install')

        return ArtifactResult(
            success=False,
            artifact_name='install',
            error_text=f"GenePattern upload failed for {zip_path.name}.",
            root_cause=RootCause(
                target_artifact='manifest',
                reason="GenePattern module install failed. The manifest or paramgroups may be invalid.",
                original_artifact='install',
            ),
        )

    def print_final_report(self, status: ModuleGenerationStatus):
        """Print comprehensive final report"""
        self.logger.print_section("Final Report")

        print(f"Tool Name: {status.tool_name}")
        print(f"Module Directory: {status.module_directory}")
        print(f"Research Complete: {'✓' if status.research_complete else '❌'}")
        print(f"Planning Complete: {'✓' if status.planning_complete else '❌'}")

        print(f"\nArtifact Status:")
        for artifact_name, artifact_status in status.artifacts_status.items():
            generated = "✓" if artifact_status.get('generated') else "❌"
            validated = "✓" if artifact_status.get('validated') else "❌"
            attempts = artifact_status.get('attempts', 0)
            skipped = " (skipped)" if artifact_status.get('skipped') else ""

            print(f"  {artifact_name}:")
            print(f"    Generated: {generated} | Validated: {validated} | Attempts: {attempts}{skipped}")

            if artifact_status.get('errors'):
                print(f"    Errors: {len(artifact_status['errors'])}")
                for error in artifact_status['errors'][:2]:
                    print(f"      - {error}")

        if status.parameters:
            print(f"\nParameters Identified: {len(status.parameters)}")
            for i, param in enumerate(status.parameters[:5]):
                name = param.name
                param_type = param.type.value if hasattr(param.type, 'value') else str(param.type)
                required = 'Required' if param.required else 'Optional'
                print(f"  - {name}: {param_type} ({required})")

            if len(status.parameters) > 5:
                print(f"  ... and {len(status.parameters) - 5} more parameters")

        module_path = Path(status.module_directory)
        if module_path.exists():
            print(f"\nGenerated Files:")
            for file in module_path.iterdir():
                if file.is_file():
                    size = file.stat().st_size
                    print(f"  - {file.name} ({size:,} bytes)")

        if status.input_tokens > 0 or status.output_tokens > 0:
            total_tokens = status.input_tokens + status.output_tokens
            estimated_cost = status.get_estimated_cost()
            print(f"\nToken Usage:")
            print(f"  Input tokens:  {status.input_tokens:,}")
            print(f"  Output tokens: {status.output_tokens:,}")
            print(f"  Total tokens:  {total_tokens:,}")
            print(f"  Estimated cost: ${estimated_cost:.4f}")

        if status.escalation_log:
            print(f"\nCross-Artifact Escalations: {len(status.escalation_log)}")
            for evt in status.escalation_log:
                print(f"  🔀 {evt['from_artifact']} → {evt['to_artifact']}: {evt['reason'][:120]}")

        all_artifacts_valid = all(
            artifact['generated'] and artifact['validated']
            for artifact in status.artifacts_status.values()
        )
        overall_success = (
            status.research_complete
            and status.planning_complete
            and all_artifacts_valid
        )

        print(f"\n{'='*60}")
        if overall_success:
            print("🎉 MODULE GENERATION SUCCESSFUL!")
            print(f"Your GenePattern module is ready in: {status.module_directory}")
        else:
            print("❌ MODULE GENERATION FAILED")
            print("Check the error messages above for details.")
            if status.error_messages:
                print("Errors encountered:")
                for error in status.error_messages:
                    print(f"  - {error}")

    async def run(self, tool_info: Dict[str, str] = None, skip_artifacts: List[str] = None, max_loops: int = MAX_ARTIFACT_LOOPS, no_zip: bool = False, zip_only: bool = False, docker_push: bool = False, example_data: List[ExampleDataItem] = None, max_escalations: int = MAX_ESCALATIONS, gp_server: str = None, gp_user: str = None, gp_password: str = None) -> int:
        """Run the complete module generation process"""

        self.logger.print_status(f"Generating module for: {tool_info['name']}")
        module_path = self.create_module_directory(
            tool_info['name'],
            module_dir=tool_info.get('module_dir', ''),
        )
        status = ModuleGenerationStatus(
            tool_name=tool_info['name'],
            module_directory=str(module_path),
            example_data=example_data or [],
        )
        tool_info['example_data'] = status.example_data

        if status.example_data:
            self.download_url_data(status.example_data, module_path)

        # Phase 1: Research
        if status.research_complete:
            self.logger.print_section("Research Phase")
            self.logger.print_status("✓ Research already complete, using existing data", "SUCCESS")
        else:
            research_success, research_data = await self.do_research(tool_info, status)
            if research_success:
                status.research_data = research_data
            else:
                status.error_messages.append(research_data.get('error', 'Research failed'))
            if status.research_data:
                effects.write_text_file(str(module_path / "research.md"), status.research_data.get('research', ''))

        if not status.research_complete:
            self.print_final_report(status)
            return 1

        # Phase 2: Planning
        if status.planning_complete:
            self.logger.print_section("Planning Phase")
            self.logger.print_status("✓ Planning already complete, using existing plan", "SUCCESS")
        else:
            planning_success, planning_data = await self.do_planning(tool_info, status.research_data, status, module_path=module_path)
            if planning_success:
                status.planning_data = planning_data
            else:
                status.error_messages.append("Planning failed")
            if status.planning_data:
                effects.write_text_file(str(module_path / "plan.md"), status.planning_data.plan)

        if not status.planning_complete:
            self.print_final_report(status)
            return 1

        # Phase 3: Artifact Generation
        if skip_artifacts is None:
            skip_artifacts = []

        for artifact_name, artifact_status in status.artifacts_status.items():
            if artifact_status.get('validated', False):
                if artifact_name not in skip_artifacts:
                    skip_artifacts.append(artifact_name)
                    self.logger.print_status(f"✓ {artifact_name} already completed, skipping")

        artifacts_success = await self.generate_all_artifacts(
            tool_info, status.planning_data, module_path, status,
            skip_artifacts, max_loops, max_escalations,
            no_zip=no_zip, zip_only=zip_only,
            gp_server=gp_server, gp_user=gp_user, gp_password=gp_password,
        )

        # Clean up downloaded data/ directory after successful dockerfile step
        dockerfile_validated = status.artifacts_status.get('dockerfile', {}).get('validated', False)
        if dockerfile_validated:
            self.cleanup_data_dir(module_path)

        # Phase 5: Docker push (if enabled)
        if artifacts_success and docker_push:
            self.docker_push(status.planning_data)

        self.print_final_report(status)

        return 0 if (status.research_complete and status.planning_complete and artifacts_success) else 1

