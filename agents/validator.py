"""
Artifact validation shim.

Thin wrapper over the ``agents.effects`` linter functions. Routes the
docker-build path to ``build_and_test_image`` (the heavy, long-running effect)
and everything else to ``run_linter``, drains the effect's captured log through
the caller's ``Logger``, and returns the legacy ``{'success', 'result'/'error'}``
dict the orchestrator expects. The actual linter dispatch lives in
``agents.effects``; this layer only adapts it to the ``Logger``-based caller.
"""
from typing import Any, Dict, List, Optional

from agents.effects import LINTER_MAP, build_and_test_image, run_linter  # noqa: F401 (LINTER_MAP re-exported)
from agents.logger import Logger


def validate_artifact(
    file_path: str,
    validate_tool: str,
    extra_args: Optional[List[str]],
    logger: Logger,
) -> Dict[str, Any]:
    """Validate an artifact via its linter effect and adapt to the legacy dict shape.

    Args:
        file_path:     Path to the artifact file to validate.
        validate_tool: Key into LINTER_MAP (e.g. 'validate_dockerfile').
        extra_args:    Additional CLI arguments to forward to the linter.
        logger:        Logger instance for draining the effect's status lines.

    Returns:
        ``{'success': True, 'result': <output>}`` or
        ``{'success': False, 'error': <output>}``.
    """
    logger.print_status(f"Validating with {validate_tool}")

    # The docker build/runtime test is its own (heavy) effect; everything else
    # is a lightweight in-process linter.
    if validate_tool == 'validate_dockerfile':
        result = build_and_test_image(file_path, extra_args)
    else:
        result = run_linter(validate_tool, file_path, extra_args)

    # Drain the effect's captured log through the caller's Logger.
    for line in result.log:
        logger.print_status(line)

    if result.success: return {'success': True, 'result': result.output}
    return {'success': False, 'error': result.output}
