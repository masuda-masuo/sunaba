"""JS/TS tool resolution: repo node_modules/.bin wins over the baked global (Issue #588)."""

from __future__ import annotations

import json
from typing import Any

from .results import VerifyResult

# ---------------------------------------------------------------------------
# JS/TS tool resolution: repo node_modules/.bin wins over the baked global
# (Issue #588)
# ---------------------------------------------------------------------------
#
# Python's ``pip install -e .[dev]`` writes into the same venv the image
# already put on PATH, so the repo naturally wins.  Node has no equivalent:
# a globally baked eslint 9 hitting a repo pinned to eslint 8's config is a
# silent version mismatch, not an error -- the worst outcome for a verify
# gate (a repo could look "clean" only because the wrong linter ran).  So
# every js/ts runner resolves per-invocation instead of trusting PATH:
# ``node_modules/.bin/<tool>`` wins when it exists, the image-baked global
# is the fallback, and *which one ran* is always surfaced in the envelope's
# ``detail`` field -- never silent.


def _resolve_js_tool(container: Any, tool: str, workdir: str | None = None) -> tuple[str, str]:
    """Resolve *tool* (``eslint`` / ``tsc`` / ``jest``) to a command + source.

    Checks ``node_modules/.bin/<tool>`` relative to *workdir* (the
    container's own working directory -- normally the repo root -- when
    *workdir* is ``None``).  Returns ``(command, source)`` where *source*
    is ``"local"`` when the repo-pinned binary exists, or ``"global"`` when
    falling back to the image-baked one on ``PATH``.
    """
    ec, _ = container.exec_run(
        ["/bin/sh", "-c", f"test -x node_modules/.bin/{tool}"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    if ec == 0:
        return f"./node_modules/.bin/{tool}", "local"
    return tool, "global"


def _annotate_resolution(result: VerifyResult, source: str, cmd: str) -> VerifyResult:
    """Stamp *result*'s ``detail`` with which eslint/tsc/jest binary ran.

    Silently using a different tool version than the repo pins is the
    worst outcome for a verify gate (#588), so every eslint/tsc/jest
    envelope must say whether it ran the repository's
    ``node_modules/.bin`` binary or the image-baked global fallback.
    Test-layer results (jest) carry a JSON test report in ``detail`` that
    downstream code parses with ``json.loads`` (``tools/verify.py``); for
    those the resolution is injected as JSON fields instead of a text
    prefix so that contract survives untouched.
    """
    if result.detail:
        try:
            payload = json.loads(result.detail)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            payload["resolved_via"] = source
            payload["resolved_cmd"] = cmd
            result.detail = json.dumps(payload)
            return result
        result.detail = f"[resolved via {source}: {cmd}] {result.detail}"
    else:
        result.detail = f"resolved via {source}: {cmd}"
    return result


def _detect_js_test_runner(container: Any, workdir: str | None = None) -> str:
    """Tell jest and vitest projects apart via ``package.json`` (design §3).

    Only a jest adapter exists today (:class:`sunaba.test_report.JestAdapter`).
    Running the jest CLI against a vitest-only project would misparse
    vitest's own output as a crash, so a vitest project is reported
    honestly instead of forced through the wrong tool.

    Returns ``"vitest"`` only when ``vitest`` appears in dependencies (or
    the ``test`` script) and ``jest`` does not -- a project migrating
    between the two, or one that runs jest via a vitest-compatible shim,
    still gets the jest path.  Returns ``"jest"`` in every other case,
    including when ``package.json`` is missing or unreadable (matches the
    tool's prior unconditional-jest behavior).

    TODO(#588 follow-up): no VitestAdapter exists yet -- add one and
    dispatch to it here once vitest support is in scope.
    """
    ec, output = container.exec_run(
        ["/bin/sh", "-c", "cat package.json 2>/dev/null || true"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    if not raw.strip():
        return "jest"
    try:
        pkg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "jest"
    if not isinstance(pkg, dict):
        return "jest"
    deps: dict[str, Any] = {}
    deps.update(pkg.get("dependencies") or {})
    deps.update(pkg.get("devDependencies") or {})
    test_script = str((pkg.get("scripts") or {}).get("test") or "")
    has_vitest = "vitest" in deps or "vitest" in test_script
    has_jest = "jest" in deps or "jest" in test_script
    if has_vitest and not has_jest:
        return "vitest"
    return "jest"
