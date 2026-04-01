---
name: gp-module
description: Creates a complete GenePattern module for a bioinformatics tool (GATK analysis, R package function, Python tool, etc.). Use when the user asks to wrap a tool as a GenePattern module, create a new GenePattern module, or add a tool to GenePattern.
---

You are creating a new GenePattern module. Follow every step below in order before writing any files.

## Step 1 — Read the spec and examples

Read these files now before doing anything else:

1. `~/.claude/skills/gp-module/GENEPATTERN_MODULE_SPEC.md` — the authoritative format spec. Pay close attention to:
   - Section 1: Manifest rules — trailing-space rule, LSID format, parameter fields
   - Section 2: Wrapper rules — multi-value FILE list-file pattern, dot-notation flags
   - Section 3: paramgroups.json — format, group rules, validation
   - Section 6: gpunit test format

2. Pick the closest existing module in the **current working directory** as a structural template:
   - Look for modules that wrap the same tool ecosystem (GATK, R, Python, etc.)
   - Good references if present: `gatk.FilterMutectCalls` (multiple optional auxiliary inputs) or `gatk.GetPipelineSummaries` (JVM tool with indexed file pairs)
   - Read its `manifest`, wrapper script, `paramgroups.json`, `build.xml`, and `gpunit/test.yml`

3. New modules are created as subdirectories of the **current working directory**.

---

## Step 2 — Gather tool documentation

Fetch the official documentation page for the tool being wrapped. Extract:
- All **required** arguments (become required GenePattern parameters)
- The most important **optional** arguments users commonly need (expose as GenePattern parameters)
- Advanced/rarely-used arguments (users pass via `arguments.file`)
- Input file formats and whether they require companion index files (BAM→BAI, VCF.GZ→TBI, FASTA→FAI+DICT)
- Output file names and formats

If the documentation URL is not given, ask the user before proceeding.

---

## Step 3 — Write a design plan before touching any files

Before creating any files, produce a concise design document:

1. **Module name** — R-safe dot notation (e.g., `gatk.LearnReadOrientationModel`)
2. **Docker image** — which image and why
3. **Wrapper language** — apply this rule:
   - JVM/Java tool (GATK, Picard, STAR, etc.) → **bash**
   - R package / Bioconductor → **R**
   - Python tool → **Python**
   - Compiled binary (samtools, bwa, etc.) → **bash**
4. **Parameter table** — one row per parameter: name | type | required? | tool flag | notes (companion index? multi-value? choice list?)
5. **Companion file staging plan** — which files need co-located indexes and how they'll be staged
6. **paramgroups layout** — the 2–5 UI groups and which parameters go in each
7. **Test data plan** — what files the gpunit test needs and where to get them

Output this as a brief markdown summary before proceeding to file creation.

---

## Step 4 — Create the module directory and files

Create `<ecosystem>.<ToolName>/` under the current working directory containing all of the following artifacts.

### 4a. manifest

Use the `gp-manifest` sub-skill for detailed rules. Key points:
- Write via Python (never Write/Edit tools) to preserve trailing spaces on `prefix_when_specified`
- `LSID`: placeholder `urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:00000:1`
- `job.docker.image`: colon escaped with `\` (e.g., `broadinstitute/gatk\:4.1.4.1`)
- `commandLine`: every parameter appears as `<param.name>`; wrapper prefixed with `<libdir>`
- Parameters p1…pN: sequential, no gaps
- Always include `arguments.file` (optional FILE) and a tool config file param if the tool supports one

### 4b. Wrapper script

Use the `gp-wrapper` sub-skill for detailed rules. Key points:
- Language follows the rule in Step 3
- Every `--flag.name` must exactly match `p<N>_name` in the manifest (dots not dashes)
- Multi-value FILE params (`numValues=1+` or `0+`): implement `expand_inputs()` to read the GenePattern list file
- Companion files: stage primary + index into `$(pwd)`; track in `LOCAL_*` vars; remove in `cleanup()` trap
- Structure: `parse_arguments()` → `expand_inputs()` (if needed) → `validate_inputs()` → `stage_inputs()` (if needed) → `run_tool()`

### 4c. paramgroups.json

Full rules in GENEPATTERN_MODULE_SPEC.md Section 3. Key points:
- Every manifest parameter in exactly one group
- Max 5 groups, max 10 params per group
- Last group always `"Advanced Options"` with `"hidden": true` containing `arguments.file` and `gatk.config.file`
- Group companion files together (e.g., `reference`, `reference.fai`, `reference.dict` in one group)
- Validate: every parameter covered, no extras

### 4d. build.xml

Copy from the nearest existing module and update:
- `name` attribute on `<project>`
- The `<fileset>` includes — always include `manifest`, `paramgroups.json`, wrapper script, `README.md`, `LICENSE`; add `Dockerfile` if present

### 4e. README.md

Follow the structure from the nearest example module:
- Header: version, description, authors, contact, algorithm version
- Summary section with pipeline workflow diagram
- References
- Parameters table (Name | Description | Default)
- Input Files section (numbered entry per file parameter)
- Output Files section
- Requirements section (Docker image name)
- Version Comments table

### 4f. release.version / prerelease.version

```
#update release version number
build.number=1
```
```
#prerelease version number
prerelease.number=1
```

### 4g. gpunit/test.yml and gpunit/data/

Always create a gpunit test. Find the smallest possible real test data:
- For GATK: check `https://github.com/broadinstitute/gatk/tree/master/src/test/resources`
- For R: use built-in datasets or tiny synthetic data
- Download all needed files (including companion indexes) into `gpunit/data/`

The test YAML:
- References files as `data/<filename>` (relative to `gpunit/`)
- Sets `output.file.name` or `output.prefix` to a predictable name
- Asserts expected output files under `assertions.files`

### 4h. runLocal.sh

Create in the module root directory. Requirements:
1. Resolve module/data dirs as absolute paths at runtime: `$(cd "$(dirname "$0")" && pwd)`
2. Create timestamped output dir under `gpunit/local_runs/`
3. Use `job.docker.image` from manifest (strip backslash escape); build `<module>:local` if Dockerfile exists
4. Mount `gpunit/data/` as `/data`, run dir as `/work` (working dir), wrapper script as `/usr/local/bin/<wrapper>` (pre-existing images)
5. For multi-value FILE params: write the list file into the run dir before calling Docker
6. Print the full docker command before running it

---

## Step 5a — Build Dockerfile if created

Only needed when the tool is not in a suitable public image. For GATK tools, skip this step.

If you do write a Dockerfile, build it immediately and iterate up to 5 times on failures:
```bash
docker build -t <module-name>:local <module-directory>
```
Update `job.docker.image` in the manifest if the build succeeds.

---

## Step 5b — Validate everything

**When iterating after a failure, identify the root artifact first:**

| Failing artifact | Likely root cause |
|-----------------|-------------------|
| gpunit test fails | wrapper logic or manifest parameter mismatch |
| Dockerfile build — missing package | add to Dockerfile, not the wrapper |
| Dockerfile build — syntax error | wrapper has bad imports or syntax |
| paramgroups check fails | parameter name mismatch with manifest |
| GATK runtime — wrong flag | wrapper passes wrong CLI flag |
| GATK runtime — file not found | staging logic or companion index missing |

Run all checks:

```bash
# 1. Bash syntax
bash -n <wrapper>.sh && echo "OK"

# 2. All p*_name placeholders in commandLine
CMDLINE=$(grep '^commandLine=' manifest | sed 's/commandLine=//')
grep '^p[0-9]*_name=' manifest | sed 's/^p[0-9]*_name=//' | while read name; do
    echo "$CMDLINE" | grep -q "<${name}>" && echo "OK: <$name>" || echo "MISSING: <$name>"
done

# 3. All p*_name handled in wrapper
grep '^p[0-9]*_name=' manifest | sed 's/^p[0-9]*_name=//' | while read name; do
    grep -q "\-\-${name})" <wrapper>.sh && echo "OK: --$name" || echo "MISSING: --$name"
done

# 4. Trailing spaces on prefix_when_specified
grep 'prefix_when_specified' manifest | cat -e
# Every line must end ' $' (space before dollar)

# 5. paramgroups.json covers all manifest parameters
python3 -c "
import json, re
params = re.findall(r'^p\d+_name=(.+)', open('manifest').read(), re.M)
groups = json.load(open('paramgroups.json'))
grouped = [p for g in groups for p in g['parameters']]
missing = [p for p in params if p not in grouped]
extra   = [p for p in grouped if p not in params]
print('Missing from paramgroups:', missing or 'none')
print('Extra in paramgroups:', extra or 'none')
print('Group count:', len(groups), '(max 5)')
" && echo "OK"

# 6. runLocal.sh is executable
chmod +x runLocal.sh && echo "OK"
```

Fix all failures before declaring done.

---

## Key gotchas checklist

**Manifest**
- [ ] All `prefix_when_specified` values end with a trailing space (written via Python, not Write/Edit tools)
- [ ] `job.docker.image` colon is escaped: `image\:tag`
- [ ] Parameter numbers are sequential with no gaps (p1, p2, p3 … pN)
- [ ] Optional params use `p<N>_optional=on`; required params use `p<N>_optional=` (empty)
- [ ] No non-ASCII characters anywhere in the manifest

**Wrapper**
- [ ] Wrapper language matches tool type (JVM→bash, R→R, Python→Python, compiled binary→bash)
- [ ] Multi-value FILE parameters use the list-file expansion pattern (`expand_inputs()`)
- [ ] Staged companion files use the exact naming convention the tool expects (e.g., `.bam.bai` not just `.bai`)
- [ ] `cleanup()` trap removes all staged files, including companion indices
- [ ] Wrapper uses `set -euo pipefail`

**paramgroups.json**
- [ ] Every manifest parameter appears in exactly one group
- [ ] `arguments.file` and `gatk.config.file` (or equivalents) are in a hidden "Advanced Options" group
- [ ] Included in `build.xml` fileset

**Testing**
- [ ] Test data format is compatible with the tool version in the Docker image
- [ ] If Dockerfile was created: image built successfully before finishing
- [ ] `runLocal.sh` exists, is executable, uses runtime-resolved absolute paths, and mounts the wrapper from the local drive when using a pre-existing image

---

## Arguments

The tool to wrap is: $ARGUMENTS
