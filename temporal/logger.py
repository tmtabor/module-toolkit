"""
Workflow-safe replacement for agents.logger.Logger.

Plain `print()`/stdlib-logging calls inside Temporal workflow code are
replay-unsafe: the workflow replays its history on worker restart, and each
replay would re-emit every log line. `WorkflowLogger` implements the same
duck-typed interface as `agents.logger.Logger` (`print_status`/`print_section`)
but routes through `temporalio.workflow.logger`, which Temporal automatically
suppresses during replay.

Only used inside temporal/workflow.py -- the legacy/`--legacy` CLI path keeps
using the plain `Logger`.
"""
from typing import Optional

from temporalio import workflow

_LEVEL_METHOD = {
    'ERROR': 'error',
    'WARNING': 'warning',
    'DEBUG': 'debug',
}

# Cap on retained log lines exposed via the workflow progress query, so a long
# run's in-workflow buffer stays bounded.
_MAX_SINK_LINES = 500


class WorkflowLogger:
    """Logger-shaped adapter that emits through temporalio.workflow.logger.

    If a *sink* list is provided, each line is also appended to it (bounded to
    the most recent `_MAX_SINK_LINES`) so the workflow can expose recent log
    output via a `@workflow.query` (temporal/PHASE4.md 4.3). Appending to a
    plain in-memory list is deterministic and replay-safe.
    """

    def __init__(self, sink: Optional[list] = None) -> None:
        self._sink = sink

    def _capture(self, line: str) -> None:
        if self._sink is None:
            return
        self._sink.append(line)
        if len(self._sink) > _MAX_SINK_LINES:
            del self._sink[:-_MAX_SINK_LINES]

    def print_status(self, message: str, level: str = "INFO") -> None:
        method_name = _LEVEL_METHOD.get(level.upper(), 'info')
        getattr(workflow.logger, method_name)(message)
        self._capture(f"{level}: {message}")

    def print_section(self, title: str) -> None:
        workflow.logger.info("=" * 60 + f"\n {title}\n" + "=" * 60)
        self._capture(f"=== {title} ===")
