import os
import sys
from pathlib import Path

# views.py imports agents.*/temporal.* (the module-toolkit's top-level
# packages) directly to talk to the Temporal client (temporal/PHASE4.md 4.4).
# Running as `python app/manage.py` puts sys.path[0] at app/ (the script's own
# directory), not the repo root, so those packages wouldn't otherwise resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
