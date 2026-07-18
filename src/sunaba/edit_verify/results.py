"""Status envelope types for verification results.

Defines :class:`VerifyResult` and its factory functions (``_envelope_*``)
used by all verification layer runners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ===========================================================================
# Status envelope (design-multilang-support.md S4)
# ===========================================================================


@dataclass
class VerifyResult:
    """Status envelope for a single verification layer.

    Each runner (lint / type / test / scan) returns one VerifyResult
    instead of a bare list of findings, so that errors, missing tools,
    and intentional skips are never silently treated as "clean".
    """

    tool: str
    status: str  # "ok" | "findings" | "not_available" | "error" | "skipped"
    findings: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    exit_code: int = -1


def _envelope_ok(tool: str, findings: list[dict[str, Any]] | None = None, exit_code: int = 0) -> VerifyResult:
    if findings is None:
        findings = []
    return VerifyResult(
        tool=tool,
        status="findings" if findings else "ok",
        findings=findings,
        exit_code=exit_code,
    )


def _envelope_not_available(tool: str, detail: str = "") -> VerifyResult:
    return VerifyResult(
        tool=tool, status="not_available", detail=detail, exit_code=127,
    )


def _envelope_error(tool: str, detail: str, exit_code: int) -> VerifyResult:
    return VerifyResult(
        tool=tool, status="error", detail=detail, exit_code=exit_code,
    )


def _envelope_skipped(tool: str, reason: str) -> VerifyResult:
    return VerifyResult(
        tool=tool, status="skipped", detail=reason,
    )
