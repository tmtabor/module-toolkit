---
name: gp-manifest
description: Generate or regenerate the GenePattern manifest file for an existing module directory. Use when the manifest needs to be created from scratch or fixed after validation failures.
---

You are generating or regenerating the `manifest` file for a GenePattern module.

## Step 1 — Read the current state

Read these files from the module directory:
1. `~/.claude/skills/gp-module/GENEPATTERN_MODULE_SPEC.md` — full manifest spec (Sections 1, 3, 4)
2. The existing wrapper script — to confirm parameter names, flags, and types
3. The existing `manifest` if present — to understand what exists and what needs fixing
4. If validation errors were reported, read them carefully before proceeding

## Step 2 — Confirm the parameter design

Before writing, list every parameter with:
- `p<N>_name` value
- Type (FILE / TEXT / Integer / Float)
- Required or optional
- The exact GATK/tool CLI flag it maps to
- Whether it needs `prefix_when_specified` (optional params and multi-value FILE params)
- numValues (`1..1`, `0..1`, `1+`, `0+`)

Cross-check against the wrapper's `case` statement — every parameter in the manifest must have a matching `--param.name)` handler in the wrapper.

## Step 3 — Write the manifest via Python

**CRITICAL: Always write the manifest using Python's file API, never with the Write or Edit tools.** Those tools strip trailing whitespace, which breaks the required trailing space on every `prefix_when_specified` line.

```python
python3 - <<'PYEOF'
content = """\
# GenePattern Module Manifest

LSID=urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:00000:1
name=<module.Name>
...
p1_prefix_when_specified=--param.name \x20
"""
# The \x20 placeholder becomes a literal trailing space after replace:
content = content.replace("\\x20", " ")
with open("manifest", "w") as f:
    f.write(content)
PYEOF
```

## Step 4 — Manifest rules (all must be satisfied)

### Core fields
- `LSID` — use placeholder `urn:lsid:broad.mit.edu:cancer.software.genepattern.module.generated:00000:1` for new modules
- `name` — R-safe: letters, digits, dots only; no leading dot or digit; no R keywords (if, else, for, function, while, repeat, in, next, break, true, false, null, na, inf, nan)
- `commandLine` — must reference every parameter as `<param.name>`; wrapper script prefixed with `<libdir>` (e.g., `bash <libdir>wrapper.sh`)
- `job.docker.image` — colon escaped with backslash: `broadinstitute/gatk\:4.1.4.1`
- `taskDoc=README.md`
- No non-ASCII characters anywhere (no em-dash, curly quotes, accented letters, ellipsis)

### commandLine construction
- Required parameters with fixed flags: hardcode the flag — `--input.vcf <input.vcf>`
- Optional parameters with `prefix_when_specified`: just the placeholder — `<input.vcf.tbi>`
- Multi-value FILE parameters (numValues=1+): use `prefix_when_specified` and just the placeholder — `<input.tar.gz>`
- Wrapper always called as `bash <libdir>wrapper.sh` (JVM tools) or `python <libdir>wrapper.py` etc.

### Parameter fields
- Sequential numbering p1, p2, … pN — **no gaps**
- Required: `p<N>_optional=` (empty); Optional: `p<N>_optional=on`
- FILE params need `p<N>_MODE=IN` and `p<N>_TYPE=FILE`
- Non-FILE params: `p<N>_MODE=` (empty)
- `p<N>_prefix_when_specified` — **must end with a single trailing space**
  - Use dots matching `p<N>_name` exactly: `--input.vcf.tbi ` not `--input-vcf-tbi `
  - Required params hardcoded in commandLine do NOT need `prefix_when_specified`
- `p<N>_numValues` — `1..1` (required single), `0..1` (optional single), `1+` (required multi), `0+` (optional multi)
- Choice params: `p<N>_value=Label\=value;Label2\=value2` — equals sign escaped with backslash, display label first

### Multi-value FILE parameters (numValues=1+ or 0+)
GenePattern passes these as a **list file** (one absolute path per line), not repeated flags.
- Use `prefix_when_specified=--param.name ` (with trailing space)
- The wrapper receives the list-file path and must expand it — see GENEPATTERN_MODULE_SPEC.md Section 2

## Step 5 — Verify after writing

```bash
# 1. Trailing space on every prefix_when_specified line
grep 'prefix_when_specified' manifest | cat -e
# Every line must end ' $' (space before $), not just '$'

# 2. All p*_name placeholders present in commandLine
CMDLINE=$(grep '^commandLine=' manifest | sed 's/commandLine=//')
grep '^p[0-9]*_name=' manifest | sed 's/^p[0-9]*_name=//' | while read name; do
    echo "$CMDLINE" | grep -q "<${name}>" && echo "OK: <$name>" || echo "MISSING: <$name>"
done

# 3. No non-ASCII characters
python3 -c "
text = open('manifest').read()
bad = [(i, c) for i, c in enumerate(text) if ord(c) > 127]
print('Non-ASCII chars:', bad or 'none')
"

# 4. No gaps in parameter numbering
python3 -c "
import re
nums = sorted(set(int(m) for m in re.findall(r'^p(\d+)_name=', open('manifest').read(), re.M)))
expected = list(range(1, len(nums)+1))
print('Gaps:', [n for n in expected if n not in nums] or 'none')
print('Parameters:', nums)
"

# 5. docker image colon escaped
grep 'job.docker.image' manifest
# Must contain \: not bare :
```

Fix any failures and re-verify.

## Arguments

$ARGUMENTS
