"""
Serializable result models for the side-effect functions in ``agents.effects``.

Each side effect returns a plain-data result (Pydantic ``BaseModel``) instead of
mutating shared state or writing to a ``Logger``. The ``log`` list carries any
progress/status lines the effect would have printed; the async orchestrator
drains it through its own ``Logger`` so console output is preserved while the
effect itself stays pure and serializable across a (future) Temporal activity
boundary. See temporal/PHASE2.md.
"""
from pydantic import BaseModel, Field


class EffectResult(BaseModel):
    """Base for every effect result: an ok flag plus drained-later log lines."""
    ok: bool = True
    log: list[str] = Field(default_factory=list)


class DownloadResult(EffectResult):
    local_path: str | None = None   # resolved absolute path on success, else None
    filename: str = ""              # the (collision-resolved) filename used
    size: int = 0
    error: str = ""


class ValidationResult(EffectResult):
    success: bool = False
    output: str = ""                # full captured linter output


class BuildResult(ValidationResult):
    """Result of the heavy docker build + runtime-test path (validate_dockerfile)."""


class UploadResult(EffectResult):
    success: bool = False
    message: str = ""


class ZipResult(EffectResult):
    zip_path: str | None = None
    size: int = 0


class PushResult(EffectResult):
    success: bool = False
