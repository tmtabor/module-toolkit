#!/usr/bin/env python
"""
build_research_prompts.py

For each module in training/manifests/prompts/:
  - If it has a repository URL: run the researcher agent, then rewrite
    training/planner/prompts/{name}.txt with a research-report-based prompt
    (matching the style of the known-good examples in training/planner/examples/).
  - If it has no repository URL: move the existing planner prompt and artifact
    to training/planner/prompts/no_research/ and training/planner/artifacts/no_research/.

Run from the repo root:
    python training/planner/build_research_prompts.py
"""

import ast
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────
REPO_ROOT      = Path(__file__).resolve().parents[2]
MANIFESTS_PROMPTS = REPO_ROOT / "training" / "manifests" / "prompts"
PLANNER_PROMPTS   = REPO_ROOT / "training" / "planner" / "prompts"
PLANNER_ARTS      = REPO_ROOT / "training" / "planner" / "artifacts"
NO_RESEARCH_PROMPTS = PLANNER_PROMPTS / "no_research"
NO_RESEARCH_ARTS    = PLANNER_ARTS    / "no_research"

# ── bootstrap the GenePattern package path ─────────────────────────────────
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv()

from agents.researcher import researcher_agent


# ── helpers ─────────────────────────────────────────────────────────────────

def parse_manifest_prompt(text: str) -> dict:
    """Extract tool info and planning data from a manifest prompt .txt file."""
    info = {}
    for field, key in [
        (r"- Name:\s*(.+)",        "name"),
        (r"- Version:\s*(.+)",     "version"),
        (r"- Language:\s*(.+)",    "language"),
        (r"- Description:\s*(.+)", "description"),
        (r"- Repository:\s*(.*)",  "repository"),
    ]:
        m = re.search(field, text)
        if m:
            info[key] = m.group(1).strip().replace("\\:", ":").replace("\\=", "=")

    pd_match = re.search(r"Planning Data:\s*\n(.+?)(?:\n\nGenerate|\Z)", text, re.DOTALL)
    if pd_match:
        raw = pd_match.group(1).strip()
        try:
            info["planning_data"] = ast.literal_eval(raw)
        except Exception:
            raw2 = raw.replace("\\=", "=").replace("\\:", ":")
            try:
                info["planning_data"] = ast.literal_eval(raw2)
            except Exception:
                info["planning_data"] = {}
    else:
        info["planning_data"] = {}

    return info


def build_research_prompt(info: dict) -> str:
    """Build the researcher-agent prompt (matches do_research() in module.py)."""
    pd = info.get("planning_data", {})
    description = info.get("description", "") or pd.get("description", "")
    repo = info.get("repository", "") or ""

    return f"""
            Research the bioinformatics tool '{info["name"]}' and provide comprehensive information.
            
            Known Information:
            - Name: {info["name"]}
            - Version: {info.get("version", "latest")}
            - Language: {info.get("language", "unknown")}
            - Description: {description or "Not provided"}
            - Repository: {repo or "Not provided"}
            - Documentation: Not provided
            
            Please provide detailed research including:
            1. Tool purpose and scientific applications
            2. Input/output formats and requirements
            3. Parameter analysis and usage patterns
            4. Installation and dependency requirements
            5. Common workflows and use cases
            6. Integration considerations for GenePattern
            
            Focus on information that will help create a complete GenePattern module.
            """


def build_planner_prompt(info: dict, research_text: str) -> str:
    """
    Build the planner training prompt in the same style as the known-good examples.
    Format: tool info header + markdown research report + closing instructions.
    """
    name    = info["name"]
    version = info.get("version", "latest")
    language = info.get("language", "unknown")
    description = info.get("description", "") or info.get("planning_data", {}).get("description", "")

    return f"""Create a comprehensive structured plan for the GenePattern module for '{name}'.
            
            Tool Information:
            - Name: {name}
            - Version: {version}
            - Language: {language}
            - Description: {description}
            
            Research Results:
            {research_text.strip()}
            
            Please create a structured ModulePlan with:
            1. Detailed parameter definitions with types and descriptions
            2. Module architecture recommendations
            3. Integration strategy for GenePattern
            4. Validation and testing approach
            5. Implementation roadmap
            
            If an author name is not provided, use 'GenePattern Team'.
            
            Focus on creating actionable specifications for module development."""


# ── main ────────────────────────────────────────────────────────────────────

def main():
    NO_RESEARCH_PROMPTS.mkdir(parents=True, exist_ok=True)
    NO_RESEARCH_ARTS.mkdir(parents=True, exist_ok=True)

    manifest_files = sorted(MANIFESTS_PROMPTS.glob("*.txt"))
    print(f"Found {len(manifest_files)} manifest prompt files", flush=True)

    with_repo    = []
    without_repo = []

    for mf in manifest_files:
        name = mf.stem
        text = mf.read_text(encoding="utf-8", errors="replace")
        info = parse_manifest_prompt(text)
        repo = info.get("repository", "").strip()
        if repo and repo.startswith("http"):
            with_repo.append((name, info))
        else:
            without_repo.append(name)

    print(f"  With repository URL:    {len(with_repo)}", flush=True)
    print(f"  Without repository URL: {len(without_repo)}", flush=True)

    # ── Step 1: move no-repo modules (skip if already moved) ────────────────
    print("\n── Moving no-repo modules to no_research/ ──", flush=True)
    moved = 0
    already_moved = 0
    for name in without_repo:
        src_prompt = PLANNER_PROMPTS / f"{name}.txt"
        src_art    = PLANNER_ARTS    / f"{name}.json"
        dst_prompt = NO_RESEARCH_PROMPTS / f"{name}.txt"
        dst_art    = NO_RESEARCH_ARTS    / f"{name}.json"

        moved_any = False
        if src_prompt.exists():
            shutil.move(str(src_prompt), str(dst_prompt))
            moved_any = True
        if src_art.exists():
            shutil.move(str(src_art), str(dst_art))
            moved_any = True

        if moved_any:
            moved += 1
        elif dst_prompt.exists() or dst_art.exists():
            already_moved += 1

    print(f"  Moved {moved} modules, {already_moved} already in no_research/", flush=True)

    # ── Step 2: run researcher agent for repo modules ────────────────────────
    # Determine which still need updating (still contain old JSON planning data)
    needs_research = []
    for name, info in with_repo:
        prompt_path = PLANNER_PROMPTS / f"{name}.txt"
        if prompt_path.exists():
            content = prompt_path.read_text(encoding="utf-8", errors="replace")
            # Old format has "planning data has been gathered" or raw JSON dict
            if "planning data has been gathered" in content or "'module_name'" in content:
                needs_research.append((name, info))
            else:
                print(f"  ✓ Already has research report: {name}", flush=True)
        else:
            needs_research.append((name, info))

    print(f"\n── Running researcher agent for {len(needs_research)}/{len(with_repo)} modules ──", flush=True)
    success = 0
    errors  = 0

    for i, (name, info) in enumerate(needs_research, 1):
        print(f"\n[{i}/{len(needs_research)}] Researching: {name}", flush=True)

        research_prompt = build_research_prompt(info)

        try:
            result = researcher_agent.run_sync(research_prompt)
            research_text = result.output
            print(f"  ✓ Research complete ({len(research_text)} chars)", flush=True)
        except Exception as e:
            print(f"  ✗ Research failed: {e}", flush=True)
            traceback.print_exc()
            errors += 1
            continue

        # Build and write the new planner prompt
        planner_prompt = build_planner_prompt(info, research_text)
        out_path = PLANNER_PROMPTS / f"{name}.txt"
        out_path.write_text(planner_prompt, encoding="utf-8")
        print(f"  ✓ Written: {out_path.name}", flush=True)
        success += 1

        # Small courtesy pause between modules to avoid hammering search APIs
        if i < len(needs_research):
            time.sleep(2)

    print(f"\n── Done ──", flush=True)
    print(f"  Research prompts written: {success}", flush=True)
    print(f"  Errors:                   {errors}", flush=True)


if __name__ == "__main__":
    main()



