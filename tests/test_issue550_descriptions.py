"""Tool descriptions and server instructions fit the 2 KB budget (Issue #550).

Claude Code truncates each tool description to its first 2048 bytes, so
anything past that limit is silently lost.  Docstrings therefore carry only
the per-tool interface contract, while the cross-tool workflow map lives in
the server-level ``instructions`` field -- which must itself stay within the
same budget.
"""
from __future__ import annotations

import asyncio
import re

from sunaba import server
from sunaba.workflow_guide import _load_guide, _parse_phases

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


# Aggregate budgets: measured after the #550 docstring diet (desc 8249 B,
# param descriptions 8040 B across 28 tools), capped at roughly +10% so the
# surface cannot quietly regrow tool by tool.
#
# Raised deliberately, twice, and recorded here each time -- the point of the
# cap is that growth is a visible decision rather than the sum of many quiet
# ones.  Raise it only for genuinely new surface, by what that surface actually
# needs, and say what needed it:
#
# - descriptions 10752 -> 11264: #676 added secret_scan_override and #675 added
#   merge_base / merge_complete / merge_abort, taking the tool count 28 -> 33.
#   Their descriptions were trimmed first (merge_complete 302 -> 153 B) and only
#   the remainder was budgeted.
# - param descriptions 9984 -> 10496: #675's `branch=` parameter, plus the
#   parameters of the three merge tools above (measured 10390 B).  An earlier
#   attempt set 11264, leaving ~1.1 KB of unearned headroom -- exactly the quiet
#   regrowth this cap exists to prevent.
TOTAL_DESCRIPTION_BYTE_LIMIT = 11264
TOTAL_PARAM_DESCRIPTION_BYTE_LIMIT = 10496


def _param_desc_bytes(tool) -> int:
    props = (tool.parameters or {}).get("properties") or {}
    return sum(_byte_len(prop.get("description")) for prop in props.values())


class TestAggregateBudget:
    """The whole tool surface stays lean, not just each tool alone.

    FastMCP splits every docstring: the body before ``Args:`` becomes the
    client-visible description, each ``Args:`` entry becomes the matching
    JSON-schema parameter description, and everything after ``Args:``
    (including ``Returns:``) is dropped from the description.  Both halves
    consume client context, so both get an aggregate cap.
    """

    def test_total_description_budget(self) -> None:
        sizes = {t.name: _byte_len(t.description) for t in _tools(server)}
        total = sum(sizes.values())
        assert total <= TOTAL_DESCRIPTION_BYTE_LIMIT, (
            f"tool descriptions total {total} bytes "
            f"(limit {TOTAL_DESCRIPTION_BYTE_LIMIT}); worst offenders: "
            f"{sorted(sizes.items(), key=lambda kv: -kv[1])[:5]}"
        )

    def test_total_param_description_budget(self) -> None:
        sizes = {t.name: _param_desc_bytes(t) for t in _tools(server)}
        total = sum(sizes.values())
        assert total <= TOTAL_PARAM_DESCRIPTION_BYTE_LIMIT, (
            f"schema parameter descriptions total {total} bytes "
            f"(limit {TOTAL_PARAM_DESCRIPTION_BYTE_LIMIT}); worst offenders: "
            f"{sorted(sizes.items(), key=lambda kv: -kv[1])[:5]}"
        )


class TestUnderscoreParams:
    """No internal-use-only parameters leak into the MCP schema."""

    def test_no_underscore_params(self) -> None:
        hidden: list[str] = []
        for t in _tools(server):
            props = (t.parameters or {}).get("properties") or {}
            for pname in props:
                if pname.startswith("_"):
                    hidden.append(f"{t.name}.{pname}")
        assert not hidden, f"internal parameters exposed in schema: {hidden}"


class TestWorkflowGuidePhases:
    """Phase names in get_workflow_guide's visible description match the shipped guide."""

    def test_phases_listed_in_description(self) -> None:
        guide_text = _load_guide()
        actual_phases = set(_parse_phases(guide_text).keys())

        tools = {t.name: t for t in _tools(server)}
        desc: str = tools["get_workflow_guide"].description or ""
        m = re.search(r"Valid phases:\s*(.+)", desc)
        assert m is not None, (
            "could not find 'Valid phases:' in get_workflow_guide description"
        )
        # The sentence ends in a period, which would otherwise ride along on
        # the last phase name.
        desc_phases = {p.strip().rstrip(".") for p in m.group(1).split(",")}

        assert desc_phases == actual_phases, (
            f"phase mismatch: description has {sorted(desc_phases)}, "
            f"guide has {sorted(actual_phases)}"
        )
