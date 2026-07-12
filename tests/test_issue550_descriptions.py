"""Tool descriptions and server instructions fit the 2 KB budget (Issue #550).

Claude Code truncates each tool description to its first 2048 bytes, so
anything past that limit is silently lost.  Docstrings therefore carry only
the per-tool interface contract, while the cross-tool workflow map lives in
the server-level ``instructions`` field -- which must itself stay within the
same budget.
"""
from __future__ import annotations

import asyncio

from sunaba import server

DESCRIPTION_BYTE_LIMIT = 2048


def _tools(mod):
    return asyncio.run(mod.mcp.list_tools())


def _byte_len(text: str | None) -> int:
    return len((text or "").encode("utf-8"))


class TestToolDescriptionBudget:
    """Every registered tool description must survive the 2 KB truncation."""

    def test_every_description_within_budget(self) -> None:
        over = {
            t.name: _byte_len(t.description)
            for t in _tools(server)
            if _byte_len(t.description) > DESCRIPTION_BYTE_LIMIT
        }
        assert not over, (
            f"tool descriptions exceed {DESCRIPTION_BYTE_LIMIT} bytes "
            f"(Claude Code silently truncates the rest): {over}"
        )

    def test_every_tool_has_a_description(self) -> None:
        missing = [
            t.name for t in _tools(server) if not (t.description or "").strip()
        ]
        assert not missing, f"tools without a description: {missing}"


class TestServerInstructions:
    """The workflow map is defined, wired into FastMCP, and within budget."""

    def test_instructions_within_budget(self) -> None:
        size = _byte_len(server.SERVER_INSTRUCTIONS)
        assert 0 < size <= DESCRIPTION_BYTE_LIMIT, (
            f"SERVER_INSTRUCTIONS is {size} bytes "
            f"(must be 1..{DESCRIPTION_BYTE_LIMIT})"
        )

    def test_instructions_wired_into_mcp(self) -> None:
        assert server.mcp.instructions == server.SERVER_INSTRUCTIONS

    def test_instructions_cover_workflow_phases(self) -> None:
        # The phase map replaces the per-docstring workflow rubrics, so the
        # core edit -> verify -> publish chain must actually be described.
        for keyword in ("sandbox_initialize", "verify_in_container", "publish"):
            assert keyword in server.SERVER_INSTRUCTIONS, (
                f"workflow map lost its {keyword!r} anchor"
            )
