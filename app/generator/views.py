"""
Views for the GenePattern Module Generator Web UI.

Module generation runs are launched and tracked via the Temporal client
(temporal/PHASE4.md 4.4) -- the workflow itself is the single source of truth
for both in-flight progress and the final result. There is no subprocess, no
status.json, and no --resume: a run is submitted with workflow_id=<module
directory name>, so any view can reconstruct the right workflow handle
directly from the directory name alone, with nothing to keep in sync.
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps

from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404
from django.conf import settings
from django.contrib import messages
from asgiref.sync import async_to_sync

from agents import effects
from agents.example_data import ExampleDataResolver
from agents.logger import Logger
from temporal.client import start_module_generation, get_workflow_state, connect as temporal_connect, decide_upload


def login_required(view_func):
    """Decorator to require login for views."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get('username'):
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper


def get_user_stats_path(username):
    """Get path to user stats JSON file."""
    user_dir = settings.GENERATED_MODULES_DIR / username
    return user_dir / 'user_stats.json'


def load_user_stats(username):
    """Load user statistics from JSON file."""
    stats_path = get_user_stats_path(username)
    if stats_path.exists():
        try:
            with open(stats_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {'run_count': 0, 'runs': []}


def save_user_stats(username, stats):
    """Save user statistics to JSON file."""
    stats_path = get_user_stats_path(username)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2)


def get_max_runs_for_user(username):
    """Get max runs for a user (check user-specific override first)."""
    stats = load_user_stats(username)
    return stats.get('max_runs', settings.MAX_RUNS_PER_USER)


def _filesystem_warning(status_data):
    """None if the workflow's module_directory is visible from this process,
    otherwise a message explaining why not.

    The worker writes every artifact to its own local disk (temporal/
    CONSIDERATIONS.md gotcha #2); Django only sees that output if it shares a
    filesystem with the worker. Surfacing this on every poll -- while a run is
    still in progress, not just after it finishes -- catches a misconfigured
    deployment early instead of after a run completes with the UI unable to
    show or download the resulting files (temporal/PHASE5.md Workstream A3).
    """
    module_directory = (status_data or {}).get('module_directory')
    if not module_directory or Path(module_directory).exists():
        return None
    return (
        f"The workflow reports its output at '{module_directory}', but that path doesn't "
        f"exist on this server. The worker and the Django process must share a filesystem -- "
        f"check that a shared volume is mounted at the same path on both."
    )


def _map_state_to_status(state):
    """Map temporal.client.get_workflow_state()'s raw dict into the
    {status, running, data} shape the frontend JS already expects (the same
    shape the old status.json-derived response had, since both ultimately
    come from ModuleGenerationStatus.to_dict())."""
    if state is None:
        return {'status': 'not_found', 'running': False}

    exec_status = state['execution_status']

    if exec_status == 'RUNNING':
        progress = state.get('progress') or {}
        status_data = progress.get('status')
        return {
            'status': 'running', 'running': True, 'data': status_data,
            'filesystem_warning': _filesystem_warning(status_data),
            'awaiting_upload_approval': progress.get('awaiting_upload_approval', False),
        }

    if exec_status == 'COMPLETED':
        result = state.get('result') or {}
        status_data = result.get('status') or {}
        if status_data.get('error_messages'):
            derived = 'error'
        else:
            artifacts = status_data.get('artifacts_status') or {}
            if artifacts:
                all_valid = all(a.get('validated', False) for a in artifacts.values())
                derived = 'success' if all_valid else 'error'
            elif not status_data.get('research_complete') or not status_data.get('planning_complete'):
                derived = 'error'
            else:
                derived = 'success' if result.get('success') else 'error'
        return {
            'status': derived, 'running': False, 'data': status_data,
            'filesystem_warning': _filesystem_warning(status_data),
        }

    # FAILED / TERMINATED / CANCELED / TIMED_OUT: the workflow itself never
    # reached a normal return (ModuleGenerationWorkflow.run() catches pipeline
    # failures internally and returns success=False rather than raising, so
    # landing here means something at the infrastructure level went wrong --
    # worker crash, manual termination, execution timeout).
    progress = state.get('progress') or {}
    return {'status': 'error', 'running': False, 'data': progress.get('status')}


async def _gather_module_states(module_dirs):
    """Concurrently fetch workflow state for every directory, sharing one
    Temporal client connection."""
    client = await temporal_connect()
    results = await asyncio.gather(
        *(get_workflow_state(module_dir, client=client) for module_dir in module_dirs)
    )
    return dict(zip(module_dirs, results))


def get_user_modules(username):
    """Get list of generated modules for a user, with live status from Temporal."""
    user_dir = settings.GENERATED_MODULES_DIR / username
    if not user_dir.exists():
        return []

    module_dirs = sorted(
        (item.name for item in user_dir.iterdir() if item.is_dir() and not item.name.startswith('.')),
        reverse=True,
    )
    if not module_dirs:
        return []

    states = async_to_sync(_gather_module_states)(module_dirs)

    modules = []
    for name in module_dirs:
        mapped = _map_state_to_status(states.get(name))
        status = mapped['status']
        if status == 'not_found':
            # A directory with no matching workflow (outside Temporal's retention
            # window, or predating this workflow_id=module_dir scheme).
            status = 'unknown'
        modules.append({
            'name': name,
            'status': status,
            'path': str(user_dir / name),
        })
    return modules


def login_view(request):
    """Handle user login."""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if username in settings.AUTH_USERS and settings.AUTH_USERS[username] == password:
            request.session['username'] = username
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid username or password')

    return render(request, 'generator/login.html')


def logout_view(request):
    """Handle user logout."""
    request.session.flush()
    return redirect('login')


@login_required
def dashboard(request):
    """Main dashboard view with form and module list."""
    username = request.session.get('username')
    modules = get_user_modules(username)
    stats = load_user_stats(username)
    max_runs = get_max_runs_for_user(username)

    context = {
        'username': username,
        'modules': modules,
        'run_count': stats.get('run_count', 0),
        'max_runs': max_runs,
    }

    return render(request, 'generator/dashboard.html', context)


@login_required
def debug_view(request):
    """Debug view to diagnose module loading issues."""
    username = request.session.get('username')
    user_dir = settings.GENERATED_MODULES_DIR / username

    debug_info = {
        'username': username,
        'GENERATED_MODULES_DIR': str(settings.GENERATED_MODULES_DIR),
        'user_dir': str(user_dir),
        'user_dir_exists': user_dir.exists(),
        'items_in_user_dir': [],
        'directories': [],
        'modules_from_function': [],
    }

    if user_dir.exists():
        try:
            items = list(user_dir.iterdir())
            debug_info['items_in_user_dir'] = [str(item.name) for item in items]
            debug_info['directories'] = [
                str(item.name) for item in items
                if item.is_dir() and not item.name.startswith('.')
            ]
        except Exception as e:
            debug_info['error_listing_dir'] = str(e)

    try:
        modules = get_user_modules(username)
        debug_info['modules_from_function'] = [
            {'name': m['name'], 'status': m['status']} for m in modules
        ]
    except Exception as e:
        debug_info['error_getting_modules'] = str(e)

    return JsonResponse(debug_info, json_dumps_params={'indent': 2})


@login_required
def generate_module(request):
    """Handle module generation request: submit a ModuleGenerationWorkflow and
    return immediately -- the workflow runs durably on the Temporal server/
    worker, decoupled from this request's lifetime."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    username = request.session.get('username')
    stats = load_user_stats(username)
    max_runs = get_max_runs_for_user(username)

    if stats.get('run_count', 0) >= max_runs:
        return JsonResponse({
            'error': f'You have reached your maximum of {max_runs} runs. Contact an administrator to increase your limit.'
        }, status=403)

    form_data = {
        'name': request.POST.get('name', '').strip(),
        'version': request.POST.get('version', '').strip(),
        'language': request.POST.get('language', '').strip(),
        'description': request.POST.get('description', '').strip(),
        'repository_url': request.POST.get('repository_url', '').strip(),
        'documentation_url': request.POST.get('documentation_url', '').strip(),
        'instructions': request.POST.get('instructions', '').strip(),
        'base_image': request.POST.get('base_image', '').strip(),
    }

    # Collect example data items: uploaded file paths (with optional hints) + typed URLs (with optional hints).
    # The JS encodes hints inline as "value::hint" before putting them in hidden inputs.
    data_file_paths = request.POST.getlist('data_file_path')
    data_urls = [u.strip() for u in request.POST.getlist('data_url') if u.strip()]
    data_items = data_file_paths + data_urls

    # Optional GenePattern upload (temporal/PHASE5.md Workstream D). gp_server/
    # gp_user must both be set for the workflow to attempt an upload at all --
    # matches the CLI's --gp-server/--gp-user gate.
    gp_server = request.POST.get('gp_server', '').strip() or None
    gp_user = request.POST.get('gp_user', '').strip() or None
    gp_password = request.POST.get('gp_password', '') or None
    require_upload_approval = request.POST.get('require_upload_approval') == 'on'

    if not form_data['name']:
        return JsonResponse({'error': 'Tool name is required'}, status=400)

    output_dir = settings.GENERATED_MODULES_DIR / username
    output_dir.mkdir(parents=True, exist_ok=True)

    # effects.make_module_dir (not a plain f-string + mkdir) so two concurrent
    # submissions for the same tool name landing in the same wall-clock second
    # get distinct directories -- and distinct workflow_ids, since that's
    # derived from module_dir below -- instead of silently colliding
    # (temporal/PHASE5.md Workstream E).
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    module_path = Path(effects.make_module_dir(str(output_dir), form_data['name'], timestamp))
    module_dir = module_path.name

    # Resolve example data (uploaded files already sit under module_path;
    # URLs are downloaded synchronously here, same as the CLI's --data resolution).
    example_data = []
    if data_items:
        resolver = ExampleDataResolver(Logger())
        example_data = [item.to_dict() for item in resolver.resolve(data_items)]

    tool_info = dict(form_data)
    tool_info['example_data'] = example_data
    tool_info['module_dir'] = str(module_path)
    tool_info['output_dir'] = str(output_dir)

    # See generate-module.py's run_via_temporal for why this needs a much
    # longer execution_timeout than the default: the upload-approval wait has
    # no timeout of its own, so the workflow's overall execution_timeout is
    # the real outer bound on how long it can wait on a human.
    execution_timeout = timedelta(days=7) if require_upload_approval else None

    try:
        async_to_sync(start_module_generation)(
            tool_info,
            workflow_id=module_dir,
            gp_server=gp_server,
            gp_user=gp_user,
            gp_password=gp_password,
            require_upload_approval=require_upload_approval,
            execution_timeout=execution_timeout,
        )
    except Exception as e:
        return JsonResponse({
            'error': f'Could not reach the Temporal server ({e}). Contact an administrator -- '
                     f'the module generation worker may be down.'
        }, status=503)

    stats['run_count'] = stats.get('run_count', 0) + 1
    stats.setdefault('runs', []).append({
        'timestamp': datetime.now().isoformat(),
        'name': form_data['name'],
    })
    save_user_stats(username, stats)

    return JsonResponse({
        'success': True,
        'message': 'Module generation started',
        'module': module_dir,
    })


@login_required
def upload_data_file(request):
    """Save an uploaded example-data file into the module's artifact directory.

    Expects a multipart POST with:
      - ``file``       — the file to upload
      - ``module_dir`` — the module directory name (not a full path)

    Returns JSON: {"path": "<absolute_path>", "filename": "<name>"}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    username = request.session.get('username')
    module_dir_name = request.POST.get('module_dir', '').strip()
    uploaded_file = request.FILES.get('file')

    if not module_dir_name:
        return JsonResponse({'error': 'module_dir is required'}, status=400)
    if not uploaded_file:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    # Resolve destination directory; create it if it doesn't exist yet
    dest_dir = settings.GENERATED_MODULES_DIR / username / module_dir_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Security: strip directory components from the filename
    safe_name = Path(uploaded_file.name).name
    dest_path = dest_dir / safe_name

    # Handle filename collisions
    if dest_path.exists():
        stem = dest_path.stem
        suffix = dest_path.suffix
        counter = 1
        while dest_path.exists():
            dest_path = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        with open(dest_path, 'wb') as f:
            for chunk in uploaded_file.chunks():
                f.write(chunk)
    except Exception as e:
        return JsonResponse({'error': f'Failed to save file: {e}'}, status=500)

    return JsonResponse({
        'path': str(dest_path.resolve()),
        'filename': dest_path.name,
    })


@login_required
def module_status(request, module_dir):
    """Get status of a module generation, sourced live from the Temporal
    workflow whose ID is the module directory name."""
    state = async_to_sync(get_workflow_state)(module_dir)
    return JsonResponse(_map_state_to_status(state))


@login_required
def upload_decision(request, module_dir):
    """Signal the workflow's pending upload-approval gate (temporal/PHASE5.md
    Workstream D). Expects POST {"decision": "approve"|"reject"}."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    decision = request.POST.get('decision', '').strip()
    if decision not in ('approve', 'reject'):
        return JsonResponse({'error': "decision must be 'approve' or 'reject'"}, status=400)

    sent = async_to_sync(decide_upload)(module_dir, decision == 'approve')
    if not sent:
        return JsonResponse({
            'error': f"Could not signal workflow '{module_dir}' -- it may have already finished."
        }, status=404)

    return JsonResponse({'success': True, 'decision': decision})


@login_required
def console_log(request, module_dir):
    """Return the workflow's current recent-log tail.

    Unlike the old subprocess-log-file tailing, this sources from the
    workflow's bounded in-memory log buffer (WorkflowLogger, capped at 500
    lines -- temporal/logger.py), which is replaced wholesale on each poll
    rather than growing indefinitely, so the client always gets the current
    tail rather than an ever-appending transcript.
    """
    state = async_to_sync(get_workflow_state)(module_dir)
    if state is None:
        return JsonResponse({'content': '', 'total_size': 0})

    progress = state.get('progress') or {}
    log_lines = progress.get('log') or []
    content = '\n'.join(log_lines)
    return JsonResponse({
        'content': content,
        'total_size': len(content.encode('utf-8')),
    })


@login_required
def module_files(request, module_dir):
    """Get list of files in a module directory."""
    username = request.session.get('username')
    module_path = settings.GENERATED_MODULES_DIR / username / module_dir

    if not module_path.exists():
        raise Http404("Module not found")

    files = []
    for item in sorted(module_path.iterdir()):
        if item.is_file():
            files.append({
                'name': item.name,
                'size': item.stat().st_size,
            })

    return JsonResponse({'files': files, 'module': module_dir})


@login_required
def download_file(request, module_dir, filename):
    """Download a file from a module directory."""
    username = request.session.get('username')
    file_path = settings.GENERATED_MODULES_DIR / username / module_dir / filename

    if not file_path.exists() or not file_path.is_file():
        raise Http404("File not found")

    # Security check - ensure path is within user's directory
    try:
        file_path.resolve().relative_to((settings.GENERATED_MODULES_DIR / username).resolve())
    except ValueError:
        raise Http404("File not found")

    return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=filename)
