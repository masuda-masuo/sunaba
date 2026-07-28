"""Lint and type-check runners for verification."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any

from .jstools import _annotate_resolution, _resolve_js_tool
from .parsers import (
    _determine_lint_severity,
    _parse_clippy_output,
    _parse_eslint_output,
    _parse_go_vet_output,
    _parse_golangci_lint_output,
    _parse_pyright_output,
    _parse_ruff_output,
    _parse_tsc_json,
    _parse_tsc_text,
)
from .results import (
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
    _envelope_skipped,
)
from .shell import _GO_ENV, _RUST_ENV, _SANDBOX_ENV, _exec_run, _path_display, _quote_path

# ---------------------------------------------------------------------------
# Linter / Type checker / Test / Scan runners
# ---------------------------------------------------------------------------
# Each runner now returns a VerifyResult envelope.  The ``|| true`` and
# ``2>/dev/null`` silencing has been removed: exit codes are inspected
# directly, and stderr is captured (not discarded).
#
# Runner return semantics:
# - exit 0   + output -> status "findings" (parse output)
# - exit 0   + no output -> status "ok" (clean)
# - exit 1   (many tools use this for "findings") -> status "findings"
# - exit 127             -> status "not_available"
# - exit other           -> status "error" (unexpected failure)
# - "skipped" is only for intentional non-execution (e.g. go type layer)


_RUFF_SECURITY_SELECT = ",".join([
    # shell injection
    "S102", "S602", "S603", "S604", "S605", "S606", "S607",
    # eval / exec
    "S307",
    # deserialization
    "S301", "S302", "S506",
    # TLS / SSL
    "S501", "S502", "S503", "S504",
    # weak hash
    "S324",
    # XML (XXE)
    "S313", "S314", "S315", "S316", "S317", "S318", "S319",
    # network safety
    "S113", "S507",
    # template injection
    "S701",
])

_RUFF_SECURITY_IGNORE = ",".join([
    # S101: assert is idiomatic in pytest and common for invariant guards in
    # application code (e.g. `assert x is not None`). Excluding it avoids
    # flooding test suites; the trade-off is that non-test assert-as-guard
    # patterns are not flagged. Acceptable because LLMs can reason about
    # assert usage from context without a dedicated lint signal.
    "S101",
    "S105", "S106", "S107",  # hardcoded-password heuristics — high false-positive rate
    "S311",          # random — usually non-security
    "S110", "S112",  # try-except-pass / try-except-continue — style, not security
])

def _run_ruff_verify(
    container: Any,
    path: str | Sequence[str],
    workdir: str | None = None,
    extra_select: bool = True,
    fix: bool = False,
) -> VerifyResult:
    """Run ruff on *path*.  Returns VerifyResult envelope.

    When *extra_select* is ``True`` (default) the curated security
    rule-set is layered on top of the project's own ruff config for
    awareness during editing.  Pass ``extra_select=False`` to run ruff
    with the project config **only** -- this mirrors CI's plain
    ``ruff check`` exactly and is what the pre-test gate uses, so the
    gate never diverges from CI on rules the project hasn't opted into.

    When *fix* is ``True`` ruff is invoked with ``--fix`` so it applies
    its safe autofixes (import sorting, unused-import removal, etc.) to
    *path* in place; the returned findings are the violations that
    remain *after* fixing (Issue #284).
    """
    # _quote_path uses shlex.quote (single-quote wrapping), so paths with
    # spaces or special characters are safe. SELECT/IGNORE are comma-separated
    # rule codes with no whitespace, so no quoting is needed for those.
    security_args = (
        f"--extend-select {_RUFF_SECURITY_SELECT} "
        f"--extend-ignore {_RUFF_SECURITY_IGNORE} "
        if extra_select
        else ""
    )
    fix_arg = "--fix " if fix else ""
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json "
            f"{fix_arg}"
            f"{security_args}"
            f"{_quote_path(path)}",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("ruff", "ruff not installed in container")
    if ec not in (0, 1):
        return _envelope_error("ruff", stderr_text.strip() or f"exit code {ec}", ec)

    findings = _parse_ruff_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _envelope_ok("ruff", findings, ec)


def _run_eslint_verify(
    container: Any, path: str | Sequence[str], workdir: str | None = None, fix: bool = False
) -> VerifyResult:
    """Run eslint on *path*.  Returns VerifyResult envelope.

    When *fix* is ``True`` eslint is invoked with ``--fix`` so it
    rewrites *path* in place; the returned findings are the problems
    that remain *after* fixing (Issue #284).

    Resolves ``node_modules/.bin/eslint`` before the image-baked global
    (Issue #588) so a repo pinned to a different eslint major never
    silently gets linted by the wrong version; the envelope's ``detail``
    always says which one ran.
    """
    fix_arg = "--fix " if fix else ""
    cmd, source = _resolve_js_tool(container, "eslint", workdir=workdir)
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} {fix_arg}--format json {_quote_path(path)}",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("eslint", "eslint not installed in container"), source, cmd
        )

    findings = _parse_eslint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))

    if ec not in (0, 1) and not findings:
        return _annotate_resolution(
            _envelope_error("eslint", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

    return _annotate_resolution(_envelope_ok("eslint", findings, ec), source, cmd)


def _run_golangci_lint_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run golangci-lint on *path*.  Falls back to go vet."""
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}golangci-lint run --out-format json {_quote_path(path)}",
        ],
    )
    if ec == 127:
        return _run_go_vet_verify(container, path)

    if ec not in (0, 1):
        # golangci-lint uses exit 2 for execution errors (config issues, etc.)
        return _envelope_error("golangci-lint", stderr_text.strip() or f"exit code {ec}", ec)

    findings = _parse_golangci_lint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("golangci-lint", findings, ec)


def _run_go_vet_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run go vet on *path*."""
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go vet {_quote_path(path)}",
        ],
    )

    if ec == 127:
        return _envelope_not_available("go vet", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go vet", stderr_text.strip() or f"exit code {ec}", ec)

    findings = _parse_go_vet_output(stdout_text + "\n" + stderr_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("go vet", findings, ec)


def _resolve_cargo_manifest(
    container: Any, path: str | Sequence[str], workdir: str | None = None
) -> str | None:
    """Locate the Cargo.toml the cargo runners should build against.

    cargo has no "lint/test this subdirectory" positional argument, so the
    manifest has to be named explicitly via ``--manifest-path`` -- otherwise
    cargo resolves the manifest from the *process cwd*, which the dispatch
    call sites pin to the git root.  A repository whose workspace manifest
    lives in a subdirectory (the shape of the first real Rust consumer:
    ``prototypes/Cargo.toml``) would then fail with "could not find
    Cargo.toml" -- and, before this helper existed, that failure exited 101
    with an empty JSON stream and was reported as a *green* lint run.

    Resolution order, all relative to *workdir*:

    1. ``<path>/Cargo.toml`` for each candidate in *path* -- the caller's
       verify path doubles as a hint (detection found the marker there).
    2. ``./Cargo.toml`` -- the ordinary manifest-at-root layout.
    3. The shallowest ``Cargo.toml`` within 3 levels (skipping ``target/``),
       depth-first so a workspace root wins over its member crates.

    Returns the manifest path relative to *workdir*, or ``None`` when the
    tree has no manifest at all -- callers must report that as an error,
    never run cargo anyway.
    """
    candidates = [path] if isinstance(path, str) else list(path)
    probes = [f"{c.rstrip('/')}/Cargo.toml" for c in candidates if c not in (".", "")]
    probes.append("Cargo.toml")
    probe_cmd = "; ".join(
        f'test -f {shlex.quote(p)} && echo {shlex.quote(p)} && exit 0' for p in probes
    )
    ec, out, _ = _exec_run(
        container, ["/bin/sh", "-c", f"({probe_cmd}; exit 1)"], workdir=workdir
    )
    if ec == 0 and out.strip():
        return out.strip().split("\n")[0]

    ec, out, _ = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            "for d in 1 2 3; do"
            "  find . -mindepth $d -maxdepth $d -name Cargo.toml"
            "    -not -path '*/target/*' 2>/dev/null | sort | head -1 | grep . && exit 0;"
            "done; exit 1",
        ],
        workdir=workdir,
    )
    if ec == 0 and out.strip():
        return out.strip().split("\n")[0]
    return None


def _run_clippy_verify(
    container: Any, path: str | Sequence[str], workdir: str | None = None
) -> VerifyResult:
    """Run cargo clippy on *path*.  Returns VerifyResult envelope.

    *path* is accepted for interface parity with the other lint runners
    (every branch of ``_gate_lint_runner`` / dispatch-table entry has the
    same call shape) but is not passed to cargo: unlike ``ruff``/
    ``eslint``, which take a file/dir argument, ``cargo clippy`` always
    lints the crate(s) rooted at the manifest found in *workdir* (or the
    container's default working directory) -- there is no cargo flag
    that means "lint just this subdirectory".  ``--workspace`` is passed
    explicitly so a workspace manifest that declares only ``[workspace]``
    and no root ``[package]`` still gets every member crate linted; this
    is the shape of the first real Rust consumer of this gate.

    Findings keep clippy's own per-message ``"severity"``
    (``_parse_clippy_output`` sets it from the JSON ``level`` field), so
    a clean-but-for-warnings run and a run with real compile errors both
    return ``status="findings"`` -- like every other lint runner here --
    but are distinguishable by finding severity rather than collapsed
    into one undifferentiated bucket.  ``cargo clippy`` (like every other
    cargo subcommand) exits 101 on any error-level diagnostic or a
    genuine compile failure, and 0 when there is nothing above a
    warning; both are treated as a normal (non-execution-error) run.
    """
    manifest = _resolve_cargo_manifest(container, path, workdir=workdir)
    if manifest is None:
        # Probe cargo availability first so a rust-less container still
        # reports not_available (the flag the gate's incompleteness check
        # keys on) rather than a confusing "no manifest" error.
        probe_ec, _, _ = _exec_run(
            container, ["/bin/sh", "-c", "command -v cargo"], workdir=workdir
        )
        if probe_ec != 0:
            return _envelope_not_available("clippy", "cargo/clippy not installed in container")
        return _envelope_error(
            "clippy", f"no Cargo.toml found under {workdir or 'the working directory'}", 1
        )

    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_RUST_ENV}cargo clippy --workspace "
            f"--manifest-path {shlex.quote(manifest)} --message-format=json",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("clippy", "cargo/clippy not installed in container")
    if ec not in (0, 101):
        return _envelope_error("clippy", stderr_text.strip() or f"exit code {ec}", ec)

    findings = _parse_clippy_output(stdout_text, _path_display(path))
    if ec == 101 and not findings:
        # 101 with zero diagnostics is an infrastructure failure (broken
        # lockfile, missing member, ...), not a clean run.  Reporting it
        # green is the eslint-exit-2 bug (#740) all over again.
        return _envelope_error(
            "clippy",
            "cargo clippy exited 101 without emitting any diagnostic:\n"
            + (stderr_text.strip()[-2000:] or "no stderr"),
            ec,
        )
    return _envelope_ok("clippy", findings, ec)


def _run_rust_type_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Rust's "type" layer: deliberately folded into the lint layer.

    Rust has no standalone type checker the way Python has pyright or
    TypeScript has tsc.  Clippy runs the full rustc frontend (type
    checking and borrow checking included) before its own lints run, so
    any type error already surfaces as an ``"error"``-severity finding
    from :func:`_run_clippy_verify`.

    This returns ``status="skipped"`` with a specific reason rather than
    ``status="not_available"`` (which would mark the pre-publish gate's
    ``lint_type_incomplete`` flag -- correctly reserved for "a tool that
    should have run did not", not "this language folds type-checking
    into another layer on purpose") and rather than simply omitting a
    rust "type" entry from the dispatch table (which would fall through
    to the generic "language 'rust' has no type layer" message and be
    indistinguishable, at the call site, from a language nobody has
    gotten around to wiring up yet).  A dedicated function -- reused by
    both ``_DISPATCH["rust"]["type"]`` and ``_gate_type_runner`` -- keeps
    that reasoning in one place instead of two independent fallbacks
    silently agreeing by accident.
    """
    return _envelope_skipped(
        "rust-type",
        "Rust has no standalone type checker; type and borrow-check "
        "errors are reported by the clippy lint layer's rustc compile "
        "pass instead of a separate type layer.",
    )


def _run_pyright_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run pyright on *path*.  Returns VerifyResult envelope."""
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson {_quote_path(path)}",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("pyright", "pyright not installed in container")

    findings = _parse_pyright_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"

    if ec not in (0, 1) and not findings:
        return _envelope_error("pyright", stderr_text.strip() or f"exit code {ec}", ec)

    return _envelope_ok("pyright", findings, ec)


def _run_tsc_verify(container: Any, path: str, workdir: str | None = None) -> VerifyResult:
    """Run tsc --noEmit on *path*.  Returns VerifyResult envelope.

    Resolves ``node_modules/.bin/tsc`` before the image-baked global
    (Issue #588); the envelope's ``detail`` always says which one ran.
    Invokes the resolved binary directly instead of ``npx`` so the
    resolution is explicit and identical across eslint/tsc/jest, rather
    than relying on npx's own (differently-behaved) fallback search.
    """
    cmd, source = _resolve_js_tool(container, "tsc", workdir=workdir)
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --noEmit {_quote_path(path)} 2>&1",
        ],
        workdir=workdir,
    )
    combined = stdout_text + stderr_text

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("tsc", "typescript (tsc) not installed in container"),
            source, cmd,
        )
    if ec not in (0, 1, 2):
        return _annotate_resolution(
            _envelope_error("tsc", combined.strip() or f"exit code {ec}", ec), source, cmd
        )

    findings = _parse_tsc_text(combined, path)
    if not findings:
        findings = _parse_tsc_json(combined, path)
    for r in findings:
        r["severity"] = "error"
    return _annotate_resolution(_envelope_ok("tsc", findings, ec), source, cmd)
