"""Edit/Verify subsystem: minimal edit loop primitives for sandbox containers.

Provides low-level file editing and verification tools that operate on
disposable sandbox containers (not the real repository).  These tools
form the core of the minimal edit loop:

    search_in_container -> read_file_range -> apply_patch
    -> lint/type_check -> verify_in_container

By sending only diffs and reading only the needed lines, each iteration
consumes only hundreds of tokens instead of thousands.

Supports multi-language verification (Python / JS / TS / Go) with
language-aware dispatch, status envelopes, and proper gate logic.
"""

from __future__ import annotations

# Re-export: all public symbols from the extracted submodules.
# Consumers that ``from sunaba.edit_verify import X`` must continue to work.
from .detect import (  # noqa: F401
    _DETECTION_MARKERS,
    _EXCLUDE_DIRS,
    _LANGUAGE_EXT_MAP,
    DetectionResult,
    _find_tsconfig_upward,
    detect_languages,
)
from .edits import (  # noqa: F401
    _normalize_diff_for_git,
    apply_patch_to_file,
    edit_symbol_in_container,
    transform_file_in_container,
)
from .fileio import (  # noqa: F401
    _compute_file_size,
    _file_size_from_counts,
    _owner_for_write,
    read_file,
    read_file_lines,
    write_file,
)
from .gate import (  # noqa: F401
    _GATE_SENTINEL_RULES,
    _dispatch_layer,
    _flatten_layer,
    _flatten_test_layer,
    _gate_lint_runner,
    _gate_type_runner,
    _run_patch_targets_verify,
    run_lint_type_gate,
)
from .jstools import (  # noqa: F401
    _annotate_resolution,
    _detect_js_test_runner,
    _resolve_js_tool,
)
from .lint_runners import (  # noqa: F401
    _RUFF_SECURITY_IGNORE,
    _RUFF_SECURITY_SELECT,
    _run_eslint_verify,
    _run_go_vet_verify,
    _run_golangci_lint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
from .parsers import (  # noqa: F401
    _RUFF_SEVERITY_MAP,
    _TSC_TEXT_RE,
    _determine_lint_severity,
    _parse_eslint_output,
    _parse_go_vet_output,
    _parse_golangci_lint_output,
    _parse_pylint_output,
    _parse_pyright_output,
    _parse_ruff_output,
    _parse_tsc_json,
    _parse_tsc_text,
)
from .paths import ScopeWorkdir, _determine_scope, _get_extension, _is_test_file  # noqa: F401
from .results import (  # noqa: F401
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
    _envelope_skipped,
)
from .shell import _GO_ENV, _SANDBOX_ENV, _path_display, _quote_path  # noqa: F401
from .single_file import (  # noqa: F401
    _run_js_linter,
    _run_pylint,
    _run_python_linter,
    _run_python_typecheck,
    _run_ts_typecheck,
    lint_file,
    type_check_file,
)
from .test_runners import (  # noqa: F401
    _DISPATCH,
    _run_go_test_verify,
    _run_jest_verify,
    _run_npm_test_verify,
    _run_pytest_verify,
)

__all__ = [
    # detect
    "_DETECTION_MARKERS",
    "_EXCLUDE_DIRS",
    "_LANGUAGE_EXT_MAP",
    "DetectionResult",
    "_find_tsconfig_upward",
    "detect_languages",
    # edits
    "_normalize_diff_for_git",
    "apply_patch_to_file",
    "edit_symbol_in_container",
    "transform_file_in_container",
    # fileio
    "_compute_file_size",
    "_file_size_from_counts",
    "_owner_for_write",
    "read_file",
    "read_file_lines",
    "write_file",
    # gate
    "_dispatch_layer",
    "_GATE_SENTINEL_RULES",
    "_gate_lint_runner",
    "_gate_type_runner",
    "_flatten_layer",
    "_flatten_test_layer",
    "_run_patch_targets_verify",
    "run_lint_type_gate",
    # jstools
    "_annotate_resolution",
    "_detect_js_test_runner",
    "_resolve_js_tool",
    # lint_runners
    "_RUFF_SECURITY_IGNORE",
    "_RUFF_SECURITY_SELECT",
    "_run_eslint_verify",
    "_run_go_vet_verify",
    "_run_golangci_lint_verify",
    "_run_pyright_verify",
    "_run_ruff_verify",
    "_run_tsc_verify",
    # parsers
    "_RUFF_SEVERITY_MAP",
    "_TSC_TEXT_RE",
    "_determine_lint_severity",
    "_parse_eslint_output",
    "_parse_go_vet_output",
    "_parse_golangci_lint_output",
    "_parse_pylint_output",
    "_parse_pyright_output",
    "_parse_ruff_output",
    "_parse_tsc_json",
    "_parse_tsc_text",
    # paths
    "ScopeWorkdir",
    "_determine_scope",
    "_get_extension",
    "_is_test_file",
    # results
    "VerifyResult",
    "_envelope_error",
    "_envelope_not_available",
    "_envelope_ok",
    "_envelope_skipped",
    # shell
    "_GO_ENV",
    "_SANDBOX_ENV",
    "_path_display",
    "_quote_path",
    # single_file
    "lint_file",
    "_run_python_linter",
    "_run_js_linter",
    "type_check_file",
    "_run_python_typecheck",
    "_run_ts_typecheck",
    "_run_pylint",
    # test_runners
    "_DISPATCH",
    "_run_go_test_verify",
    "_run_jest_verify",
    "_run_npm_test_verify",
    "_run_pytest_verify",
]
