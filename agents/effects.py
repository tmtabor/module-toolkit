"""
Activity-ready side-effect functions for the module-generation pipeline.

Every non-deterministic operation the pipeline performs (filesystem I/O, HTTP,
docker/subprocess, archiving, linter dispatch) lives here as a module-level,
``Logger``-free, ``self``-free function with serializable inputs and outputs.
Paths cross as ``str``; files cross by path, never by content. This is what lets
Phase 3 decorate each function with ``@activity.defn`` without a rewrite.

See temporal/PHASE2.md for the design rules and the full side-effect inventory.
"""
import importlib
import io
import json
import shutil
import subprocess
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import requests

from agents.effects_models import (
    BuildResult, DownloadResult, PushResult, UploadResult, ValidationResult, ZipResult,
)

# Maps the validate_tool name (from ModuleAgent.artifact_agents) to the
# importable linter module. Lives here so both effects and the legacy
# agents.validator shim can share it.
LINTER_MAP: dict[str, str] = {
    'validate_manifest': 'manifest.linter',
    'validate_dockerfile': 'dockerfile.linter',
    'validate_documentation': 'documentation.linter',
    'validate_gpunit': 'gpunit.linter',
    'validate_paramgroups': 'paramgroups.linter',
    'validate_wrapper': 'wrapper.linter',
}

# Filenames that share an extension with wrapper scripts but are never the wrapper.
_NON_WRAPPER_NAMES = frozenset({"manage.py", "setup.py", "setup.cfg", "conftest.py"})
_WRAPPER_EXTENSIONS = (".py", ".r", ".sh", ".bash", ".pl")


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def make_module_dir(output_dir: str, tool_name: str, timestamp: str, module_dir: str = "") -> str:
    """Create the module directory and return its path as a string.

    *timestamp* is injected (not read from the clock) so the caller — a Temporal
    workflow in Phase 3 — controls determinism. If *module_dir* is given it is
    used verbatim (the web UI relies on this).
    """
    if module_dir:
        path = Path(module_dir)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    tool_name_clean = tool_name.lower().replace(' ', '_').replace('-', '_')
    path = Path(output_dir) / f"{tool_name_clean}_{timestamp}"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def write_text_file(path: str, content: str) -> None:
    """Write *content* to *path* (UTF-8), creating parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        f.write(content)


def read_text_file(path: str) -> str | None:
    """Return the file's text, or None if it does not exist / cannot be read."""
    p = Path(path)
    if not p.exists(): return None
    try:
        return p.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return None


def file_exists(path: str) -> bool:
    """Return whether *path* exists on disk.

    Added during the temporal/PHASE5.md Workstream B spike: `--legacy`
    (agents/module.py) was calling `pathlib.Path.exists()` directly for
    existence checks -- a real gap in Phase 2's "every side effect is
    extracted" claim, undetected until the spike compared both drivers line
    by line. The Temporal path had no equivalent primitive to call (workflow
    code can't touch the filesystem directly), so it worked around the gap by
    repurposing `read_text_file`'s `None`-return as an existence signal
    instead -- functionally equivalent but semantically the wrong effect for
    the question actually being asked, and wasteful when the content itself
    is never used. This is the correct primitive for both.
    """
    return Path(path).exists()


def remove_dir(path: str) -> None:
    """Recursively remove *path* if present; a no-op otherwise (idempotent)."""
    p = Path(path)
    if not p.exists(): return
    shutil.rmtree(p, ignore_errors=True)


def find_wrapper_file(module_dir: str) -> str | None:
    """Return the most likely wrapper filename in *module_dir*, or None.

    Deterministic: candidates are scored (``wrapper*`` first, then ``run_*``,
    then the rest) and ties broken by sorted name, so repeated calls agree.
    """
    module_path = Path(module_dir)
    if not module_path.exists(): return None

    candidates = [
        p for p in module_path.iterdir()
        if p.is_file()
        and p.suffix.lower() in _WRAPPER_EXTENSIONS
        and p.name not in _NON_WRAPPER_NAMES
    ]
    if not candidates: return None

    def _score(p: Path) -> tuple[int, str]:
        n = p.name.lower()
        if n.startswith("wrapper"): return (0, p.name)
        if n.startswith("run_"): return (1, p.name)
        return (2, p.name)

    candidates.sort(key=_score)
    return candidates[0].name


def read_manifest_docker_image(module_dir: str) -> str | None:
    """Return job.docker.image from the module's manifest (colons unescaped), or None."""
    manifest_path = Path(module_dir) / 'manifest'
    if not manifest_path.exists(): return None
    try:
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('job.docker.image='):
                return line[len('job.docker.image='):].replace('\\:', ':')
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def download_one(url: str, dest_dir: str, filename: str) -> DownloadResult:
    """Download *url* to ``dest_dir/filename``, streaming to disk.

    Returns a DownloadResult with the resolved absolute path on success; on
    failure any partial file is removed and ``local_path`` stays None so the
    caller can skip the item.
    """
    result = DownloadResult(filename=filename)
    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)
    dest = dest_dir_path / filename

    result.log.append(f"Downloading {url} -> {dest}")
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk: f.write(chunk)
        result.local_path = str(dest.resolve())
        result.size = dest.stat().st_size
        result.log.append(f"SUCCESS: Downloaded {filename} ({result.size:,} bytes)")
        return result

    except Exception as e:
        result.ok = False
        result.error = str(e)
        result.log.append(f"WARNING: Failed to download {url}: {e} - skipping this item")
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        return result


def upload_module(zip_path: str, gp_server: str, gp_user: str, gp_password: str) -> UploadResult:
    """Upload a module zip to a GenePattern server's installModule endpoint."""
    result = UploadResult()
    endpoint = f"{gp_server.rstrip('/')}/rest/v1/tasks/installModule"
    zip_name = Path(zip_path).name
    result.log.append(f"Uploading {zip_name} to {endpoint}")

    try:
        with open(zip_path, 'rb') as f:
            response = requests.post(
                endpoint,
                auth=(gp_user, gp_password),
                files={'file': (zip_name, f, 'application/zip')},
                data={'privacy': '1'},
            )

        try:
            body = response.json()
        except Exception:
            body = {}

        status = body.get('status', '')
        message = body.get('message', response.text[:200])

        if status == 'success' or (not status and response.status_code in (200, 201)):
            result.success = True
            result.message = message or f"Module uploaded successfully (HTTP {response.status_code})"
            result.log.append(f"SUCCESS: {result.message}")
        else:
            result.ok = False
            result.message = message
            result.log.append(f"ERROR: Upload failed: HTTP {response.status_code} - {message}")
        return result

    except Exception as e:
        result.ok = False
        result.message = str(e)
        result.log.append(f"ERROR: Upload failed: {e}")
        return result


# ---------------------------------------------------------------------------
# Archive + subprocess
# ---------------------------------------------------------------------------

def zip_artifacts(module_dir: str, zip_name: str, member_filenames: list[str], zip_only: bool = False) -> ZipResult:
    """Zip the given member files (by basename) found in *module_dir* into *zip_name*.

    *member_filenames* is the explicit, caller-computed list of files to include
    (the orchestrator derives it from the plan). When *zip_only* is set the
    original member files are deleted, leaving only the archive.
    """
    result = ZipResult()
    module_path = Path(module_dir)
    wanted = set(member_filenames)

    files_to_zip = [f for f in module_path.iterdir() if f.is_file() and f.name in wanted]
    if not files_to_zip:
        result.ok = False
        result.log.append("WARNING: No artifact files found to zip")
        return result

    zip_path = module_path / zip_name
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for f in files_to_zip:
            zipf.write(f, arcname=f.name)
            result.log.append(f"  Added {f.name} to zip")

    result.zip_path = str(zip_path)
    result.size = zip_path.stat().st_size
    result.log.append(f"SUCCESS: Created {zip_name} ({result.size:,} bytes)")

    if zip_only:
        result.log.append("Cleaning up artifact files (zip-only)")
        for f in files_to_zip:
            try:
                f.unlink()
                result.log.append(f"  Deleted {f.name}")
            except Exception as e:
                result.log.append(f"WARNING: Failed to delete {f.name}: {e}")

    return result


def docker_push(tag: str) -> PushResult:
    """Push a built docker image to its registry via ``docker push``."""
    result = PushResult()
    result.log.append(f"Pushing Docker image: {tag}")
    try:
        proc = subprocess.Popen(
            ["docker", "push", tag],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            result.log.append(line.rstrip("\n"))
        proc.wait()

        if proc.returncode == 0:
            result.success = True
            result.log.append(f"SUCCESS: Pushed {tag}")
        else:
            result.ok = False
            result.log.append(f"ERROR: Docker push failed for {tag} (exit code {proc.returncode})")
        return result

    except FileNotFoundError:
        result.ok = False
        result.log.append("ERROR: Docker CLI not found; ensure Docker is installed and on PATH")
        return result
    except Exception as e:
        result.ok = False
        result.log.append(f"ERROR: Docker push error: {e}")
        return result


# ---------------------------------------------------------------------------
# Linter dispatch + docker build/test
# ---------------------------------------------------------------------------

# Substrings that classify a linter's captured output as failure / success.
_FAIL_INDICATORS = ["fail:", "failed", "error:", "invalid json", "validation failed"]
_PASS_INDICATORS = [
    "pass:", "passed", "validation passed", "has passed", "**passed**",
    "successfully", "validation successful", "all checks passed",
]


def _dispatch_linter(validate_tool: str, file_path: str, extra_args: list[str] | None,
                     result: ValidationResult) -> ValidationResult:
    """Run a linter module in-process, capture output, and classify pass/fail.

    Shared by run_linter (lightweight linters) and build_and_test_image (the
    docker build/runtime test, which lives in dockerfile.linter).
    """
    if validate_tool not in LINTER_MAP:
        result.ok = False
        result.output = f"Unknown validation tool: {validate_tool}"
        return result

    linter_module = importlib.import_module(LINTER_MAP[validate_tool])
    stdout_capture, stderr_capture = io.StringIO(), io.StringIO()

    try:
        linter_args = [file_path] + (list(extra_args) if extra_args else [])
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exit_code = linter_module.main(linter_args)
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0
    except Exception as e:
        import traceback
        result.ok = False
        result.output = f"Validation error: {e}\n{traceback.format_exc()}"
        result.log.append(f"ERROR: {result.output}")
        return result

    output = stdout_capture.getvalue()
    errors = stderr_capture.getvalue()
    full_output = output + (f"\nErrors:\n{errors}" if errors else "")
    result.output = full_output
    lowered = full_output.lower()

    if exit_code != 0 or any(i in lowered for i in _FAIL_INDICATORS):
        result.success = False
        result.log.append("ERROR: Validation failed. Full validation output:")
        result.log.append(full_output)
    elif any(i in lowered for i in _PASS_INDICATORS):
        result.success = True
        result.log.append("SUCCESS: Validation passed")
    else:
        result.success = False
        result.log.append("WARNING: Ambiguous validation result, defaulting to failure")
        result.log.append(full_output)
        result.output = f"Ambiguous validation result: {full_output}"

    return result


def run_linter(validate_tool: str, file_path: str, extra_args: list[str] | None = None) -> ValidationResult:
    """Validate an artifact with its lightweight in-process linter."""
    return _dispatch_linter(validate_tool, file_path, extra_args, ValidationResult())


def build_and_test_image(dockerfile_path: str, extra_args: list[str] | None = None) -> BuildResult:
    """Build the docker image and run its runtime test (the heavy, long-running effect).

    Delegates to the dockerfile linter (which shells out to ``docker build`` /
    ``docker run``). Kept as its own function so Phase 3 can give it a distinct
    Temporal timeout/heartbeat policy and a Docker-capable worker.
    """
    return _dispatch_linter('validate_dockerfile', dockerfile_path, extra_args, BuildResult())
