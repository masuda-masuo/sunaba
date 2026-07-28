"""Language detection for sandbox containers.

Detects programming language(s) from file extension, project markers,
or explicit parameter.
"""

from __future__ import annotations

import fnmatch
import posixpath
import shlex
from dataclasses import dataclass
from typing import Any

from .paths import _get_extension
from .shell import _exec_run


@dataclass
class DetectionResult:
    """Result of language detection with scope information.

    Attributes:
        languages: Detected language set (e.g. {"python"}, {"python", "js"}, set()).
        scope: Language to root path mapping for polyglot projects
               (e.g. {"python": "backend/", "js": "frontend/"}).
        reason: Human-readable explanation when languages is empty (unknown).
    """

    languages: set[str]
    scope: dict[str, str]
    reason: str | None = None


# ===========================================================================
# Language detection (design-multilang-support.md S3)
# ===========================================================================

_LANGUAGE_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".go": "go",
    ".rs": "rust",
}

# (pattern, language) — pattern supports fnmatch glob (e.g. requirements*.txt)
_DETECTION_MARKERS: list[tuple[str, str]] = [
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements*.txt", "python"),
    ("Pipfile", "python"),
    ("tox.ini", "python"),
    ("package.json", "js"),
    ("tsconfig.json", "ts"),
]

_EXCLUDE_DIRS: tuple[str, ...] = (
    "node_modules", ".venv", "vendor", "dist", "build",
)


def detect_languages(
    container: Any,
    path: str,
    language: str | None = None,
    working_dir: str | None = None,
) -> DetectionResult:
    """Detect languages from a file or directory path inside the container.

    Priority:
    1. Explicit ``language=`` parameter (skip detection).
    2. File extension map, with tsconfig.json upward search for .ts files.
    3. Directory marker files and glob patterns (e.g. ``requirements*.txt``)
       via a single ``find`` exec for efficiency.

    For polyglot projects, returns a ``scope`` dict mapping each language
    to its root directory so tools can be run per sub-tree.

    Args:
        container: Docker container object.
        path: File or directory path (relative to ``working_dir``, or absolute).
        language: Explicit language override to skip detection.
        working_dir: Working directory for exec_run. When set, ``path`` is
            resolved relative to this directory.

    Returns:
        ``DetectionResult(languages, scope, reason)`` where *reason* is
        set when languages are empty (unknown).
    """
    if language:
        return DetectionResult(languages={language}, scope={language: path})

    ext = _get_extension(path)
    if ext in _LANGUAGE_EXT_MAP:
        lang = _LANGUAGE_EXT_MAP[ext]
        scope_dir = path
        if lang == "ts":
            tsconfig_dir = _find_tsconfig_upward(container, path, working_dir=working_dir)
            if tsconfig_dir is not None:
                scope_dir = tsconfig_dir
        return DetectionResult(languages={lang}, scope={lang: scope_dir})

    lang_scope: dict[str, str] = {}
    find_expr_parts = []
    for pattern, _ in _DETECTION_MARKERS:
        find_expr_parts.append(f'-name "{pattern}"')
    or_expr = " -o ".join(find_expr_parts)
    # Search both the target path and the working directory root for
    # project-level markers (e.g. pyproject.toml at repo root, found when
    # path="tests/").  The working_dir root is "." when workdir is set.
    search_paths = [shlex.quote(path)]
    if path not in (".", "", working_dir):
        search_paths.append(".")
    find_cmd = "; ".join(
        f"find {p} -maxdepth 1 \\( {or_expr} \\) 2>/dev/null"
        for p in search_paths
    )

    ec, out, _ = _exec_run(container, ["/bin/sh", "-c", find_cmd], workdir=working_dir)
    for line_out in out.strip().split("\n"):
        line_out = line_out.strip()
        if not line_out:
            continue
        basename = posixpath.basename(line_out)
        marker_dir = posixpath.dirname(line_out)
        for pattern, marker_lang in _DETECTION_MARKERS:
            if fnmatch.fnmatch(basename, pattern):
                lang_scope[marker_lang] = marker_dir
                break

    if not lang_scope:
        lang_scope = _deep_marker_scan(container, working_dir=working_dir)

    if not lang_scope:
        return DetectionResult(
            languages=set(),
            scope={},
            reason=(
                "No recognized project markers found in path. "
                "Use language= parameter to force a specific toolchain."
            ),
        )

    languages = set(lang_scope.keys())
    return DetectionResult(languages=languages, scope=lang_scope)


def _deep_marker_scan(
    container: Any, working_dir: str | None = None
) -> dict[str, str]:
    """Second-chance marker scan, 2-3 levels deep, when depth 1 found nothing.

    The primary scan looks at ``maxdepth 1`` of the target path and the
    working root.  A repository whose project manifest lives in a
    subdirectory -- e.g. a Rust repo carrying its Cargo workspace in
    ``prototypes/`` -- then detects *nothing*, and an empty detection makes
    the pre-publish gate pass with "no languages detected" and the test
    layer report ``no_tests``: an end-to-end false green measured on a real
    repository (the js dispatch shipped with exactly this class of gap,
    #738).  Deepening only the nothing-found case keeps every currently
    detected layout byte-identical while converting that silent pass into a
    real run.

    Returns the same ``{language: root_dir}`` shape as the primary scan,
    choosing the shallowest match per language so a workspace root wins
    over its member crates.
    """
    prune = " -o ".join(
        f'-name "{d}"' for d in (*_EXCLUDE_DIRS, "target", ".git")
    )
    names = " -o ".join(f'-name "{pattern}"' for pattern, _ in _DETECTION_MARKERS)
    find_cmd = (
        f"find . -mindepth 2 -maxdepth 3 \\( {prune} \\) -prune"
        f" -o \\( {names} \\) -print 2>/dev/null"
    )
    ec, out, _ = _exec_run(container, ["/bin/sh", "-c", find_cmd], workdir=working_dir)

    hits: list[tuple[int, str, str]] = []  # (depth, marker_dir, language)
    for line_out in out.strip().split("\n"):
        line_out = line_out.strip()
        if not line_out:
            continue
        basename = posixpath.basename(line_out)
        for pattern, marker_lang in _DETECTION_MARKERS:
            if fnmatch.fnmatch(basename, pattern):
                hits.append(
                    (line_out.count("/"), posixpath.dirname(line_out), marker_lang)
                )
                break

    lang_scope: dict[str, str] = {}
    for _depth, marker_dir, marker_lang in sorted(hits):
        lang_scope.setdefault(marker_lang, marker_dir)
    return lang_scope


def _find_tsconfig_upward(container: Any, file_path: str, working_dir: str | None = None) -> str | None:
    """Search upward from *file_path* for a tsconfig.json.

    Returns the directory containing tsconfig.json, or None if not found.
    """
    current = posixpath.dirname(posixpath.abspath(file_path))
    while True:
        ec, out, _ = _exec_run(
            container,
            ["/bin/sh", "-c", f'test -f {shlex.quote(posixpath.join(current, "tsconfig.json"))} && echo found || echo notfound'],
            workdir=working_dir,
        )
        out = out.strip()
        if "found" in out:
            return current
        parent = posixpath.dirname(current)
        if parent == current:
            return None
        current = parent

