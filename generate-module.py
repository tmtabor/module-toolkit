#!/usr/bin/env python
"""
GenePattern Module Generator

A multi-agent system for automatically generating GenePattern modules from bioinformatics tools.
Uses Pydantic AI to orchestrate research, planning, and artifact generation.
"""

import os
import sys
import asyncio
import traceback
import argparse
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from agents.config import DEFAULT_OUTPUT_DIR, MAX_ARTIFACT_LOOPS, MAX_ESCALATIONS, configure_telemetry
from agents.example_data import ExampleDataResolver
from agents.logger import Logger
from agents.module import ModuleAgent

# Enable telemetry with Logfire
configure_telemetry()



class GenerationScript:
    """
    Main script orchestration class for GenePattern module generation.
    Handles user input, argument parsing, and overall script coordination.
    """
    
    def __init__(self):
        """Initialize the generation script"""
        self.logger = Logger()
        self.args = None
        self.tool_info = None
        self.module_agent = None
        self.skip_artifacts = None

    def get_user_input(self) -> Dict[str, str]:
        """Prompt user for bioinformatics tool information"""
        self.logger.print_section("GenePattern Module Generator")
        print("This script will help you create a GenePattern module for a bioinformatics tool.")
        print("Please provide the following information:\n")
        
        tool_info = {}
        
        # Required fields
        tool_info['name'] = input("Tool name (e.g., 'samtools', 'bwa', 'star'): ").strip()
        if not tool_info['name']:
            print("Error: Tool name is required.")
            sys.exit(1)
        
        # Optional fields with defaults
        tool_info['version'] = input("Tool version (optional): ").strip() or "latest"
        tool_info['language'] = input("Primary language (python/r/java/c/cpp/other, optional): ").strip() or "unknown"
        tool_info['description'] = input("Brief description (optional): ").strip()
        tool_info['repository_url'] = input("Repository URL (optional): ").strip()
        tool_info['documentation_url'] = input("Documentation URL (optional): ").strip()
        tool_info['instructions'] = input("Additional instructions/context (optional): ").strip()
        tool_info['base_image'] = input("Known Docker base image (optional, e.g. 'broadinstitute/gatk:4.5.0.0'): ").strip()

        # Example data (optional)
        data_input = input("Example data files or URLs (space-separated, optional).\n"
                           "  Tip: append ::hint to clarify each file's role, e.g.:\n"
                           "    sample1.bam::tumor_sample sample2.bam::normal_sample hg38.fasta::reference\n"
                           "> ").strip()
        if data_input:
            raw_items = data_input.split()
            resolver = ExampleDataResolver(self.logger)
            tool_info['example_data'] = resolver.resolve(raw_items)
        else:
            tool_info['example_data'] = []

        return tool_info

    def parse_arguments(self):
        """Parse command line arguments"""
        parser = argparse.ArgumentParser(
            description="Generate complete GenePattern modules from bioinformatics tool information",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
                Examples:
                  # Generate all artifacts (default)
                  python generate-module.py
                
                  # Skip specific artifacts
                  python generate-module.py --skip-dockerfile --skip-gpunit
                  
                  # Generate only wrapper and manifest
                  python generate-module.py --artifacts wrapper manifest
                  
                  # Skip container-related artifacts for local development
                  python generate-module.py --skip-dockerfile
                
                Available artifacts: wrapper, manifest, paramgroups, gpunit, documentation, dockerfile
            """
        )
        
        # Tool information
        parser.add_argument('--name', type=str, help='Tool name (e.g., "samtools")')
        parser.add_argument('--version', type=str, help='Tool version')
        parser.add_argument('--language', type=str, help='Primary language (e.g., "python")')
        parser.add_argument('--description', type=str, help='Brief description of the tool')
        parser.add_argument('--repository-url', type=str, help='URL of the source code repository')
        parser.add_argument('--documentation-url', type=str, help='URL of the tool documentation')
        parser.add_argument('--instructions', type=str, help='Additional instructions and context for module generation (e.g., which features to expose, which function to call)')
        parser.add_argument('--base-image', type=str, metavar='IMAGE',
                            help='Known Docker base image to use (e.g. "broadinstitute/gatk:4.5.0.0"). '
                                 'When provided this value is written directly into the plan\'s docker_image_tag '
                                 'field and passed to the Dockerfile agent, skipping the automatic image selection.')

        # Artifact skip flags
        parser.add_argument('--skip-wrapper', action='store_true', help='Skip generating wrapper script')
        parser.add_argument('--skip-manifest', action='store_true', help='Skip generating manifest file')
        parser.add_argument('--skip-paramgroups', action='store_true', help='Skip generating paramgroups.json file')
        parser.add_argument('--skip-gpunit', action='store_true', help='Skip generating GPUnit test file')
        parser.add_argument('--skip-documentation', action='store_true', help='Skip generating README.md documentation')
        parser.add_argument('--skip-dockerfile', action='store_true', help='Skip generating Dockerfile')
        
        # Alternative: specify only artifacts to generate
        parser.add_argument('--artifacts', nargs='+', choices=['wrapper', 'manifest', 'paramgroups', 'gpunit', 'documentation', 'dockerfile', 'none'], help="Generate only specified artifacts, or 'none' to skip all (alternative to --skip-* flags)")


        # Temporal escape hatch (temporal/PHASE3.md Step 3.6): by default, fresh runs go
        # through the Temporal workflow (requires a running Temporal server + worker,
        # see temporal/worker.py).
        parser.add_argument('--legacy', action='store_true',
                            help='Run the generation pipeline in-process (the pre-Temporal Phase 1/2 path) '
                                 'instead of via a Temporal workflow.')

        # Max loops configuration
        parser.add_argument('--max-loops', type=int, metavar='X', default=MAX_ARTIFACT_LOOPS, help=f'Maximum number of generation attempts per artifact (default: {MAX_ARTIFACT_LOOPS})')

        # Max escalations configuration
        parser.add_argument('--max-escalations', type=int, metavar='N', default=MAX_ESCALATIONS, help=f'Maximum cross-artifact escalation attempts per artifact pair (default: {MAX_ESCALATIONS})')

        # Output directory
        parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR, type=str, help=f'Output directory for generated modules (default: {DEFAULT_OUTPUT_DIR})')

        # Pre-created module directory (used by the web UI to guarantee name consistency)
        parser.add_argument('--module-dir', type=str, metavar='PATH',
                            help='Use this pre-created directory as the module output directory instead of '
                                 'generating a new timestamped name under --output-dir.')

        # Zip options
        parser.add_argument('--no-zip', action='store_true', help='Skip creating a zip archive of artifact files')
        parser.add_argument('--zip-only', action='store_true', help='After creating zip archive, delete the individual artifact files (keeps only the zip)')

        # Docker push
        parser.add_argument('--docker-push', action='store_true', help='Push the Docker image to Docker Hub after building')

        # GenePattern upload
        parser.add_argument('--gp-server', type=str, metavar='URL',
                            default=os.getenv('GP_SERVER', 'https://beta.genepattern.org/gp'),
                            help='GenePattern server URL to upload the module zip to (default: https://beta.genepattern.org, or GP_SERVER env var)')
        parser.add_argument('--gp-user', type=str, metavar='USERNAME',
                            default=os.getenv('GP_USER', ''),
                            help='GenePattern username (or set GP_USER env var)')
        parser.add_argument('--gp-password', type=str, metavar='PASSWORD',
                            default=os.getenv('GP_PASSWORD', ''),
                            help='GenePattern password (or set GP_PASSWORD env var)')
        parser.add_argument('--require-upload-approval', action='store_true',
                            help='Pause the Temporal workflow before uploading to GenePattern and wait for a '
                                 'human to approve or reject it via --approve-upload/--reject-upload from '
                                 'another invocation. Only takes effect on the default (Temporal) path with '
                                 '--gp-server/--gp-user set; ignored under --legacy.')
        parser.add_argument('--approve-upload', type=str, metavar='WORKFLOW_ID', default=None,
                            help='Signal a workflow (started with --require-upload-approval) to proceed with '
                                 'its pending GenePattern upload, then exit. Does not start a new generation.')
        parser.add_argument('--reject-upload', type=str, metavar='WORKFLOW_ID', default=None,
                            help='Signal a workflow to skip its pending GenePattern upload, then exit. '
                                 'Does not start a new generation.')

        # Example data
        parser.add_argument('--data', nargs='+', metavar='PATH_OR_URL[::HINT]',
                            help='Example data files (local paths or HTTP/HTTPS URLs). '
                                 'Each entry may include an optional semantic hint after "::" '
                                 'to clarify the role of the file when multiple files share the '
                                 'same extension (e.g. sample1.bam::tumor_sample '
                                 'sample2.bam::normal_sample hg38.fasta::reference '
                                 'foo.vcf::germline_resource bar.vcf::panel_of_normals). '
                                 'Hints are shown to the LLM during planning and artifact '
                                 'generation, and are used by the runtime test to assign the '
                                 'correct file to each parameter when multiple files have the '
                                 'same extension. URLs are downloaded before planning so their '
                                 'contents can inform the LLM. Local files are used directly. '
                                 'All files are bind-mounted during the Dockerfile runtime test.')

        self.args = parser.parse_args()

    def tool_info_from_args(self):
        """Extract tool information from command line arguments"""
        self.tool_info = {
            'name': self.args.name,
            'version': self.args.version or "latest",
            'language': self.args.language or "unknown",
            'description': self.args.description or "",
            'repository_url': self.args.repository_url or "",
            'documentation_url': self.args.documentation_url or "",
            'instructions': self.args.instructions or "",
            'base_image': self.args.base_image or "",
            'example_data': [],
            'module_dir': self.args.module_dir or "",
        }
        # Resolve --data items if provided
        if self.args.data:
            resolver = ExampleDataResolver(self.logger)
            self.tool_info['example_data'] = resolver.resolve(self.args.data)

    def parse_skip_artifacts(self):
        """Determine which artifacts to skip based on command line arguments"""
        self.skip_artifacts = []
        all_artifacts = ['wrapper', 'manifest', 'paramgroups', 'gpunit', 'documentation', 'dockerfile']

        # If --artifacts specified, skip everything not in the list
        if self.args.artifacts:
            if 'none' in self.args.artifacts:
                self.skip_artifacts = all_artifacts
                self.logger.print_status("Skipping all artifact generation as '--artifacts none' was specified.")
            else:
                self.skip_artifacts = [artifact for artifact in all_artifacts if artifact not in self.args.artifacts]
                self.logger.print_status(f"Generating only: {', '.join(self.args.artifacts)}")
        else:
            # Use individual skip flags
            if self.args.skip_wrapper:       self.skip_artifacts.append('wrapper')
            if self.args.skip_manifest:      self.skip_artifacts.append('manifest')
            if self.args.skip_paramgroups:   self.skip_artifacts.append('paramgroups')
            if self.args.skip_gpunit:        self.skip_artifacts.append('gpunit')
            if self.args.skip_documentation: self.skip_artifacts.append('documentation')
            if self.args.skip_dockerfile:    self.skip_artifacts.append('dockerfile')

            if self.skip_artifacts:          self.logger.print_status(f"Skipping: {', '.join(self.skip_artifacts)}")

    async def run_via_temporal(self, example_data) -> int:
        """Start ModuleGenerationWorkflow and wait for it, then print a report.

        Requires a reachable Temporal server and a running worker
        (`uv run python -m temporal.worker`, or `python temporal/worker.py`).
        """
        # Local import: keeps --legacy/--help usable without pulling
        # in the Temporal client machinery for those paths.
        from datetime import timedelta
        from temporal.client import start_module_generation, MODULE_GENERATION_QUEUE

        tool_info = dict(self.tool_info)
        tool_info['example_data'] = [item.to_dict() for item in example_data]
        tool_info['output_dir'] = self.args.output_dir

        # The upload-approval wait (if requested) has no timeout of its own --
        # the workflow's own execution_timeout is the real outer bound on how
        # long it can sit waiting for a human. The client default (2h) is
        # almost certainly too short for that; give it a week instead.
        execution_timeout = timedelta(days=7) if self.args.require_upload_approval else None

        # Submitting (connect + start_workflow) fails fast if no server is
        # reachable.
        try:
            handle = await start_module_generation(
                tool_info,
                skip_artifacts=self.skip_artifacts,
                max_loops=self.args.max_loops,
                max_escalations=self.args.max_escalations,
                no_zip=self.args.no_zip,
                zip_only=self.args.zip_only,
                docker_push=self.args.docker_push,
                gp_server=self.args.gp_server,
                gp_user=self.args.gp_user,
                gp_password=self.args.gp_password,
                require_upload_approval=self.args.require_upload_approval,
                execution_timeout=execution_timeout,
            )
        except Exception as e:
            self.logger.print_status(
                f"Could not reach a Temporal server ({e}). Start one with "
                f"'temporal server start-dev', or use --legacy to run in-process.",
                "ERROR",
            )
            return 1

        # The workflow is now durably queued. Awaiting the result blocks until a
        # worker picks it up; if none is serving the queue it will eventually
        # hit the client's bounded execution_timeout rather than hang forever.
        self.logger.print_status(
            f"Submitted workflow '{handle.id}' to task queue '{MODULE_GENERATION_QUEUE}'. "
            f"Waiting for a worker to run it (start one with 'uv run python -m temporal.worker')..."
        )
        if self.args.require_upload_approval and self.args.gp_server and self.args.gp_user:
            self.logger.print_status(
                f"This run will pause before uploading to GenePattern. Once it reaches that "
                f"point, approve or reject it from another terminal with:\n"
                f"    python generate-module.py --approve-upload {handle.id}\n"
                f"    python generate-module.py --reject-upload {handle.id}",
            )
        try:
            result = await handle.result()
        except Exception as e:
            self.logger.print_status(
                f"Workflow did not complete ({e}). If this timed out, no worker is serving "
                f"queue '{MODULE_GENERATION_QUEUE}' -- start one with "
                f"'uv run python -m temporal.worker', or use --legacy to run in-process.",
                "ERROR",
            )
            return 1

        self._check_shared_filesystem(result.get('module_directory'))
        self.print_report_from_status_dict(result.get('status', {}))
        return 0 if result.get('success') else 1

    def _check_shared_filesystem(self, module_directory: Optional[str]) -> None:
        """Warn loudly if the worker's output directory isn't visible from here.

        The worker writes every artifact to its own local disk (temporal/CONSIDERATIONS.md
        gotcha #2); this CLI process only sees that output if it shares a filesystem with
        the worker. Silently printing "your module is ready in <path>" when that path
        doesn't exist locally is confusing -- surface the real cause instead
        (temporal/PHASE5.md Workstream A3).
        """
        if not module_directory:
            return
        if not Path(module_directory).exists():
            self.logger.print_status(
                f"The workflow reports its output at '{module_directory}', but that path "
                f"doesn't exist on this machine. The worker and this CLI must share a "
                f"filesystem (temporal/CONSIDERATIONS.md gotcha #2) -- run them on the same "
                f"host, or check that a shared volume is mounted at the same path on both.",
                "WARNING",
            )

    def print_report_from_status_dict(self, status: dict) -> None:
        """Console report for a Temporal-workflow result -- mirrors
        agents.module.ModuleAgent.print_final_report, but reads the plain
        dict a workflow returns instead of a ModuleGenerationStatus object."""
        self.logger.print_section("Final Report")

        print(f"Tool Name: {status.get('tool_name')}")
        print(f"Module Directory: {status.get('module_directory')}")
        print(f"Research Complete: {'v' if status.get('research_complete') else 'x'}")
        print(f"Planning Complete: {'v' if status.get('planning_complete') else 'x'}")

        print("\nArtifact Status:")
        for artifact_name, artifact_status in (status.get('artifacts_status') or {}).items():
            generated = 'v' if artifact_status.get('generated') else 'x'
            validated = 'v' if artifact_status.get('validated') else 'x'
            attempts = artifact_status.get('attempts', 0)
            skipped = " (skipped)" if artifact_status.get('skipped') else ""
            print(f"  {artifact_name}:")
            print(f"    Generated: {generated} | Validated: {validated} | Attempts: {attempts}{skipped}")
            if artifact_status.get('errors'):
                print(f"    Errors: {len(artifact_status['errors'])}")

        input_tokens = status.get('input_tokens', 0)
        output_tokens = status.get('output_tokens', 0)
        if input_tokens or output_tokens:
            print("\nToken Usage:")
            print(f"  Input tokens:  {input_tokens:,}")
            print(f"  Output tokens: {output_tokens:,}")
            print(f"  Estimated cost: ${status.get('estimated_cost', 0.0):.4f}")

        if status.get('escalation_log'):
            print(f"\nCross-Artifact Escalations: {len(status['escalation_log'])}")
            for evt in status['escalation_log']:
                print(f"  {evt['from_artifact']} -> {evt['to_artifact']}: {evt['reason'][:120]}")

        print(f"\n{'=' * 60}")
        artifacts_status = status.get('artifacts_status') or {}
        all_valid = all(a.get('generated') and a.get('validated') for a in artifacts_status.values())
        if status.get('research_complete') and status.get('planning_complete') and all_valid:
            print("MODULE GENERATION SUCCESSFUL!")
            print(f"Your GenePattern module is ready in: {status.get('module_directory')}")
        else:
            print("MODULE GENERATION FAILED")
            if status.get('error_messages'):
                print("Errors encountered:")
                for error in status['error_messages']:
                    print(f"  - {error}")

    async def send_upload_decision(self, workflow_id: str, approve: bool) -> int:
        """Signal a running workflow's pending upload-approval gate and exit --
        does not start a new generation (temporal/PHASE5.md Workstream D)."""
        from temporal.client import decide_upload

        try:
            sent = await decide_upload(workflow_id, approve)
        except Exception as e:
            self.logger.print_status(f"Could not reach a Temporal server ({e}).", "ERROR")
            return 1

        if not sent:
            self.logger.print_status(
                f"Could not signal workflow '{workflow_id}' -- check the ID and that the "
                f"workflow is still running.",
                "ERROR",
            )
            return 1

        verb = "approval" if approve else "rejection"
        self.logger.print_status(f"Sent {verb} signal to workflow '{workflow_id}'.", "SUCCESS")
        return 0

    def main(self):
        """Main entry point for module generation"""
        try:
            # Parse command line arguments
            self.parse_arguments()

            # --approve-upload/--reject-upload signal an existing workflow and
            # exit; they don't start a new generation, so skip everything below.
            if self.args.approve_upload:
                return asyncio.run(self.send_upload_decision(self.args.approve_upload, approve=True))
            if self.args.reject_upload:
                return asyncio.run(self.send_upload_decision(self.args.reject_upload, approve=False))

            self.parse_skip_artifacts()

            # Initialize ModuleAgent with logger and module directory
            self.module_agent = ModuleAgent(self.logger, self.args.output_dir)

            # Get tool information from args or user input
            if self.args.name:
                self.tool_info_from_args()
            else:
                self.tool_info = self.get_user_input()

            example_data = self.tool_info.pop('example_data', []) or []

            if self.args.legacy:
                # Run the generation process in-process (Phase 1/2 path)
                return asyncio.run(self.module_agent.run(
                    self.tool_info,
                    self.skip_artifacts,
                    max_loops=self.args.max_loops,
                    no_zip=self.args.no_zip,
                    zip_only=self.args.zip_only,
                    docker_push=self.args.docker_push,
                    example_data=example_data,
                    max_escalations=self.args.max_escalations,
                    gp_server=self.args.gp_server,
                    gp_user=self.args.gp_user,
                    gp_password=self.args.gp_password,
                ))

            # Default: run via the Temporal workflow (requires a running
            # Temporal server + worker -- see temporal/worker.py).
            return asyncio.run(self.run_via_temporal(example_data))

        except KeyboardInterrupt:
            self.logger.print_status("\nGeneration interrupted by user", "WARNING")
            return 1
        except Exception as e:
            self.logger.print_status(f"Unexpected error: {str(e)}", "ERROR")
            self.logger.print_status(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return 1

if __name__ == "__main__":
    script = GenerationScript()
    sys.exit(script.main())