"""
Wrapper script introspection utilities.

Parses a generated wrapper script's argparse add_argument() calls to extract
the exact flag names (e.g. '--input-file') used at runtime.  The dockerfile
linter uses these to construct a realistic runtime validation command.
"""
import ast
import re
from pathlib import Path
from typing import Dict, Optional


def parse_wrapper_flags(wrapper_path: Path) -> Dict[str, Optional[str]]:
    """Read *wrapper_path* and parse its argparse add_argument flags.

    Convenience wrapper around ``parse_wrapper_flags_from_source`` for callers
    that don't need to route the read through an effects/activity seam. Callers
    that do (e.g. the pipeline orchestrator) should read the file via
    ``agents.effects.read_text_file`` and call ``parse_wrapper_flags_from_source``
    directly instead.
    """
    return parse_wrapper_flags_from_source(wrapper_path.read_text(encoding='utf-8'))


def parse_wrapper_flags_from_source(source: str) -> Dict[str, Optional[str]]:
    """Parse a wrapper script's argparse add_argument calls to extract flag names.

    Finds lines like::

        parser.add_argument('--input-file', ...)
        parser.add_argument('input_file', ...)   # positional

    Returns a dict mapping GenePattern parameter name (dashes/underscores
    normalised to canonical form) to the flag string (e.g. '--input-file') or
    ``None`` for positional arguments.

    Falls back to a regex-based scan if the source cannot be parsed as valid
    Python/R AST. Pure — no I/O — so it is safe to call from deterministic
    (future workflow) code.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _parse_wrapper_flags_regex(source)

    flags: Dict[str, Optional[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_add_argument = (
            (isinstance(func, ast.Attribute) and func.attr == 'add_argument') or
            (isinstance(func, ast.Name) and func.id == 'add_argument')
        )
        if not is_add_argument or not node.args:
            continue

        str_args = [
            a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
            for a in node.args
        ]
        str_args = [s for s in str_args if s is not None]

        long_flag = next((s for s in str_args if s.startswith('--')), None)
        positional = next((s for s in str_args if not s.startswith('-')), None)

        if long_flag:
            canon = long_flag.lstrip('-').replace('-', '.').replace('_', '.')
            flags[canon] = long_flag
            flags[long_flag.lstrip('-').replace('-', '_')] = long_flag
            flags[long_flag.lstrip('-').replace('-', '.')] = long_flag
        elif positional:
            canon = positional.replace('-', '.').replace('_', '.')
            flags[canon] = None
            flags[positional.replace('-', '_')] = None
            flags[positional.replace('-', '.')] = None

    return flags


def _parse_wrapper_flags_regex(source: str) -> Dict[str, Optional[str]]:
    """Regex fallback for parse_wrapper_flags when AST parsing fails."""
    flags: Dict[str, Optional[str]] = {}
    pattern = re.compile(r"""add_argument\s*\(\s*(['"])(--?[\w-]+)\1""")
    for match in pattern.finditer(source):
        flag = match.group(2)
        if flag.startswith('--'):
            canon = flag.lstrip('-').replace('-', '.')
            flags[canon] = flag
            flags[flag.lstrip('-').replace('-', '_')] = flag
    return flags

