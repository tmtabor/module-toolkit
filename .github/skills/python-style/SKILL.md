---
name: python-style
description: Always use this skill when writing, refactoring or reviewing Python.
---

## 1. Triggering Context
* Activate this skill for any task involving writing, refactoring, or reviewing Python.
* Prioritize this style even if standard PEP8 suggestions would dictate more vertical expansion.

## 2. Structural Philosophy
* **Functional Density:** Prioritize compact but readable logic. Avoid unnecessary vertical expansion by grouping related logical checks.
* **Guard Clauses:** Always handle invalid states or "empty" checks at the start of a function using early returns.
* **Visual Narrative:** Use comments to guide the reader through the code — label logical phases with block comments and annotate complex or non-obvious lines with trailing inline comments.

## 3. Formatting & Whitespace
* **The Single-Line Conditional:** When an `if` statement has a single-line result (especially a return or assignment), keep it on one line rather than expanding to multiple lines.
    * *Example:* `if not self.job: return`.
* **The Single-Line Else:** For simple binary logic, keep the `else` on one line.
    * *Example:* `else: return ''`.
* **Vertical Density:**
    * Use one blank line between methods within a class.
    * Use two blank lines between top-level class definitions or standalone functions.
* **Horizontal Alignment:** Occasionally align inline `#` comments vertically to create a readable column, particularly for a group of sequential method calls. Do not pad `=` signs to force alignment on ordinary assignments.

## 4. Commenting Style
* **Descriptive and Helpful:** Comments should help the reader follow multi-step logic. Use them freely to label phases, describe what a group of calls does, or explain a complex line. Avoid only the truly redundant (e.g., `x += 1  # increment x`).
    * *Good:* `# Add the job information to the widget` (above a block of assignments).
    * *Good:* `self.function_wrapper = self.create_function_wrapper(self.task)  # Create run task function`.
* **Trailing Inline Placement:** When a comment is warranted, place it on the same line as the code, separated by at least two spaces. Use comments as a parallel track to the code, often aligned to the right.
    * *Example:* `self.set_color()  # Derived from subscription tier, not raw input`.
* **Aligned Comment Columns:** For a group of sequential method calls or assignments, align the trailing `#` comments vertically into a column. Each comment briefly labels what that call does, making the sequence readable at a glance.
    ```python
    self.register_data()       # Register any output files
    self.handle_notification() # Send notification if completed
    self.poll_if_needed()      # Begin polling if pending or running
    ```
* **Narrative Block Comments:** Use block comments to introduce distinct logical phases or steps within a longer function. Each major step should have a label.
    * *Example:* `# Add the job information to the widget`, `# Build the job sharing form by iterating over groups`.
* **Docstrings:** Use triple double-quotes `"""`. For simple single-purpose methods, keep the summary on one line. For complex functions use Google-style with `Args:`, `Returns:`, and `Raises:` sections.

## 5. Naming Conventions
* **Classes:** Use `PascalCase` (e.g., `GPJobWidget`, `TaskTool`).
* **Methods & Variables:** Use `snake_case` (e.g., `job_origin`, `session_index`).
* **Internal Helpers:** Use a single leading underscore for methods meant for internal class logic (e.g., `_generate_doc_link`).
* **Booleans:** Use descriptive names that imply state (e.g., `sharing_displayed`, `initialized`).

## 6. Idioms & Patterns
* **Dictionary Merging:** Use the `|` operator (Python 3.9+) to merge dicts.
    * *Example:* `config = defaults | overrides`.
* **F-Strings:** Use f-strings for all variable interpolation and string building.
* **Attribute Checks:** Use `hasattr()` to create flexible "shims" for different object versions.

## 7. Functional Decomposition
* **Method Length:** Most methods should be between 3 and 15 lines.
* **Static Methods:** Use `@staticmethod` for utility-style logic that belongs to the class's domain but doesn't require state.
    * *Example:* `set_logo(kwargs={})`.
* **Lambda Callbacks:** Use inline lambdas for simple event handlers or timer calls.
    * *Example:* `timer = Timer(15.0, lambda: self.poll())`.

## 8. Type Hints
* **Always annotate** function and method signatures — both parameters and return types.
* **Python 3.10+ syntax:** Use `list[str]`, `dict[str, int]`, `X | None` (never `List`, `Dict`, or `Optional`).
* **`None` returns:** Annotate as `-> None` explicitly on all methods that return nothing.
* **Callables:** Use `Callable[[ArgType], ReturnType]` from `collections.abc`.
    * *Example:*
    ```python
    def process(self, items: list[int], callback: Callable[[int], bool] | None = None) -> dict[str, int]:
    ```

## 9. Imports
* **Grouping order** (one blank line between each group):
    1. Standard library
    2. Third-party (Django, Celery, etc.)
    3. Local apps
* **Lazy / inline imports:** Only use `from x import y` inside a function body to break a circular dependency. Always add a comment explaining why the import is deferred.
    * *Example:*
    ```python
    def get_limits(user):
        from accounts.utils import get_post_sync_limits  # avoids circular import with accounts app
        return get_post_sync_limits(user)
    ```
* Never use wildcard imports (`from x import *`).

---

### Implementation Example

> **Instructions:** When I ask you to write Python, follow this style.
>
> * **DON'T** write multi-line if-statements for simple returns.
> * **DO** annotate all function signatures with type hints.
> * **DO** use f-strings and early returns.
> * **DO** use comments to label phases and annotate complex lines.
> * **DO** use the `|` operator to merge dicts.

```python
def process_update(self, data: dict[str, int] | None = None) -> None:
    if data is None: return

    # Apply display state derived from user's subscription tier
    self.raw_data = data
    self.set_color()

    # Notify listeners, or log silently if notifications are suppressed
    if self.active: self.notify(f"Updated {len(data)} records")
    else: self.log("Silently updated")


def build_config(self, overrides: dict[str, str]) -> dict[str, str]:
    defaults = self.get_defaults()
    return defaults | overrides  # overrides win; callers supply per-request values
```