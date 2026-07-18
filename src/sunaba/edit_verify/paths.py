"""Container-independent path utilities and scope resolution.

Provides test-file detection, extension extraction, and scope/workdir
determination — all pure functions with no container dependency.
"""

from __future__ import annotations

import posixpath
from typing import NamedTuple


def _is_test_file(file_path: str) -> bool:
    """Check whether *file_path* follows test-file naming/directory conventions.

    Heuristic:
    - File basename starts with ``test_`` or contains ``_test`` in its stem (Python/Go).
    - File basename contains ``.test.`` or ``.spec.`` (JS/TS).
    - Path contains ``/tests/``, ``/test/``, or ``/__tests__/`` segment.
    """
    norm = posixpath.normpath(file_path)
    basename = posixpath.basename(norm)
    # Strip the extension so suffix matching works for e.g. ``utils_test.go``.
    stem = basename.rsplit(".", 1)[0]
    if stem.startswith("test_") or "_test" in stem:
        return True
    if ".test." in basename or ".spec." in basename:
        return True
    parts = norm.split(posixpath.sep)
    if "tests" in parts or "test" in parts or "__tests__" in parts:
        return True
    return False


def _get_extension(file_path: str) -> str:
    """Return the lowercase file extension including the dot."""
    _, dot_ext = file_path.rstrip("/").rsplit(".", 1) if "." in file_path else ("", "")
    return f".{dot_ext.lower()}" if dot_ext else ""


class ScopeWorkdir(NamedTuple):
    """``(scope, workdir)`` tuple with named field access.

    Return type for :func:`_determine_scope`.
    """
    scope: str
    workdir: str


def _determine_scope(file_path: str) -> ScopeWorkdir:
    """Determine the project scope and working directory for lint/type-check.

    Returns a ``(scope, workdir)`` tuple:

    * *scope* — path to pass to the tool (e.g. ``"src"``, ``"."``).
    * *workdir* — project-root directory that should be the CWD when
      running scope checks (e.g. ``"/app"``, ``"."``).

    Both values are derived from *file_path* so callers no longer need
    to call :func:`_resolve_workdir` separately.

    Examples
    --------
    >>> _determine_scope("/app/src/foo.py")
    ('src', '/app')
    >>> _determine_scope("src/foo.py")
    ('src', '.')
    >>> _determine_scope("/home/foo.py")
    ('/home', '/home')
    >>> _determine_scope("foo.py")
    ('.', '.')
    """
    normalized = file_path.replace("\\", "/")
    idx = normalized.find("/src/")
    if idx != -1:
        return ScopeWorkdir("src", normalized[:idx] or ".")
    if normalized.startswith("src/"):
        return ScopeWorkdir("src", ".")
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    scope = parent or "."
    return ScopeWorkdir(scope, scope)
