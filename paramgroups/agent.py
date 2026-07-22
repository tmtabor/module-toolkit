import json
from typing import Annotated, List, Dict, Any
from pydantic import BeforeValidator
from pydantic_ai import Agent, RunContext
from dotenv import load_dotenv
from agents.config import MAX_ARTIFACT_LOOPS
from agents.models import configured_llm_model, ArtifactDeps, coerce_stringified_json, guard_single_call
from paramgroups.models import ParamgroupsModel


# Load environment variables from .env file
load_dotenv()


system_prompt = """
You are an expert UX designer and bioinformatician specializing in creating intuitive parameter 
organization for GenePattern modules. Your task is to generate well-structured paramgroups.json 
files that provide optimal user experience by logically grouping related parameters.

CRITICAL: Your output must ALWAYS be valid JSON only - no markdown, no explanations, no text before or after the JSON.

Key requirements for GenePattern paramgroups:
- Group related parameters together for intuitive workflows
- Create clear, descriptive group names that users understand
- Organize from most essential to least essential parameters
- Balance group sizes (avoid too many small groups or overly large groups)
- Consider parameter dependencies and typical usage patterns
- Use appropriate group descriptions to guide users
- Mark advanced/expert groups as hidden when appropriate

Paramgroups Structure:
- JSON array of group objects
- Each group: {"name": "string", "description": "string", "hidden": boolean, "parameters": ["array"]}
- Required fields: name, parameters
- Optional fields: description, hidden

Best Practices:
- Start with "Required" or "Basic" parameters group
- Group by functional area (Input/Output, Analysis Options, Quality Control, etc.)
- Use clear, non-technical group names when possible
- Provide helpful descriptions that explain the group's purpose
- Hide complex/advanced groups by default (hidden: true)
- Ensure every parameter appears in exactly one group
- Order groups by typical workflow sequence
- have at most 5 Paramgroups
- Limit parameters per group to 10 or fewer for usability

REMEMBER: Output ONLY valid JSON. No explanations, no markdown, no additional text.

CRITICAL: call the create_paramgroups tool EXACTLY ONCE. It returns grouping analysis and
the full parameter list -- everything you need. Do NOT call it again to "double check" or
"regenerate"; use what it returned to write your own final structured paramgroups output
directly. Calling it repeatedly wastes your turn budget without producing new information.
"""

# Create agent without MCP dependency
paramgroups_agent = Agent(configured_llm_model(), instructions=system_prompt, output_type=ParamgroupsModel, deps_type=ArtifactDeps, retries=MAX_ARTIFACT_LOOPS)


@paramgroups_agent.instructions
def paramgroups_context_instructions(ctx: RunContext[ArtifactDeps]) -> str:
    """Inject per-call context into the paramgroups agent's instructions."""
    deps = ctx.deps
    tool_info = deps.tool_info
    planning_data = deps.planning_data or {}

    lines = []
    lines.append(
        f"You are generating the PARAMGROUPS artifact for GenePattern module '{tool_info.get('name', 'unknown')}'. "
        f"This is attempt {deps.attempt} of {deps.max_loops}."
    )

    if tool_info.get('instructions'):
        lines.append(f"\nIMPORTANT — Additional Instructions:\n{tool_info['instructions']}")

    if deps.example_data:
        distinct_exts = list(dict.fromkeys(
            item.get('extension', '') for item in deps.example_data if item.get('extension')
        ))
        if len(distinct_exts) >= 2:
            ex_lines = ["\nExample Data Provided:"]
            for item in deps.example_data:
                kind = "URL" if item.get('is_url') else "local file"
                hint = f" [hint: {item['hint']}]" if item.get('hint') else ""
                ex_lines.append(f"- {item.get('filename', '')} ({item.get('extension', '')}) — {kind}{hint}")
            ex_lines.append("These files represent distinct input roles. Keep related parameters in the same group.")
            lines.append("\n".join(ex_lines))

    if deps.error_history:
        history_lines = ["Previous attempt errors (avoid repeating these mistakes):"]
        for i, err in enumerate(deps.error_history, 1):
            history_lines.append(f"\nAttempt {i} error:\n{err}")
        lines.append("\n" + "\n".join(history_lines))

    if deps.downstream_error_context:
        lines.append(
            "\n⚠️  CROSS-ARTIFACT ESCALATION — READ CAREFULLY ⚠️\n"
            + deps.downstream_error_context
            + "\n\nYou MUST address the issue described above."
        )

    return "\n".join(lines)


@paramgroups_agent.tool
def validate_paramgroups(context: RunContext[ArtifactDeps], path: str, parameters: List[str] | None = None) -> str:
    """
    Validate GenePattern paramgroups.json files.

    This tool validates paramgroups.json files that define parameter groupings
    and UI layout for GenePattern modules. These files control how parameters
    are organized and displayed in the GenePattern web interface.

    Args:
        path: Path to the paramgroups.json file to validate. The file should
              contain valid JSON with parameter group definitions, including
              group names, descriptions, and parameter memberships.
        parameters: Optional list of parameter names that should be included
                   in the parameter groups. If provided, validates that all
                   specified parameters are properly assigned to groups and
                   that no orphaned parameters exist.

    Returns:
        A string containing the validation results, indicating whether the
        paramgroups.json file is properly formatted and contains valid parameter
        groupings, along with any JSON syntax errors or logical inconsistencies.
    """
    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout
    import traceback

    print(f"🔍 PARAMGROUPS TOOL: Running validate_paramgroups on '{path}'")

    try:
        import paramgroups.linter

        argv = [path]
        if parameters and isinstance(parameters, list):
            argv.extend(["--parameters"] + parameters)

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        try:
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exit_code = paramgroups.linter.main(argv)

            output = stdout_capture.getvalue()
            errors = stderr_capture.getvalue()
            result_text = f"Paramgroups validation {'PASSED' if exit_code == 0 else 'FAILED'}\n\n{output}"
            if errors:
                result_text += f"\nErrors:\n{errors}"
            return result_text
        except SystemExit as e:
            exit_code = e.code if e.code is not None else 0
            output = stdout_capture.getvalue()
            errors = stderr_capture.getvalue()
            result_text = f"Paramgroups validation {'PASSED' if exit_code == 0 else 'FAILED'}\n\n{output}"
            if errors:
                result_text += f"\nErrors:\n{errors}"
            return result_text
    except Exception as e:
        error_msg = f"Error running paramgroups linter: {str(e)}\n{traceback.format_exc()}"
        print(f"❌ PARAMGROUPS TOOL: {error_msg}")
        return error_msg


@paramgroups_agent.tool
def analyze_parameter_groupings(context: RunContext[ArtifactDeps], parameters: Annotated[List[Dict[str, Any]], BeforeValidator(coerce_stringified_json)], group_strategy: str = "functional") -> str:
    """
    Analyze a list of parameters and suggest optimal groupings for paramgroups.json.
    
    Args:
        parameters: List of parameter dictionaries with 'name', 'type', 'required', 'description' fields
        group_strategy: Grouping strategy ('functional', 'workflow', 'complexity', 'alphabetical')
    
    Returns:
        Analysis of suggested parameter groupings with rationale
    """
    print(f"📋 PARAMGROUPS TOOL: Running analyze_parameter_groupings with {len(parameters)} parameters (strategy: {group_strategy})")
    
    if not parameters:
        print("❌ PARAMGROUPS TOOL: analyze_parameter_groupings failed - no parameters provided")
        return "Error: No parameters provided for grouping analysis"
    
    analysis = f"Parameter Grouping Analysis ({group_strategy} strategy):\n"
    analysis += "=" * 50 + "\n\n"
    
    # Categorize parameters based on strategy
    groups = {}
    
    if group_strategy == "functional":
        # Group by functional categories
        groups = {
            "Required Parameters": [],
            "Input/Output": [],
            "Analysis Options": [],
            "Quality Control": [],
            "Advanced Settings": [],
            "System Parameters": []
        }
        
        for param in parameters:
            name = param.get('name', '')
            param_type = param.get('type', '')
            required = param.get('required', False)
            description = param.get('description', '').lower()
            
            # Categorization logic
            if required or any(term in name.lower() for term in ['input', 'output'] if required):
                groups["Required Parameters"].append(param)
            elif any(term in name.lower() for term in ['input', 'file', 'data', 'output', 'result']):
                groups["Input/Output"].append(param)
            elif any(term in description for term in ['quality', 'filter', 'threshold', 'cutoff']):
                groups["Quality Control"].append(param)
            elif any(term in name.lower() for term in ['thread', 'memory', 'cpu', 'timeout', 'debug']):
                groups["System Parameters"].append(param)
            elif any(term in description for term in ['advanced', 'expert', 'optional']):
                groups["Advanced Settings"].append(param)
            else:
                groups["Analysis Options"].append(param)
    
    elif group_strategy == "workflow":
        # Group by typical workflow sequence
        groups = {
            "Data Input": [],
            "Processing Options": [],
            "Output Configuration": [],
            "Post-processing": []
        }
        
        for param in parameters:
            name = param.get('name', '').lower()
            if any(term in name for term in ['input', 'data', 'file', 'source']):
                groups["Data Input"].append(param)
            elif any(term in name for term in ['output', 'result', 'save', 'export']):
                groups["Output Configuration"].append(param)
            elif any(term in name for term in ['post', 'final', 'summary', 'report']):
                groups["Post-processing"].append(param)
            else:
                groups["Processing Options"].append(param)
    
    elif group_strategy == "complexity":
        # Group by complexity level
        groups = {
            "Basic Parameters": [],
            "Intermediate Options": [],
            "Advanced Configuration": []
        }
        
        for param in parameters:
            required = param.get('required', False)
            description = param.get('description', '').lower()
            name = param.get('name', '').lower()
            
            if required or any(term in name for term in ['input', 'output', 'method']):
                groups["Basic Parameters"].append(param)
            elif any(term in description for term in ['advanced', 'expert', 'complex']):
                groups["Advanced Configuration"].append(param)
            else:
                groups["Intermediate Options"].append(param)
    
    elif group_strategy == "alphabetical":
        # Simple alphabetical grouping
        groups = {
            "A-F": [],
            "G-M": [],
            "N-S": [],
            "T-Z": []
        }
        
        for param in parameters:
            name = param.get('name', '')
            if name:
                first_letter = name[0].upper()
                if first_letter <= 'F':
                    groups["A-F"].append(param)
                elif first_letter <= 'M':
                    groups["G-M"].append(param)
                elif first_letter <= 'S':
                    groups["N-S"].append(param)
                else:
                    groups["T-Z"].append(param)
    
    # Generate analysis output
    analysis += f"**Suggested Groups ({len([g for g in groups.values() if g])} groups):**\n\n"
    
    total_params = 0
    for group_name, group_params in groups.items():
        if group_params:
            total_params += len(group_params)
            analysis += f"**{group_name}** ({len(group_params)} parameters):\n"
            
            # Show parameter details
            for param in group_params[:5]:  # Limit to first 5 for readability
                name = param.get('name', 'Unknown')
                param_type = param.get('type', 'Unknown')
                required = param.get('required', False)
                status = "Required" if required else "Optional"
                analysis += f"  - {name} ({param_type}, {status})\n"
            
            if len(group_params) > 5:
                analysis += f"  ... and {len(group_params) - 5} more parameters\n"
            
            # Suggest group properties
            has_required = any(p.get('required', False) for p in group_params)
            should_hide = group_name in ["Advanced Settings", "System Parameters", "Advanced Configuration"]
            
            analysis += f"  → Suggested hidden: {should_hide}\n"
            analysis += f"  → Contains required parameters: {has_required}\n\n"
    
    # Grouping recommendations
    analysis += "**Recommendations:**\n"
    analysis += "- Use these groupings to structure your paramgroups.json file.\n"
    analysis += "- The 'Advanced' and 'System' groups are good candidates for `\"hidden\": true`.\n"
    analysis += "- Ensure all parameters from the plan are included in the final JSON.\n"

    print(f"✅ PARAMGROUPS TOOL: analyze_parameter_groupings completed successfully")
    return analysis


@paramgroups_agent.tool
@guard_single_call
def create_paramgroups(context: RunContext[ArtifactDeps]) -> str:
    """
    Generate a valid paramgroups.json file based on the provided tool information and planning data.

    Args:
        context: RunContext with dependencies containing tool_info, planning_data, error_report, and attempt

    Returns:
        A string containing the complete and valid paramgroups.json content.
    """
    # Extract data from context dependencies
    tool_info = context.deps.tool_info
    planning_data_raw = context.deps.planning_data or {}
    error_report = context.deps.error_report
    attempt = context.deps.attempt

    print(f"📋 PARAMGROUPS TOOL: Running create_paramgroups for {tool_info.get('name', 'Unknown Tool')} (attempt {attempt})")

    # Extract tool information including instructions
    tool_instructions = tool_info.get('instructions', '')

    if tool_instructions:
        print(f"✓ User provided instructions: {tool_instructions[:100]}...")

    # planning_data is always a dict (or None) via ArtifactDeps
    planning_data_dict: Dict[str, Any] = planning_data_raw if isinstance(planning_data_raw, dict) else {}
    if not planning_data_dict.get('parameters'):
        print("⚠️ PARAMGROUPS TOOL: No parameters found in planning_data. Generating empty paramgroups.")
        return "[]"
    parameters = planning_data_dict['parameters']

    # Use the analyze_parameter_groupings tool to get a suggested structure
    grouping_analysis = analyze_parameter_groupings(context, parameters)

    # Build the generation prompt with all the necessary information
    generation_info = f"""
Tool Information:
- Name: {tool_info.get('name')}
- Version: {tool_info.get('version', 'unknown')}
- Description: {tool_info.get('description', 'No description provided')}

Parameters to Group ({len(parameters)} total):
{json.dumps(parameters, indent=2)}

Grouping Analysis:
{grouping_analysis}

Previous Error Report: {error_report if error_report else "None"}
Attempt Number: {attempt}

Generate a valid paramgroups.json file that groups these {len(parameters)} parameters logically.
Each parameter must appear in exactly one group. Output ONLY valid JSON - no markdown, no explanations.
"""

    print(f"✅ PARAMGROUPS TOOL: create_paramgroups completed successfully")
    return generation_info
