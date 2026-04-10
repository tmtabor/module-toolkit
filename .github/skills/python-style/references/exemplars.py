"""
EXEMPLAR 1: High-Density Logic, Guard Clauses, and Aligned Comment Columns
Demonstrates: Single-line guard clause, compact try/except, block comments labeling
phases, and vertically-aligned trailing comments on a sequence of method calls.
"""

def poll(self) -> None:
    """Poll the server for job info and refresh the widget."""
    if not self.initialized(): return

    try:
        self.job.get_info()
    except HTTPError:
        self.name = 'Error Loading Job'
        self.error = f'Error loading job #{self.job.job_number}'
        return

    self.name = f'{self.job.job_number}. {self.job.task_name}'
    self.origin = server_name(self.job.server_data.url)
    self.status = self.status_text()
    self.description = self.submitted_text()
    self.files = self.files_list()
    self.visualization = self.visualizer()

    self.register_data()       # Register any output files
    self.handle_notification() # Send notification if completed
    self.poll_if_needed()      # Begin polling if pending or running


"""
EXEMPLAR 2: Initializer with Type Hints and Guard Clause
Demonstrates: Typed __init__, single-line guard, dict merge with | operator.
"""

def __init__(self, task=None, origin: str = '', id: str = '', **kwargs) -> None:
    """Initialize the task widget."""
    self.task = task
    self.kwargs = kwargs

    if self.task is None:
        self.handle_error_task('No module specified.')
        return

    self.function_wrapper = self.create_function_wrapper(self.task)
    self.parameter_spec = self.create_param_spec(self.task, kwargs)

    ui_args: dict = {
        'color': session_color(self.task.server_data.url),
        'logo': GENEPATTERN_LOGO,
        'parameters': self.parameter_spec,
    }
    # Caller-supplied kwargs take precedence over widget defaults
    ui_args = ui_args | kwargs

    self.attach_menu_items()
    self.attach_sharing()
    self.attach_terminate()


"""
EXEMPLAR 3: Conditional Menu Item Mutation
Demonstrates: Boolean variable naming, dict comprehension for exclusion, | for inclusion.
"""

def attach_terminate(self) -> None:
    """Show or hide the Terminate Job menu option based on current job status."""
    is_running = self.status in ('Pending', 'Running')

    if is_running and 'Terminate Job' not in self.extra_menu_items:
        self.extra_menu_items = self.extra_menu_items | {
            'Terminate Job': {'action': 'method', 'code': 'terminate_job'},
        }

    if not is_running and 'Terminate Job' in self.extra_menu_items:
        self.extra_menu_items = { k:v for k, v in self.extra_menu_items.items() if k != 'Terminate Job'}


"""
EXEMPLAR 4: Aligned Trailing Comments on Single-Line Conditionals and Assignments
Demonstrates: Type hints, single-line guard, aligned comment column on complex lines.
"""

def __init__(self, spec: str | dict | None = None) -> None:
    super(Project, self).__init__()                                     # Call the superclass constructor
    if spec is None: return                                             # If no spec, nothing left to do

    # If a spec was given, parse it and instantiate this project with the data
    try:
        if isinstance(spec, str): spec = json.loads(spec)               # Parse the JSON, if necessary
        for key in Project.__dict__:                                    # Assign attributes from the json
            if key in spec:
                if isinstance(spec[key], str): setattr(self, key, spec[key].strip())
                else: setattr(self, key, spec[key])
    except json.JSONDecodeError:
        raise SpecError('Error parsing json')
