---
name: gp-wrapper
description: Generate or regenerate the wrapper script for a GenePattern module. Use when the wrapper needs to be created from scratch or fixed after validation failures.
---

You are generating or regenerating the wrapper script for a GenePattern module.

## Step 1 — Read the current state

Read these files:
1. `~/.claude/skills/gp-module/GENEPATTERN_MODULE_SPEC.md` — wrapper spec (Section 2)
2. The module's `manifest` — for the definitive parameter list, types, and numValues
3. The existing wrapper if present — to understand what exists and what needs fixing
4. Any validation error messages provided

## Step 2 — Choose the wrapper language

Apply this decision rule exactly:

| Tool type | Wrapper language |
|-----------|-----------------|
| JVM/Java tool (GATK, Picard, STAR, etc.) | **bash** — invoke via `gatk ToolName` or `java -jar` |
| R package or Bioconductor tool | **R** — use `optparse`, call library functions directly |
| Python tool or Python package | **Python** — use `argparse`, call via `subprocess` or import |
| Compiled binary (samtools, bwa, bowtie, etc.) | **bash** — direct invocation |
| Shell-based or minimal-parameter tool | **bash** |

**Never write a Java source wrapper for a Java tool.** The bash wrapper calls the tool's CLI.

## Step 3 — Identify parameter handling needs

For each parameter in the manifest, note:
1. **Required or optional** (from `p<N>_optional`)
2. **Single or multi-value** (from `p<N>_numValues`) — `1+` or `0+` means list-file expansion needed
3. **Has companion index?** — needs staging into the working directory
4. **Choice parameter?** — validate against allowed values

## Step 4 — Write the bash wrapper

Use this structure (for JVM and compiled tools):

```bash
#!/bin/bash
set -euo pipefail

TOOL_NAME="module.Name"

# ── Parameter variables ────────────────────────────────────────────────────
PARAM_ONE=""
PARAM_TWO=""
# For multi-value FILE params: store the list-file path
INPUT_LIST_FILE=""
INPUT_FILES=()    # populated by expand_inputs()

# ── Staged file variables (for companion-index staging) ───────────────────
LOCAL_INPUT=""
LOCAL_INPUT_INDEX=""

# ── Cleanup trap ──────────────────────────────────────────────────────────
cleanup() {
    [[ -n "$LOCAL_INPUT"       && -f "$LOCAL_INPUT"       ]] && rm -f "$LOCAL_INPUT"
    [[ -n "$LOCAL_INPUT_INDEX" && -f "$LOCAL_INPUT_INDEX" ]] && rm -f "$LOCAL_INPUT_INDEX"
    echo "[INFO] Cleanup complete."
}
trap cleanup EXIT

# ── Usage ─────────────────────────────────────────────────────────────────
usage() {
    echo "Usage: $0 [OPTIONS]"; exit 1
}

# ── Argument parsing ──────────────────────────────────────────────────────
parse_arguments() {
    [[ $# -eq 0 ]] && { echo "[ERROR] No arguments."; usage; }
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --param.one)          PARAM_ONE="$2";        shift 2 ;;
            --param.two)          PARAM_TWO="$2";        shift 2 ;;
            --multi.value.input)  INPUT_LIST_FILE="$2";  shift 2 ;;  # list-file for 1+ params
            -h|--help)            usage ;;
            *) echo "[ERROR] Unknown option: $1"; usage ;;
        esac
    done
}

# ── Multi-value FILE expansion ─────────────────────────────────────────────
# GenePattern passes multi-value FILE params (numValues=1+ or 0+) as a single
# path to a list file containing one absolute path per line.
expand_inputs() {
    [[ -z "$INPUT_LIST_FILE" ]] && { echo "[ERROR] --multi.value.input required"; exit 1; }
    [[ ! -f "$INPUT_LIST_FILE" ]] && { echo "[ERROR] List file not found: $INPUT_LIST_FILE"; exit 1; }
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" ]] && continue
        INPUT_FILES+=("$line")
    done < "$INPUT_LIST_FILE"
    [[ ${#INPUT_FILES[@]} -eq 0 ]] && { echo "[ERROR] No paths in list file"; exit 1; }
    echo "[INFO] Found ${#INPUT_FILES[@]} input file(s)"
}

# ── Input validation ──────────────────────────────────────────────────────
validate_inputs() {
    local errors=0
    [[ -z "$PARAM_ONE" ]] && { echo "[ERROR] --param.one required"; errors=$((errors+1)); }
    for f in "${INPUT_FILES[@]}"; do
        [[ ! -f "$f" ]] && { echo "[ERROR] File not found: $f"; errors=$((errors+1)); }
    done
    [[ $errors -gt 0 ]] && { echo "[ERROR] $errors error(s). Exiting."; exit 1; }
    echo "[INFO] Validation passed."
}

# ── Companion-file staging ─────────────────────────────────────────────────
# Copy primary + companion index into the writable working directory.
# GATK requires the index to be named exactly <primary>.<ext> in the same dir.
stage_inputs() {
    local workdir; workdir="$(pwd)"
    LOCAL_INPUT="${workdir}/$(basename "${PARAM_ONE}")"
    cp "${PARAM_ONE}" "${LOCAL_INPUT}"
    # Example companion: .bam.bai must be staged as <bam>.bai
    # LOCAL_INPUT_INDEX="${LOCAL_INPUT}.bai"
    # cp "${PARAM_ONE_INDEX}" "${LOCAL_INPUT_INDEX}"
    echo "[INFO] Staged inputs."
}

# ── Tool execution ────────────────────────────────────────────────────────
run_tool() {
    local -a cmd=(gatk ToolName
        -I "${LOCAL_INPUT}"
        -O "${PARAM_TWO}"
    )
    for f in "${INPUT_FILES[@]}"; do cmd+=(-I "$f"); done
    echo "[INFO] Running: ${cmd[*]}"
    echo "---"
    "${cmd[@]}"
    echo "---"
    echo "[INFO] Done."
}

# ── Main ──────────────────────────────────────────────────────────────────
main() {
    echo "[INFO] === ${TOOL_NAME} starting ==="
    parse_arguments "$@"
    expand_inputs          # only if there are multi-value FILE params
    validate_inputs
    stage_inputs           # only if companion-file staging is needed
    run_tool
    echo "[INFO] === ${TOOL_NAME} finished ==="
}

main "$@"
```

## Step 5 — Key rules

### Parameter flag names
- Every `--flag.name` in the wrapper **must exactly match** `p<N>_name` in the manifest (dots, not dashes, not underscores)
- Every manifest parameter must have a `case` entry — no silent skips

### Multi-value FILE parameters
When `p<N>_numValues=1+` or `0+` and `p<N>_TYPE=FILE`:
- GenePattern passes **one list-file path**, not multiple `--flag` repetitions
- The wrapper receives that path via the normal `--flag.name` case
- `expand_inputs()` reads it line-by-line into an array
- Each array element is passed to the tool individually (e.g., repeated `-I` flags for GATK)
- `runLocal.sh` must simulate this by writing the list file before calling Docker

### Companion-file staging
When a FILE parameter needs a co-located index (BAM+BAI, VCF.GZ+TBI, FASTA+FAI+DICT):
- Copy both primary and index into `$(pwd)` (the writable job dir)
- Name the local index with the exact suffix the tool expects:
  - BAM: `<local_bam>.bai`
  - VCF.GZ: `<local_vcf>.tbi`
  - FASTA: `<local_fasta>.fai` and `<stem>.dict`
- Track all local copies in `LOCAL_*` variables so `cleanup()` can remove them

### cleanup() trap
- Must cover every file in a `LOCAL_*` variable
- Must run on EXIT (both success and failure)
- Use `[[ -n "$VAR" && -f "$VAR" ]] && rm -f "$VAR"` — safe even if staging was skipped

### Optional arguments
```bash
if [[ -n "$OPTIONAL_PARAM" ]]; then
    cmd+=(--tool-flag "${OPTIONAL_PARAM}")
fi
```

## Step 6 — Python wrapper structure (for Python tools)

```python
#!/usr/bin/env python3
import argparse, logging, os, subprocess, sys

def parse_arguments():
    p = argparse.ArgumentParser()
    p.add_argument("--input.file",   dest="input_file",   required=True)
    p.add_argument("--output.prefix", dest="output_prefix", default="output")
    return p.parse_args()

def expand_inputs(list_file):
    """Expand a GenePattern multi-value FILE list file into individual paths."""
    paths = [line.strip() for line in open(list_file) if line.strip()]
    if not paths:
        raise ValueError(f"No paths in list file: {list_file}")
    return paths

def validate_inputs(args):
    if not os.path.exists(args.input_file):
        raise FileNotFoundError(f"Not found: {args.input_file}")

def run_tool(args):
    cmd = ["tool-binary", "--input", args.input_file, "--output", args.output_prefix]
    logging.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_arguments()
    validate_inputs(args)
    run_tool(args)

if __name__ == "__main__":
    main()
```

## Step 7 — Validate after writing

```bash
# 1. Bash syntax check
bash -n wrapper.sh && echo "SYNTAX OK"

# 2. All manifest parameters handled in wrapper
grep '^p[0-9]*_name=' manifest | sed 's/^p[0-9]*_name=//' | while read name; do
    grep -q "\-\-${name})" wrapper.sh && echo "OK: --$name" || echo "MISSING: --$name"
done

# 3. Python syntax check (if Python)
python3 -m py_compile wrapper.py && echo "SYNTAX OK"
```

Fix any failures and re-verify.

## Arguments

$ARGUMENTS
