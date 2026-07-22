# GenePattern Module Generator Web UI

A minimal Django web application that provides a frontend for the module-generation pipeline.

> **Requires a running Temporal server + worker.** The UI submits each generation as a
> `ModuleGenerationWorkflow` via `temporal.client` and polls the workflow's `progress()` query for
> live status/log output — there is no subprocess, no `status.json`, and no in-process fallback.
> Start a server (`temporal server start-dev`) and a worker (`uv run python -m temporal.worker`,
> from the repo root) before running `manage.py runserver`. This is a change from the earlier
> `--legacy`-based UI (see `temporal/PHASE4.md` 4.4) — the CLI's `--legacy` in-process path still
> exists for no-infra use, but the web UI no longer uses it.

## Features

- Simple username/password authentication (configured via `.env`)
- Web form to run the module generation pipeline
- Live status and log streaming from the Temporal workflow
- View and download generated module files
- Track user run counts with configurable limits

There is no "resume a previous run" feature — a failed or interrupted run is retried by starting a
fresh one from the form (the CLI's `--resume` flag this used to map to was removed entirely; see
`temporal/PHASE4.md` 4.5).

## Setup

1. Install dependencies (from the repo root):
   ```bash
   uv sync --extra app
   ```

2. Configure the `.env` file (repo root `.env`, not `app/.env` — Django loads `app/.env` if
   present, but the Temporal client reads `TEMPORAL_ADDRESS` etc. from the root one; simplest to
   put everything in the root `.env`):
   - `SECRET_KEY`: Django secret key (change in production!)
   - `DEBUG`: Set to `False` in production
   - `USERS`: Comma-separated list of usernames
   - `PASSWORDS`: Comma-separated list of passwords (matching order with USERS)
   - `MAX_RUNS_PER_USER`: Maximum runs allowed per user (default: 20)
   - `MODULE_TOOLKIT_PATH`: Path to the module-toolkit directory
   - `TEMPORAL_ADDRESS`: Temporal server address (default: `localhost:7233`)

3. Start a Temporal server and a worker (see the root README's "Durable execution with Temporal"):
   ```bash
   temporal server start-dev
   uv run python -m temporal.worker
   ```

4. Run the development server, from the repo root:
   ```bash
   uv run --extra app python app/manage.py runserver
   ```

5. Access the application at `http://localhost:8000`

## User Run Tracking

User runs are tracked in `generated-modules/{username}/user_stats.json`. Admins can edit this file to:
- View run history
- Override max runs for specific users by adding `"max_runs": <number>`

## File Structure

```
webapp/
├── .env                 # Environment configuration
├── README.md            # This file
├── requirements.txt     # Python dependencies
├── manage.py            # Django management script
├── config/              # Django project settings
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── generator/           # Main application
    ├── __init__.py
    ├── views.py
    ├── urls.py
    └── templates/
        └── generator/
            ├── login.html
            └── dashboard.html
```

## Notes

- No database is used; all data is read from the filesystem
- Bootstrap 5.2 is loaded from CDN
- Generated modules are stored in `{MODULE_TOOLKIT_PATH}/generated-modules/{username}/`

