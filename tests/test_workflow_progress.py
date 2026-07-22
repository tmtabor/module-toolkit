"""
Unit tests for ModuleGenerationWorkflow.progress() and the WorkflowLogger
capture that backs it -- the query that replaced the shadow status.json the web
UI used to tail (temporal/PHASE4.md 4.1/4.3).

These run in the default suite (no Temporal server). progress() is a plain
read-only method over `self._status`/`self._log`, so the workflow can be
constructed and queried directly. Note WorkflowLogger.print_status routes
through temporalio.workflow.logger, which only works inside a workflow event
loop -- so the tests populate the log sink directly / via the context-free
capture path rather than calling print_status here.
"""
from temporal.workflow import ModuleGenerationWorkflow
from temporal.logger import WorkflowLogger, _MAX_SINK_LINES
from agents.status import ModuleGenerationStatus


def _sample_status() -> ModuleGenerationStatus:
    status = ModuleGenerationStatus(tool_name='SamTool', module_directory='/tmp/samtool_x')
    status.research_data = {'research': 'some research'}
    status.artifacts_status = {
        'wrapper': {'generated': True, 'validated': True, 'attempts': 1, 'errors': []},
        'manifest': {'generated': True, 'validated': False, 'attempts': 2, 'errors': ['boom']},
    }
    status.input_tokens = 100
    status.output_tokens = 40
    return status


def test_progress_before_run_returns_empty():
    wf = ModuleGenerationWorkflow()
    assert wf.progress() == {'status': None, 'log': [], 'awaiting_upload_approval': False}


def test_progress_reflects_status_and_log():
    wf = ModuleGenerationWorkflow()
    wf._status = _sample_status()
    wf._log.extend(['INFO: Generating wrapper', 'ERROR: manifest failed'])

    p = wf.progress()

    assert p['status']['tool_name'] == 'SamTool'
    assert p['status']['research_complete'] is True
    assert p['status']['artifacts_status']['wrapper']['validated'] is True
    assert p['status']['artifacts_status']['manifest']['validated'] is False
    assert p['status']['input_tokens'] == 100
    assert p['log'] == ['INFO: Generating wrapper', 'ERROR: manifest failed']


def test_progress_log_is_a_copy_not_the_live_list():
    """The query must not hand out the workflow's mutable internal list."""
    wf = ModuleGenerationWorkflow()
    wf._log.append('INFO: one')
    snapshot = wf.progress()['log']
    wf._log.append('INFO: two')
    assert snapshot == ['INFO: one']   # snapshot unaffected by later mutation


def test_logger_capture_bounds_to_max_lines():
    sink: list[str] = []
    logger = WorkflowLogger(sink=sink)
    for i in range(_MAX_SINK_LINES + 50):
        logger._capture(f"line {i}")
    assert len(sink) == _MAX_SINK_LINES
    assert sink[-1] == f"line {_MAX_SINK_LINES + 49}"   # newest kept
    assert sink[0] == "line 50"                          # oldest 50 dropped


def test_logger_without_sink_is_a_noop_capture():
    logger = WorkflowLogger()   # no sink
    logger._capture("anything")  # must not raise


# ---------------------------------------------------------------------------
# Human-in-the-loop upload approval gate (temporal/PHASE5.md Workstream D).
# approve_upload/reject_upload are plain @workflow.signal methods -- no
# Temporal server needed to call them directly, same as progress() above.
# ---------------------------------------------------------------------------

def test_progress_reflects_awaiting_upload_approval():
    wf = ModuleGenerationWorkflow()
    assert wf.progress()['awaiting_upload_approval'] is False
    wf._awaiting_upload_approval = True
    assert wf.progress()['awaiting_upload_approval'] is True


def test_approve_upload_sets_decision_only_when_awaiting():
    wf = ModuleGenerationWorkflow()
    wf.approve_upload()
    assert wf._upload_decision is None   # no-op: nothing was pending

    wf._awaiting_upload_approval = True
    wf.approve_upload()
    assert wf._upload_decision == 'approved'


def test_reject_upload_sets_decision_only_when_awaiting():
    wf = ModuleGenerationWorkflow()
    wf.reject_upload()
    assert wf._upload_decision is None   # no-op: nothing was pending

    wf._awaiting_upload_approval = True
    wf.reject_upload()
    assert wf._upload_decision == 'rejected'
