#!/usr/bin/env python
"""
Generate planner training data (prompt + ModulePlan JSON) from existing
manifest training pairs (prompts/*.txt + artifacts/*.properties).

For each manifest prompt/artifact pair, this script:
1. Parses the manifest prompt to extract tool info and planning data
2. Parses the manifest .properties file to extract parameter details
3. Generates a planner-style user prompt (training/planner/prompts/{name}.txt)
4. Generates the corresponding ModulePlan JSON (training/planner/artifacts/{name}.json)

The output structure matches the format captured in training/planner/examples/*.jsonl.
"""

import ast
import json
import os
import random
import re
import sys
from pathlib import Path

MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"
PROMPTS_IN = MANIFESTS_DIR / "prompts"
ARTIFACTS_IN = MANIFESTS_DIR / "artifacts"
PROMPTS_OUT = Path(__file__).parent / "prompts"
ARTIFACTS_OUT = Path(__file__).parent / "artifacts"


def parse_manifest_prompt(text: str) -> dict:
    """Parse a manifest prompt .txt file to extract tool info and planning data."""
    info = {}

    # Extract tool name from first line
    m = re.search(r"manifest for (.+)\.", text)
    info["name"] = m.group(1).strip() if m else ""

    # Extract fields
    for field, key in [
        (r"- Name:\s*(.+)", "name"),
        (r"- Version:\s*(.+)", "version"),
        (r"- Language:\s*(.+)", "language"),
        (r"- Description:\s*(.+)", "description"),
        (r"- Repository:\s*(.*)", "repository"),
    ]:
        m = re.search(field, text)
        if m:
            info[key] = m.group(1).strip()

    # Extract planning data dict
    pd_match = re.search(r"Planning Data:\s*\n(.+?)(?:\n\nGenerate|\Z)", text, re.DOTALL)
    if pd_match:
        raw = pd_match.group(1).strip()
        try:
            info["planning_data"] = ast.literal_eval(raw)
        except Exception:
            # Try fixing common issues: escaped characters
            raw_fixed = raw.replace("\\=", "=").replace("\\:", ":")
            try:
                info["planning_data"] = ast.literal_eval(raw_fixed)
            except Exception as e:
                print(f"  WARNING: Could not parse planning_data: {e}", file=sys.stderr)
                info["planning_data"] = {}
    else:
        info["planning_data"] = {}

    return info


def parse_properties(path: Path) -> dict:
    """Parse a GenePattern .properties manifest file into a dict."""
    props = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        continuation = ""
        for line in f:
            line = line.rstrip("\n")
            # Skip comments
            if line.startswith("#"):
                continue
            # Handle continuation lines
            if continuation:
                line = continuation + line
                continuation = ""
            if line.endswith("\\"):
                continuation = line[:-1]
                continue
            # Parse key=value
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    return props


def detect_language_from_command(command_line: str, language_field: str) -> str:
    """Detect wrapper script language from the command line."""
    if not command_line:
        return language_field or "unknown"
    cmd = command_line.lower()
    if "rscript" in cmd or cmd.strip().startswith("r "):
        return "R"
    if "python" in cmd:
        return "Python"
    if "perl" in cmd:
        return "Perl"
    if "java " in cmd or "java.exe" in cmd:
        return "Java"
    if "bash" in cmd or cmd.startswith("/bin/"):
        return "Shell"
    return language_field or "unknown"


def detect_wrapper_script(command_line: str, language: str) -> str:
    """Extract or infer wrapper script name from command line."""
    if not command_line:
        return "wrapper.py"
    # Look for common patterns like <libdir>something.py or /path/to/script.R
    m = re.search(r"<libdir>(\S+)", command_line)
    if m:
        return m.group(1)
    # Look for script file after Rscript/python/perl/bash
    m = re.search(r"(?:Rscript|python|perl|bash)\s+(?:--\S+\s+)*(\S+\.(?:R|py|pl|sh))", command_line, re.I)
    if m:
        return os.path.basename(m.group(1))
    # Look for any file with a script extension
    m = re.search(r"(/\S+\.(?:R|py|pl|sh))", command_line, re.I)
    if m:
        return os.path.basename(m.group(1))
    lang_lower = language.lower() if language else "python"
    ext_map = {"r": ".R", "python": ".py", "perl": ".pl", "java": ".sh", "shell": ".sh"}
    ext = ext_map.get(lang_lower, ".py")
    return f"wrapper{ext}"


def map_param_type(planning_type: str) -> str:
    """Map manifest/planning parameter types to ModulePlan types."""
    t = (planning_type or "").upper().strip()
    if t in ("FILE", "JAVA.IO.FILE"):
        return "file"
    if t in ("INTEGER", "INT", "JAVA.LANG.INTEGER"):
        return "integer"
    if t in ("FLOAT", "DOUBLE", "JAVA.LANG.FLOAT", "JAVA.LANG.DOUBLE"):
        return "float"
    if t in ("TEXT", "JAVA.LANG.STRING", "STRING"):
        return "text"
    if t == "CHOICE":
        return "choice"
    return "text"


def parse_choices_string(choices_str: str) -> list:
    """Parse a choices string like 'val1=Display 1;val2=Display 2' into choice objects."""
    if not choices_str:
        return None
    choices = []
    for item in choices_str.split(";"):
        item = item.strip().replace("\\=", "=").replace("\\:", ":")
        if "=" in item:
            value, _, display = item.partition("=")
            choices.append({"display": display.strip(), "value": value.strip()})
        elif item:
            choices.append({"display": item, "value": item})
    return choices if choices else None


def build_parameters(planning_data: dict, props: dict) -> list:
    """Build the parameters list for ModulePlan from planning data and properties."""
    parameters = []
    plan_params = planning_data.get("parameters", [])

    for i, pp in enumerate(plan_params, 1):
        name = pp.get("name", "")
        raw_type = pp.get("type", "TEXT")
        p_type = map_param_type(raw_type)

        # Check if there are choices, which makes this a choice parameter
        choices_raw = pp.get("choices", "")
        if choices_raw:
            choices_raw = choices_raw.replace("\\=", "=").replace("\\:", ":")
        choices = parse_choices_string(choices_raw)
        if choices and p_type == "text":
            p_type = "choice"

        # Check properties file for additional info
        prop_key = f"p{i}_"
        file_format = pp.get("file_format", "")
        if not file_format:
            file_format = props.get(f"p{i}_fileFormat", "")
        file_format = file_format.replace("\\", "")

        file_formats = None
        if p_type == "file" and file_format:
            file_formats = [f.strip() for f in file_format.replace(";", ",").split(",") if f.strip()]
            # Remove leading dots
            file_formats = [f.lstrip(".") for f in file_formats]

        # Determine required / value_count
        required = pp.get("required", True)
        if isinstance(required, str):
            required = required.lower() not in ("false", "no", "0", "")

        num_values = pp.get("num_values", "1..1")
        if num_values not in ("0..1", "1..1", "0+", "1+"):
            num_values = "0..1" if not required else "1..1"

        default_value = pp.get("default_value", None)
        if default_value == "":
            default_value = None

        # Build prefix from planning data or properties
        prefix = pp.get("prefix", "")
        if not prefix:
            prop_prefix = props.get(f"p{i}_prefix_when_specified", "")
            if prop_prefix:
                prefix = prop_prefix.replace("\\", "")
            else:
                prefix = f"--{name}"

        prefix_only_if_value = not required and num_values in ("0..1", "0+")

        param = {
            "name": name,
            "description": pp.get("description", ""),
            "required": required,
            "type": p_type,
            "value_count": num_values,
            "default_value": default_value,
            "file_formats": file_formats,
            "choices": choices,
            "accept_user_values": None,
            "prefix": prefix,
            "prefix_only_if_value": prefix_only_if_value,
        }
        parameters.append(param)

    return parameters


def extract_file_formats(parameters: list) -> list:
    """Extract unique input file formats from parameters."""
    formats = set()
    for p in parameters:
        if p.get("type") == "file" and p.get("file_formats"):
            for fmt in p["file_formats"]:
                if fmt and fmt.upper() not in ("DIRECTORY",):
                    formats.add(fmt)
    return sorted(formats) if formats else ["txt"]


def generate_lsid() -> str:
    """Generate a random LSID in GenePattern format."""
    random_id = random.randint(10000, 99999)
    return f"urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:{random_id}:1"


def detect_categories(planning_data: dict, description: str) -> list:
    """Detect module categories from planning data and description."""
    cats = planning_data.get("categories", "")
    if isinstance(cats, list):
        return cats if cats else ["Analysis"]
    if cats:
        # Split on comma or semicolon
        return [c.strip() for c in re.split(r"[;,]", cats) if c.strip()]
    # Try to infer from description
    desc_lower = (description or "").lower()
    if "rna-seq" in desc_lower or "rna seq" in desc_lower:
        return ["RNA-seq"]
    if "single-cell" in desc_lower or "single cell" in desc_lower:
        return ["Single-Cell Analysis"]
    if "variant" in desc_lower or "snp" in desc_lower:
        return ["Variant Analysis"]
    if "alignment" in desc_lower or "mapping" in desc_lower:
        return ["Sequence Analysis"]
    return ["Analysis"]


def build_plan_text(info: dict, planning_data: dict, parameters: list) -> str:
    """Generate a plan text summary for the ModulePlan."""
    name = planning_data.get("module_name", info.get("name", "Unknown"))
    description = info.get("description", "")
    command_line = planning_data.get("command_line", "")

    param_names = [p["name"] for p in parameters]
    required_params = [p["name"] for p in parameters if p.get("required")]
    optional_params = [p["name"] for p in parameters if not p.get("required")]

    lines = [
        f"## {name} GenePattern Module — Implementation Plan",
        "",
        "### 1. Overview",
        f"{name} is a GenePattern module that {description}" if description else f"{name} is a GenePattern module.",
        "",
        "### 2. Parameters",
        f"The module exposes {len(parameters)} parameters:",
        f"- Required: {', '.join(required_params) if required_params else 'none'}",
        f"- Optional: {', '.join(optional_params) if optional_params else 'none'}",
        "",
        "### 3. Command Line",
        f"```",
        f"{command_line}" if command_line else "See wrapper script",
        f"```",
        "",
        "### 4. Docker Image",
        f"Docker image: {planning_data.get('docker_image_tag', 'Not specified')}",
        "",
        "### 5. Validation Strategy",
        "- Validate all required parameters are provided",
        "- Validate file formats match expected extensions",
        "- Check parameter value ranges where applicable",
        "",
        "### 6. Implementation Roadmap",
        "Phase 1: Wrapper script implementation",
        "Phase 2: Docker container setup",
        "Phase 3: Parameter validation and error handling",
        "Phase 4: Testing and documentation",
    ]
    return "\n".join(lines)


def build_instruction(info: dict) -> str:
    """Build the planner-style instruction prompt from tool info."""
    name = info.get("name", "Unknown")
    version = info.get("version", "latest")
    language = info.get("language", "unknown")
    description = info.get("description", "")

    planning_data = info.get("planning_data", {})
    planning_str = json.dumps(planning_data, indent=2)

    prompt = f"""Create a comprehensive structured plan for the GenePattern module for '{name}'.
            
            Tool Information:
            - Name: {name}
            - Version: {version}
            - Language: {language}
            - Description: {description}
            
            Research Results:
            The following planning data has been gathered from existing module specifications:

{planning_str}
            
            Please create a structured ModulePlan with:
            1. Detailed parameter definitions with types and descriptions
            2. Module architecture recommendations
            3. Integration strategy for GenePattern
            4. Validation and testing approach
            5. Implementation roadmap
            
            If an author name is not provided, use 'GenePattern Team'.
            
            Focus on creating actionable specifications for module development."""

    return prompt


def process_pair(name: str, prompt_path: Path, artifact_path: Path) -> bool:
    """Process one manifest prompt/artifact pair and generate planner training data."""
    # Read inputs
    prompt_text = prompt_path.read_text(encoding="utf-8", errors="replace")
    info = parse_manifest_prompt(prompt_text)
    props = parse_properties(artifact_path)
    planning_data = info.get("planning_data", {})

    if not planning_data:
        print(f"  SKIP: No planning data found for {name}", file=sys.stderr)
        return False

    # Build ModulePlan
    parameters = build_parameters(planning_data, props)
    command_line = (planning_data.get("command_line", "") or "").replace("\\=", "=").replace("\\:", ":")
    language = detect_language_from_command(command_line, info.get("language", "unknown"))
    wrapper_script = detect_wrapper_script(command_line, language)
    description = info.get("description", "")
    docker_image_tag = (planning_data.get("docker_image_tag", "") or "").replace("\\:", ":").replace("\\=", "=")
    author = (planning_data.get("author", "") or "").replace("\\:", ":").replace("\\=", "=") or "GenePattern Team"
    categories = detect_categories(planning_data, description)
    input_file_formats = extract_file_formats(parameters)

    module_plan = {
        "module_name": planning_data.get("module_name", info.get("name", name)),
        "description": description,
        "author": author,
        "input_file_formats": input_file_formats,
        "language": language,
        "categories": categories,
        "cpu_cores": 4,
        "memory": "8GB",
        "lsid": generate_lsid(),
        "plan": build_plan_text(info, planning_data, parameters),
        "wrapper_script": wrapper_script,
        "command_line": command_line,
        "parameters": parameters,
        "docker_image_tag": docker_image_tag,
    }

    # Build instruction prompt
    instruction = build_instruction(info)

    # Write outputs
    prompt_out = PROMPTS_OUT / f"{name}.txt"
    artifact_out = ARTIFACTS_OUT / f"{name}.json"

    prompt_out.write_text(instruction, encoding="utf-8")
    artifact_out.write_text(json.dumps(module_plan, indent=2, ensure_ascii=False), encoding="utf-8")

    return True


def main():
    # Create output directories
    PROMPTS_OUT.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_OUT.mkdir(parents=True, exist_ok=True)

    # Get all prompt files
    prompt_files = sorted(PROMPTS_IN.glob("*.txt"))
    print(f"Found {len(prompt_files)} manifest prompts")

    success = 0
    skipped = 0
    errors = 0

    for prompt_path in prompt_files:
        name = prompt_path.stem
        artifact_path = ARTIFACTS_IN / f"{name}.properties"

        if not artifact_path.exists():
            print(f"  SKIP: No matching artifact for {name}", file=sys.stderr)
            skipped += 1
            continue

        try:
            if process_pair(name, prompt_path, artifact_path):
                success += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  ERROR processing {name}: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone! Generated {success} pairs, skipped {skipped}, errors {errors}")
    print(f"Outputs: {PROMPTS_OUT}/ and {ARTIFACTS_OUT}/")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    main()


